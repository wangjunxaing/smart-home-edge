#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MQTT vs ESP-NOW 对比实测（5.4）— 在 Orange Pi 上运行
=====================================================
同一节点、同一上报周期，交替切换 transport=mqtt / espnow，阶段内均匀下发灯带命令，采集：

  1) 控制时延：下发 → 节点 ACK（服务器时钟）
       - MQTT：  [MQTT-ACK]   <dev8> seq=N latency_ms=..
       - ESP-NOW：[ESPNOW-ACK] <dev8> seq=N latency_ms=..  rssi=..
  2) 上行到达：阶段内 [MQTT-UP]/[ESPNOW-UP] 打点数 + SQLite 落库行数（对照期望行数）
  3) 上行抖动：设备 t0 间隔 vs 服务器 recv 间隔之差
  4) 流量：
       - MQTT：tcpdump 抓 wlan0 上该设备的 1883 端口，按 length 统计字节/分钟
       - ESP-NOW：不经 IP，用网关 USB 字节计数（usb_tx_bytes/usb_rx_bytes）差值折算空口字节
  5) 功耗：上行打点里的 cur=（INA219 实测电流 mA）
  6) ESP-NOW 专属：网关 data_events / crc_bad / 序号缺口 gaps / 节点 RSSI

切换说明：服务器 /api/esp32/config/update 内部按设备当前 transport 路由下行，
  - 当前 MQTT  → 经 MQTT 控制面（≤30s 生效）
  - 当前 ESP-NOW → 经网关 ESP-NOW CMD（秒级生效）
两个方向都无需等 10 分钟维护窗口（维护窗口只用于配置兜底与 OTA）。

用法（推荐 2 阶段 ×15 分钟）：
  python3 measure_mqtt_espnow.py --device A1B2C3D4E5F6 --minutes 15 --phases espnow,mqtt \
      --pokes 30 --capture-iface wlan0 --device-ip 10.0.0.50

烟测：
  python3 measure_mqtt_espnow.py --device A1B2C3D4E5F6 --minutes 1 --phases espnow,mqtt \
      --pokes 3 --capture-iface wlan0

结果：/home/ubuntu/ab_results/v3/<run_id>.md / .json
"""
import argparse
import datetime as dt
import http.cookiejar
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.request

BASE = 'http://127.0.0.1:8080'
HERE = os.path.dirname(os.path.abspath(__file__))
SENSORS_DB = os.path.join(HERE, '..', 'sensors_history.db')
GW_STATS = '/home/ubuntu/ab_results/espnow_gateway_stats.json'
RESULT_DIR = '/home/ubuntu/ab_results/v3'
INTERVAL_MS = 5000          # 设备上报周期（与 config send_interval 一致）

_UP_RE = re.compile(r'\[(MQTT-UP|ESPNOW-UP|HTTP-UP)\] (\w+) t0=(\d+) recv_ms=(\d+)(?: cur=([\d.]+))?')
_ACK_RE = re.compile(r'\[(MQTT-ACK|ESPNOW-ACK)\] (\w+) seq=(\d+) latency_ms=(-?\d+)(?: rssi=(-?\d+))?')
_IP_RE = re.compile(r'192\.168\.\d+\.\d+')

# ESP-NOW 空口帧长常量（与 shared/espnow_proto.h 一致）
AIR_HDR = 15          # AA55|ver|type|dev6|seq2|flags|len2
AIR_CRC = 2
USB_HDR = 6           # ABCD|type|len2 + 尾部 CRC2
ACK_PAYLOAD = 4       # seq2|rssi|flags
ACK_AIR_LEN = AIR_HDR + ACK_PAYLOAD + AIR_CRC

COOKIES = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIES))


def api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with OPENER.open(req, timeout=10) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f'  [api] {method} {path} 失败: {e}')
        return {'status': 'error', 'message': str(e)}


def iso(ts):
    return ts.strftime('%Y-%m-%d %H:%M:%S')


def journal_lines(since=None, until=None, tail=None):
    cmd = ['journalctl', '-u', 'smart-server', '-o', 'short-iso', '--no-pager']
    if since:
        cmd += ['--since', since]
    if until:
        cmd += ['--until', until]
    if tail:
        cmd += ['-n', str(tail)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=40).stdout
    except Exception:
        return []
    lines = []
    for ln in out.splitlines():
        try:
            ts_str, _, rest = ln.split(' ', 2)
            lines.append((dt.datetime.fromisoformat(ts_str),
                          re.sub(r'^python3\[\d+\]:\s*', '', rest)))
        except Exception:
            continue
    return lines


def gateway_stats():
    """网关落盘统计（usb_gateway 每 10s 刷一次）；不可用时返回 {}"""
    try:
        with open(GW_STATS, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def wait_channel(device8, mode, timeout_s=240):
    """等待目标通道出现上行打点（切换生效判定）"""
    want = 'ESPNOW-UP' if mode == 'espnow' else 'MQTT-UP'
    pat = re.compile(r'\[(?:MQTT-UP|ESPNOW-UP)\] ' + re.escape(device8) + r'\b')
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        since = (dt.datetime.now() - dt.timedelta(seconds=8)).strftime('%Y-%m-%d %H:%M:%S')
        for _, msg in journal_lines(since=since):
            if pat.search(msg) and want in msg:
                return True
        time.sleep(3)
    return False


def do_login(user, pw):
    r = api('POST', '/api/login', {'username': user, 'password': pw})
    if not (r.get('success') is True or r.get('status') == 'ok'):
        sys.exit(f'登录失败: {r}')
    print('  登录成功 role =', r.get('role'))


def set_transport(device, mode):
    r = api('POST', '/api/esp32/config/update', {'device_id': device, 'transport': mode})
    print(f'  [config] transport={mode} -> {json.dumps(r, ensure_ascii=False)[:160]}')


def poke_light(device, on):
    return api('POST', '/api/admin/light/set',
               {'device_id': device, 'on': on, 'r': 255, 'g': 160, 'b': 0, 'brightness': 80})


def pct(v, p):
    s = sorted(v)
    if not s:
        return None
    if len(s) < 20:
        return round(s[min(len(s) - 1, int(len(s) * p))], 1)
    return round(statistics.quantiles(s, n=20)[int(p * 20) - 1], 1)


def analyze_pcap(pcap):
    """tcpdump 读 pcap：按每包行尾 'length N' 统计字节/分钟 平均与峰值"""
    if not pcap or not os.path.isfile(pcap) or os.path.getsize(pcap) == 0:
        return None
    try:
        r = subprocess.run(['sudo', '-n', 'tcpdump', '-nn', '-tt', '-r', pcap],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        print('  [tcpdump] 失败:', e)
        return None
    buckets = {}
    total = 0
    n = 0
    for ln in r.stdout.splitlines():
        m = re.search(r'length (\d+)', ln)
        if not m:
            continue
        try:
            ts = float(ln.split()[0])
        except (ValueError, IndexError):
            ts = None
        blen = int(m.group(1))
        total += blen
        n += 1
        if ts is not None:
            b = int(ts // 60)
            buckets[b] = buckets.get(b, 0) + blen
    if n == 0:
        return {'frames': 0, 'bytes': 0, 'avg_bytes_per_min': 0, 'peak_bytes_per_min': 0}
    dur_min = 1.0
    if buckets:
        dur_min = max(1.0, (max(buckets) * 60 + 60 - min(buckets) * 60) / 60.0)
    return {'frames': n, 'bytes': total,
            'avg_bytes_per_min': round(total / dur_min, 1),
            'peak_bytes_per_min': max(buckets.values()) if buckets else 0}


def espnow_traffic(snap0, snap1, window_s):
    """ESP-NOW 流量折算：上行 USB 帧去掉 USB 封装 = 空口上行帧；
    再加网关→节点 ACK 与 节点→网关 ACK（每个 ACK 帧 21B）。"""
    s0, s1 = snap0.get('stats', {}), snap1.get('stats', {})
    n0, n1 = snap0.get('nodes', {}), snap1.get('nodes', {})

    def d(k):
        return max(0, int(s1.get(k, 0)) - int(s0.get(k, 0)))

    usb_tx = d('usb_tx_bytes')      # Pi→网关 方向（下行 CMD 帧）
    usb_rx = d('usb_rx_bytes')      # 网关→Pi 方向（上行业务事件帧）
    frames = d('usb_frames')
    data_ev = d('data_events')
    cmd_sent = d('cmd_sent')
    air_up = max(0, usb_rx - USB_HDR * frames)                    # 上行数据帧空口字节
    air_ack_down = ACK_AIR_LEN * data_ev                          # 网关→节点 ACK
    air_ack_up = ACK_AIR_LEN * cmd_sent                           # 节点→网关 ACK
    air_total = air_up + air_ack_down + air_ack_up
    per_min = round(air_total / max(window_s / 60.0, 0.01), 1)
    return {
        'usb_bytes_up': usb_rx, 'usb_bytes_down': usb_tx,
        'air_bytes_est': air_total, 'avg_bytes_per_min': per_min,
        'data_events': data_ev, 'cmd_sent': cmd_sent,
        'air_up_bytes_per_min': round(air_up / max(window_s / 60.0, 0.01), 1),
    }


def phase_stats(device, t0, t1, mode, snap0, snap1, pcap=None, t_silent=None):
    device8 = device[:8]
    lines = journal_lines(since=iso(t0), until=iso(t1))
    # journalctl short-iso 带时区（aware），t0/t1/t_silent 是 naive → 统一成 naive 再比较
    lines = [(ts.replace(tzinfo=None) if ts.tzinfo else ts, msg) for ts, msg in lines]
    up, ack = [], []
    for ts, msg in lines:
        m = _UP_RE.search(msg)
        if m and m.group(2) == device8:
            up.append({'kind': m.group(1), 't0': int(m.group(3)), 'recv': int(m.group(4)),
                       'ts': ts, 'cur': float(m.group(5)) if m.group(5) else None})
        m = _ACK_RE.search(msg)
        if m and m.group(2) == device8:
            ack.append({'kind': m.group(1), 'seq': int(m.group(3)), 'lat': int(m.group(4)),
                        'rssi': int(m.group(5)) if m.group(5) else None})

    want_up = 'ESPNOW-UP' if mode == 'espnow' else 'MQTT-UP'
    want_ack = 'ESPNOW-ACK' if mode == 'espnow' else 'MQTT-ACK'
    up = [x for x in up if x['kind'] == want_up]
    ack = [x for x in ack if x['kind'] == want_ack]
    lat = [x['lat'] for x in ack if x['lat'] >= 0]
    rssi = [x['rssi'] for x in ack if x['rssi'] is not None]

    recv_ms = sorted(x['recv'] for x in up)
    t0_ms = sorted(x['t0'] for x in up)
    jitter = None
    if len(recv_ms) > 2:
        dr = [recv_ms[i] - recv_ms[i - 1] for i in range(1, len(recv_ms))]
        d0 = [t0_ms[i] - t0_ms[i - 1] for i in range(1, len(t0_ms))]
        pairs = [(a, b) for a, b in zip(dr, d0) if 0 < b < 20000 and a < 20000]
        jitter = [a - b for a, b in pairs]
    power = [x['cur'] for x in up if x['cur'] is not None]
    # 静默期功耗（控制命令结束后、灯带已关）：与论文 5.1 稳态功耗口径可比
    power_silent = [x['cur'] for x in up
                    if x['cur'] is not None and t_silent is not None and x['ts'] >= t_silent]

    db_rows = None
    try:
        conn = sqlite3.connect(SENSORS_DB)
        db_rows = conn.execute(
            'SELECT COUNT(*) FROM sensor_history WHERE esp32_id=? AND timestamp>=? AND timestamp<=?',
            (device, iso(t0), iso(t1))).fetchone()[0]
        conn.close()
    except Exception:
        pass

    window_s = (t1 - t0).total_seconds()
    expected = max(0, int(window_s * 1000 / INTERVAL_MS))
    stat = {'mode': mode, 'window_s': round(window_s, 1),
            'up_samples': len(recv_ms), 'db_rows': db_rows, 'expected_rows': expected}
    if expected:
        stat['uplink_arrival_pct'] = round(100.0 * len(recv_ms) / expected, 1)
    if lat:
        stat['ctrl_latency_ms'] = {'n': len(lat), 'min': min(lat), 'p50': pct(lat, 0.5),
                                   'p95': pct(lat, 0.95), 'max': max(lat)}
    if jitter:
        stat['uplink_jitter_ms'] = {'n': len(jitter), 'p50': pct(jitter, 0.5),
                                    'p95': pct(jitter, 0.95), 'max': max(jitter)}
    if power:
        stat['power_ma'] = {'n': len(power), 'avg': round(sum(power) / len(power), 1),
                            'min': min(power), 'max': max(power)}
    if power_silent:
        stat['power_ma_silent'] = {'n': len(power_silent),
                                   'avg': round(sum(power_silent) / len(power_silent), 1),
                                   'min': min(power_silent), 'max': max(power_silent)}
    if rssi:
        stat['rssi'] = {'avg': round(sum(rssi) / len(rssi), 1), 'min': min(rssi), 'max': max(rssi)}

    # 流量
    if snap0 and snap1:
        if mode == 'espnow':
            trf = espnow_traffic(snap0, snap1, window_s)
            dev0 = (snap0.get('nodes') or {}).get(device, {})
            dev1 = (snap1.get('nodes') or {}).get(device, {})
            trf['node_rx'] = max(0, int(dev1.get('rx', 0)) - int(dev0.get('rx', 0)))
            trf['node_gaps'] = max(0, int(dev1.get('gaps', 0)) - int(dev0.get('gaps', 0)))
            trf['node_rssi'] = dev1.get('rssi')
            trf['crc_bad'] = max(0, int((snap1.get('stats') or {}).get('crc_bad', 0))
                                 - int((snap0.get('stats') or {}).get('crc_bad', 0)))
            trf['ack_events'] = max(0, int((snap1.get('stats') or {}).get('ack_events', 0))
                                    - int((snap0.get('stats') or {}).get('ack_events', 0)))
            stat['traffic'] = trf
            cap = trf['node_rx'] + trf['node_gaps']
            if cap > 0:
                stat['uplink_loss_pct'] = round(100.0 * trf['node_gaps'] / cap, 2)
        elif pcap:
            stat['traffic'] = analyze_pcap(pcap)
    return stat


def md_table(stats):
    out = ['| 阶段 | 传输 | 控制样本 | ctrl p50 | ctrl p95 | ctrl max | 上行样本 | 到达率 | 抖动p95 | 流量B/m | 静默功耗mA | 空口/丢包 |',
           '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for s in stats:
        lat = s.get('ctrl_latency_ms'); jit = s.get('uplink_jitter_ms')
        trf = s.get('traffic') or {}; pw = s.get('power_ma_silent') or s.get('power_ma')
        extra = '-'
        if s['mode'] == 'espnow':
            extra = (f"usb {trf.get('usb_bytes_up', '-')}B up / gaps {trf.get('node_gaps', '-')}"
                     f" / loss {s.get('uplink_loss_pct', '-')}%")
        out.append(f"| {s.get('order')} | {s['mode']} | {lat['n'] if lat else '-'} | "
                   f"{lat['p50'] if lat else '-'} | {lat['p95'] if lat else '-'} | "
                   f"{lat['max'] if lat else '-'} | {s['up_samples']} | "
                   f"{s.get('uplink_arrival_pct', '-')}% | {jit['p95'] if jit else '-'} | "
                   f"{trf.get('avg_bytes_per_min', '-')} | {pw['avg'] if pw else 'N/A'} | {extra} |")
    out.append('')
    out.append('> 控制时延＝服务器下发→收到节点 ACK（同一服务器时钟）。MQTT 走 WiFi/1883，'
               'ESP-NOW 走网关 USB→2.4G 空口。')
    out.append('> ESP-NOW 流量口径：网关 USB 字节去掉 USB 封装＝上行空口帧，再加双向 ACK（21B/帧）；'
               'MQTT 流量为 tcpdump 抓到的 IP 总字节（含 TCP/IP 头）。两者口径不同，只作量级对比。')
    out.append('> 到达率＝阶段内 [X-UP] 打点数 / (阶段秒数/上报周期)。')
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser(description='MQTT vs ESP-NOW 对比实测（5.4）')
    ap.add_argument('--device', default='A1B2C3D4E5F6', help='节点 12 位设备 ID（替换为你的设备）')
    ap.add_argument('--minutes', type=int, default=15, help='每阶段分钟数')
    ap.add_argument('--phases', default='espnow,mqtt', help='阶段序列，逗号分隔')
    ap.add_argument('--cycles', type=int, default=1, help='阶段序列重复轮数')
    ap.add_argument('--pokes', type=int, default=30, help='每阶段灯带下发次数')
    ap.add_argument('--poke-interval', type=int, default=5, help='下发最小间隔秒')
    ap.add_argument('--settle', type=int, default=15, help='通道切换后静置秒数')
    ap.add_argument('--switch-timeout', type=int, default=180,
                    help='切到 ESP-NOW 的等待超时（MQTT 模式配置周期 30s，通常 1~2min 生效）')
    ap.add_argument('--espnow-exit-timeout', type=int, default=700,
                    help='ESP-NOW→MQTT 的等待超时：ESP-NOW 模式配置只能靠 10min 维护窗口拉取')
    ap.add_argument('--user', default=os.environ.get('SMART_WEB_USER', 'admin'))
    ap.add_argument('--passwd', default=os.environ.get('SMART_WEB_PASS', 'admin123'),
                    help='服务器登录口令（默认取环境变量 SMART_WEB_PASS）')
    ap.add_argument('--capture-iface', default='', help='MQTT 阶段抓包网卡（如 wlan0）')
    ap.add_argument('--device-ip', default='', help='设备 IP，如 10.0.0.50（MQTT 抓包过滤用）')
    args = ap.parse_args()

    device = args.device.upper()
    os.makedirs(RESULT_DIR, exist_ok=True)
    run_id = dt.datetime.now().strftime('%Y%m%d_%H%M%S')

    print('== 前置自检 ==')
    if args.capture_iface:
        if subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode != 0:
            print('  !! sudo -n 失败：需 NOPASSWD tcpdump')
        elif subprocess.run(['sh', '-c', 'command -v tcpdump'], capture_output=True).returncode != 0:
            print('  !! tcpdump 未安装')
        else:
            print('  tcpdump / sudo 就绪')
    if not gateway_stats():
        print('  !! 未读到网关统计文件（ESP-NOW 阶段流量会缺）：', GW_STATS)
    else:
        print('  网关统计就绪:', GW_STATS)

    print('== 登录 ==')
    do_login(args.user, args.passwd)

    phases = []
    for _ in range(args.cycles):
        phases += [p.strip() for p in args.phases.split(',') if p.strip()]
    print(f'== 阶段序列: {phases}（每阶段 {args.minutes} 分钟）==')

    all_stats = []
    for i, mode in enumerate(phases):
        print(f'\n===== 阶段 {i + 1}/{len(phases)}: {mode.upper()} =====')
        set_transport(device, mode)
        # 切到 ESP-NOW：MQTT 模式下 ≤30s 拉到配置；切回 MQTT：ESP-NOW 模式只能等维护窗口
        cto = args.switch_timeout if mode == 'espnow' else args.espnow_exit_timeout
        if not wait_channel(device[:8], mode, timeout_s=cto):
            print(f'  !! {cto}s 内未见 {mode.upper()} 上行打点，仍继续（可能首帧较慢）')
        time.sleep(args.settle)

        pcap = None
        cap = None
        if mode == 'mqtt' and args.capture_iface and args.device_ip:
            pcap = f'{RESULT_DIR}/{run_id}_{mode}.pcap'
            cap = subprocess.Popen(
                ['sudo', '-n', 'tcpdump', '-i', args.capture_iface, '-w', pcap,
                 'tcp', 'and', f'host {args.device_ip}', 'and', '(', 'port', '8080',
                 'or', 'port', '1883', ')'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f'  [capture] -> {pcap}')

        snap0 = gateway_stats()
        t0 = dt.datetime.now()

        sent = 0
        phase_sec = args.minutes * 60
        t_silent = dt.datetime.now()
        if args.pokes > 0:
            # 控制采样集中在前 50%：后 50% 保持静默（灯带关闭），用于稳态功耗/流量对比
            poke_window = phase_sec * 0.5
            max_n = max(0, int((poke_window - 20) / max(args.poke_interval, 5)))
            n = min(args.pokes, max_n)
            if n > 0:
                step = max(args.poke_interval, (poke_window - 20) // n)
                for k in range(n):
                    if (dt.datetime.now() - t0).total_seconds() > poke_window - 20:
                        break
                    poke_light(device, k % 2 == 0)
                    sent += 1
                    time.sleep(step)
                print(f'  已下发 {sent} 次（间隔≈{step}s），恢复灯带关闭并进入静默采集')
        poke_light(device, False)          # 统一基线：静默期灯带必须为关
        t_silent = dt.datetime.now()

        remain = phase_sec - (dt.datetime.now() - t0).total_seconds()
        if remain > 0:
            time.sleep(remain)
        t1 = dt.datetime.now()
        snap1 = gateway_stats()

        if cap is not None:
            cap.terminate()
            try:
                cap.wait(timeout=10)
            except Exception:
                cap.kill()
            time.sleep(2)

        st = phase_stats(device, t0, t1, mode, snap0, snap1, pcap, t_silent=t_silent)
        st['silent_from'] = iso(t_silent)
        st['order'] = i + 1
        st['cmds_sent'] = sent
        if sent:
            got = (st.get('ctrl_latency_ms') or {}).get('n', 0)
            st['ctrl_ack_rate_pct'] = round(100.0 * got / sent, 1)
        all_stats.append(st)
        print(json.dumps(st, ensure_ascii=False, indent=2))

    print('\n================ 汇总 ================')
    md = md_table(all_stats)
    print(md)
    with open(f'{RESULT_DIR}/{run_id}.md', 'w', encoding='utf-8') as f:
        f.write(f'# MQTT vs ESP-NOW 对比实测 {run_id}\n\n设备 {device}，'
                f'每阶段 {args.minutes} 分钟，阶段序列 {phases}\n\n{md}\n')
    with open(f'{RESULT_DIR}/{run_id}.json', 'w', encoding='utf-8') as f:
        json.dump(all_stats, f, ensure_ascii=False, indent=2)
    print(f'\n结果已存: {RESULT_DIR}/{run_id}.md / .json')


if __name__ == '__main__':
    main()

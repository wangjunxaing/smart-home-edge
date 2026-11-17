#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A/B 对比实测 v2（论文完整数据采集）— 在 Orange Pi 上运行
==========================================================
在 measure_ab.py 基础上扩展：
  1) 阶段更长、下发更多（默认每阶段 60min、60 次灯带命令均匀分散）
  2) 每阶段 tcpdump 抓包 + tshark 统计 HTTP/MQTT 流量（字节/分钟 平均与峰值）
  3) 功耗采集位：从上报打点里收集 current_ma（需 INA219 接线 + 固件上报该字段，
     未接时该表输出"N/A"并给接线提示）
  4) 上行到达/抖动、控制时延统计（沿用 v1 方法）

前置条件（服务器侧，脚本会自检并提示）：
  - 已为 ubuntu 配置 NOPASSWD 运行 tcpdump（/etc/sudoers.d/smart-measure），脚本用 tcpdump 统计流量
  - 结果目录 /home/ubuntu/ab_results/v2/

用法示例（推荐：4 阶段 ×60 分钟 ≈ 4 小时，A/B/A/B）：
  sudo -n true && python3 measure_ab_v2.py --device 04B247F9EA08 \
      --minutes 60 --cycles 2 --pokes 60 --capture-iface wlan0

烟测：
  python3 measure_ab_v2.py --device 04B247F9EA08 --minutes 1 --cycles 1 --pokes 3 --capture-iface wlan0

抓包需要 root（tcpdump/tshark 已配 NOPASSWD）；功耗在 INA219 + 固件就绪前输出 N/A。
"""
import argparse
import datetime as dt
import http.cookiejar
import json
import os
import re
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.request

BASE = 'http://127.0.0.1:8080'
# 本脚本在 server/scripts/measure/，数据库在 server/data/（见 core/config.py）
SENSORS_DB = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          '..', '..', 'data', 'sensors_history.db'))
if not os.path.exists(SENSORS_DB):
    _alt = '/home/ubuntu/server/data/sensors_history.db'
    if os.path.exists(_alt):
        SENSORS_DB = _alt
RESULT_DIR = '/home/ubuntu/ab_results/v2'
INTERVAL_MS = 5000          # 设备上报周期（与 config send_interval 一致）

_UP_RE = re.compile(r'\[(MQTT-UP|HTTP-UP)\] (\w+) t0=(\d+) recv_ms=(\d+)( cur=([\d.]+))?')
_ACK_RE = re.compile(r'\[MQTT-ACK\] (\w+) seq=(\d+) latency_ms=(\d+)')
_CTRL_RE = re.compile(r'\[HTTP-CTRL\] (\w+) seq=(\d+) t_ms=(\d+)')
_SET_RE = re.compile(r'\[灯带\] (\w+) -> on=(True|False) .*seq=(\d+)')
_IP_RE = re.compile(r'192\.168\.\d+\.\d+')

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


def wait_channel(device8, kind, timeout_s=90):
    pat = re.compile(r'\[(?:HTTP-UP|MQTT-UP)\] ' + re.escape(device8) + r'\b')
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        since = (dt.datetime.now() - dt.timedelta(seconds=3)).strftime('%Y-%m-%d %H:%M:%S')
        for _, msg in journal_lines(since=since):
            if pat.search(msg) and ('MQTT-UP' in msg) == (kind == 'mqtt'):
                return True
        time.sleep(2)
    return False


def detect_device_ip(device):
    """从最近日志里该设备访问服务器的源 IP 推断（供抓包过滤）"""
    since = (dt.datetime.now() - dt.timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
    for _, msg in journal_lines(since=since, tail=2000):
        if device[:8].upper() in msg.upper() or device in msg:
            m = _IP_RE.search(msg)
            if m:
                return m.group(0)
    return ''


def do_login(user, pw):
    r = api('POST', '/api/login', {'username': user, 'password': pw})
    if not (r.get('success') is True or r.get('status') == 'ok'):
        sys.exit(f'登录失败: {r}')
    print('  登录成功 role =', r.get('role'))


def set_transport(device, mode):
    r = api('POST', '/api/esp32/config/update', {'device_id': device, 'transport': mode})
    print(f'  [config] transport={mode} -> {r.get("status")}')


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
    """tcpdump 读 pcap：按每包行尾 'length N' 统计字节/分钟 平均与峰值（无需 tshark）"""
    if not os.path.isfile(pcap) or os.path.getsize(pcap) == 0:
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


def phase_stats(device, t0, t1, kind):
    device8 = device[:8]
    lines = journal_lines(since=iso(t0), until=iso(t1))
    up, ack, ctrl, sett = [], [], [], []
    for ts, msg in lines:
        m = _UP_RE.search(msg)
        if m and m.group(2) == device8:
            up.append({'kind': m.group(1), 't0': int(m.group(3)),
                       'recv': int(m.group(4)),
                       'cur': float(m.group(6)) if m.group(6) else None})
        m = _ACK_RE.search(msg)
        if m and m.group(1) == device8:
            ack.append(int(m.group(3)))
        m = _CTRL_RE.search(msg)
        if m and m.group(1) == device8:
            ctrl.append((int(m.group(2)), ts))
        m = _SET_RE.search(msg)
        if m and m.group(1) == device8:
            sett.append((int(m.group(3)), ts))

    http_lat, seen = [], set()
    for seq, ts_set in sett:
        for seq2, ts_ctrl in ctrl:
            if seq2 == seq and seq2 not in seen and ts_ctrl >= ts_set:
                http_lat.append((ts_ctrl - ts_set).total_seconds() * 1000.0)
                seen.add(seq)
                break
    lat = ack if kind == 'mqtt' else [round(x, 1) for x in http_lat]

    up_kind = 'MQTT-UP' if kind == 'mqtt' else 'HTTP-UP'
    recv_ms = sorted(x['recv'] for x in up if x['kind'] == up_kind)
    t0_ms = sorted(x['t0'] for x in up if x['kind'] == up_kind)
    jitter = None
    if len(recv_ms) > 2:
        dr = [recv_ms[i] - recv_ms[i - 1] for i in range(1, len(recv_ms))]
        d0 = [t0_ms[i] - t0_ms[i - 1] for i in range(1, len(t0_ms))]
        pairs = [(a, b) for a, b in zip(dr, d0) if b > 0 and a < 20000]
        jitter = [a - b for a, b in pairs]
    power = [x['cur'] for x in up if x['cur'] is not None]

    db_rows = None
    try:
        conn = sqlite3.connect(SENSORS_DB)
        conn.execute  # noqa
        start, end = iso(t0), iso(t1)
        db_rows = conn.execute(
            'SELECT COUNT(*) FROM sensor_history WHERE esp32_id=? AND timestamp>=? AND timestamp<=?',
            (device, start, end)).fetchone()[0]
        conn.close()
    except Exception:
        pass

    stat = {'phase': kind, 'window_s': round((t1 - t0).total_seconds(), 1),
            'uplink_journal_samples': len(recv_ms), 'db_rows': db_rows,
            'expected_rows': max(0, int((t1 - t0).total_seconds() * 1000 / INTERVAL_MS))}
    if lat:
        stat['ctrl_latency_ms'] = {'n': len(lat), 'min': round(min(lat), 1),
                                   'p50': pct(lat, 0.5), 'p95': pct(lat, 0.95),
                                   'max': round(max(lat), 1)}
    if jitter:
        stat['uplink_jitter_ms'] = {'n': len(jitter), 'p50': pct(jitter, 0.5),
                                    'p95': pct(jitter, 0.95), 'max': round(max(jitter), 1)}
    if power:
        stat['power_ma'] = {'n': len(power), 'avg': round(sum(power) / len(power), 1),
                            'min': round(min(power), 1), 'max': round(max(power), 1)}
    return stat


def md_table(stats):
    out = ['| 阶段 | 传输 | 控制样本 | ctrl p50 | ctrl p95 | ctrl max | 上行样本 | DB行/预期 | 抖动p95 | 流量均B/m | 流量峰B/m | 功耗均值mA |',
           '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for s in stats:
        lat = s.get('ctrl_latency_ms'); jit = s.get('uplink_jitter_ms')
        trf = s.get('traffic'); pw = s.get('power_ma')
        out.append(f"| {s.get('order')} | {s['phase']} | "
                   f"{lat['n'] if lat else '-'} | {lat['p50'] if lat else '-'} | "
                   f"{lat['p95'] if lat else '-'} | {lat['max'] if lat else '-'} | "
                   f"{s['uplink_journal_samples']} | {s['db_rows'] or '-'}/{s['expected_rows']} | "
                   f"{jit['p95'] if jit else '-'} | "
                   f"{trf['avg_bytes_per_min'] if trf else '-'} | "
                   f"{trf['peak_bytes_per_min'] if trf else '-'} | "
                   f"{pw['avg'] if pw else 'N/A'} |")
    out.append('')
    out.append('> 控制时延：HTTP=下发→轮询到（受 2s 周期量化），MQTT=下发→ack（服务器时钟，绝对）。'
               '上行抖动为相对差；绝对上行时延需设备 NTP。功耗需 INA219 + 固件上报 current_ma。')
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser(description='5.1 HTTP vs MQTT A/B 完整实测 v2')
    ap.add_argument('--device', default='04B247F9EA08')
    ap.add_argument('--minutes', type=int, default=60)
    ap.add_argument('--cycles', type=int, default=2, help='配对轮数（2=HTTP+MQTT+HTTP+MQTT 共 4 阶段）')
    ap.add_argument('--pokes', type=int, default=60)
    ap.add_argument('--poke-interval', type=int, default=5)
    ap.add_argument('--user', default='admin')
    ap.add_argument('--passwd', default='admin123')
    ap.add_argument('--capture-iface', default='', help='抓包网卡（如 wlan0）；留空则只测时延/上行')
    ap.add_argument('--device-ip', default='', help='设备 IP（缺省自动从日志推断）')
    args = ap.parse_args()

    device = args.device.upper()
    os.makedirs(RESULT_DIR, exist_ok=True)
    run_id = dt.datetime.now().strftime('%Y%m%d_%H%M%S')

    # 前置自检
    print('== 前置自检 ==')
    if args.capture_iface:
        r = subprocess.run(['sudo', '-n', 'true'], capture_output=True)
        if r.returncode != 0:
            print('  !! sudo -n 失败：需为 ubuntu 配 NOPASSWD tcpdump，或先 sudo -n true')
            sys.exit(1)
        r = subprocess.run(['sh', '-c', 'command -v tcpdump'], capture_output=True)
        if r.returncode != 0:
            print('  !! tcpdump 未安装')
            sys.exit(1)
        print('  tcpdump / sudo 就绪（流量按 tcpdump length 统计，无需 tshark）')
    if not args.device_ip and args.capture_iface:
        args.device_ip = detect_device_ip(device)
        print('  自动推断设备 IP:', args.device_ip or '未找到（用 --device-ip 指定）')

    print('== 登录 ==')
    do_login(args.user, args.passwd)
    phases = []
    for _ in range(args.cycles):
        phases += ['http', 'mqtt']
    print(f'== 阶段序列: {phases}（每阶段 {args.minutes} 分钟）==')

    all_stats = []
    for i, kind in enumerate(phases):
        print(f'\n===== 阶段 {i + 1}/{len(phases)}: {kind.upper()} =====')
        set_transport(device, kind)
        ok = wait_channel(device[:8], kind, timeout_s=120)
        if not ok:
            print('  !! 120s 未见该通道打点，仍继续（计入窗口）')
        time.sleep(10)
        t0 = dt.datetime.now()

        pcap = None
        if args.capture_iface and args.device_ip:
            pcap = f'{RESULT_DIR}/{run_id}_{kind}.pcap'
            cap = subprocess.Popen(['sudo', '-n', 'tcpdump', '-i', args.capture_iface, '-w', pcap,
                                    'tcp', 'and', f'host {args.device_ip}',
                                    'and', '(', 'port', '8080', 'or', 'port', '1883', ')'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f'  [capture] -> {pcap}')
        else:
            cap = None

        poke_sent = 0
        if args.pokes > 0:
            phase_sec = args.minutes * 60
            max_n = max(0, int((phase_sec - 20) / max(args.poke_interval, 5)))
            n = min(args.pokes, max_n)
            if n <= 0:
                print('  阶段时长不足以下发，跳过控制采样')
            else:
                step = max(args.poke_interval, (phase_sec - 20) // n)
                for k in range(n):
                    if (dt.datetime.now() - t0).total_seconds() > phase_sec - 20:
                        break
                    poke_light(device, k % 2 == 0)
                    poke_sent += 1
                    time.sleep(step)
                print(f'  已均匀下发 {poke_sent} 次（间隔≈{step}s），进入静默采集...')

        remain = args.minutes * 60 - (dt.datetime.now() - t0).total_seconds()
        if remain > 0:
            time.sleep(remain)
        t1 = dt.datetime.now()

        if cap is not None:
            cap.terminate()
            try:
                cap.wait(timeout=10)
            except Exception:
                cap.kill()
            time.sleep(2)

        st = phase_stats(device, t0, t1, kind)
        st['order'] = i + 1
        if pcap:
            st['traffic'] = analyze_pcap(pcap)
            if st['traffic']:
                print('  [traffic]', st['traffic'])
        all_stats.append(st)
        print(json.dumps(st, ensure_ascii=False, indent=2))

    print('\n================ 汇总 ================')
    md = md_table(all_stats)
    print(md)
    with open(f'{RESULT_DIR}/{run_id}.md', 'w', encoding='utf-8') as f:
        f.write(md)
    with open(f'{RESULT_DIR}/{run_id}.json', 'w', encoding='utf-8') as f:
        json.dump(all_stats, f, ensure_ascii=False, indent=2)
    print(f'\n结果已存: {RESULT_DIR}/{run_id}.md / .json（pcap 同目录）')


if __name__ == '__main__':
    main()

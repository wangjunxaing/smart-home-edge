#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A/B 对比实测脚本（5.1 论文数据采集）— 在 Orange Pi 上运行
=========================================================
自动化执行 docs/comparison_http_vs_mqtt.md 的采集步骤：
  1) 按 phase 列表把设备 config transport 切到 http / mqtt（各 ≥30 分钟，A/B/A/B 交替）
  2) 每个 phase 内周期下发 N 次灯带命令（采集控制时延样本）
  3) 从 smart-server journal 收集四类打点，从 DB 统计上行到达率
  4) 可选 tcpdump/tshark 抓包统计流量字节
  5) 输出 markdown/json 汇总到 /home/ubuntu/ab_results/

用法示例（真实采集：http/mqtt 各 30 分钟交替两轮 ≈ 2 小时）:
  python3 measure_ab.py --device 04B247F9EA08 --minutes 30 --cycles 4 --pokes 24

快速烟测（各 1 分钟 × 2，验证链路用）:
  python3 measure_ab.py --device 04B247F9EA08 --minutes 1 --cycles 2 --pokes 3

默认每阶段均匀下发 20 次灯带命令采集控制时延；--pokes 0 则纯测上行不动灯。
可用 nohup 后台跑（不依赖登录会话）:
  nohup python3 -u measure_ab.py --device 04B247F9EA08 --minutes 30 --cycles 2 --pokes 20 \\
      > /home/ubuntu/ab_results/run.log 2>&1 &

可选流量抓包（需 tshark，否则只存 pcap 供离线分析）:
  python3 measure_ab.py --device 04B247F9EA08 --minutes 30 --capture-iface eth0
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
# 本脚本在 server/scripts/measure/，数据库在 server/data/（见 core/config.py）
SENSORS_DB = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          '..', '..', 'data', 'sensors_history.db'))
if not os.path.exists(SENSORS_DB):
    _alt = '/home/ubuntu/server/data/sensors_history.db'
    if os.path.exists(_alt):
        SENSORS_DB = _alt
RESULT_DIR = '/home/ubuntu/ab_results'
INTERVAL_MS = 5000          # 设备上报周期（与 config send_interval 一致）

_UP_RE = re.compile(r'\[(MQTT-UP|HTTP-UP)\] (\w+) t0=(\d+) recv_ms=(\d+)')
_ACK_RE = re.compile(r'\[MQTT-ACK\] (\w+) seq=(\d+) latency_ms=(\d+)')
_CTRL_RE = re.compile(r'\[HTTP-CTRL\] (\w+) seq=(\d+) t_ms=(\d+)')
_SET_RE = re.compile(r'\[灯带\] (\w+) -> on=(True|False) .*seq=(\d+)')

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


def iso(ts):  # datetime → journalctl --since/--until 格式
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
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    # short-iso: "2026-09-06T12:55:18.123456+0800 host python3[..]: msg"
    lines = []
    for ln in out.splitlines():
        try:
            ts_str, _, rest = ln.split(' ', 2)
            ts = dt.datetime.fromisoformat(ts_str)
            msg = re.sub(r'^python3\[\d+\]:\s*', '', rest)
            lines.append((ts, msg))
        except Exception:
            continue
    return lines


def wait_channel(device8, kind, timeout_s=75):
    """等待设备在目标通道上出现新打点（http→HTTP-UP / mqtt→MQTT-UP）"""
    pat = re.compile(r'\[(?:HTTP-UP|MQTT-UP)\] ' + re.escape(device8) + r'\b')
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for _, msg in journal_lines(since=(dt.datetime.now() - dt.timedelta(seconds=3)).strftime('%Y-%m-%d %H:%M:%S')):
            if pat.search(msg) and ('MQTT-UP' in msg) == (kind == 'mqtt'):
                return True
        time.sleep(2)
    return False


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


def phase_stats(device, t0, t1, kind):
    """从 journal 窗口解析控制时延与上行到达统计（device 为完整 ID）"""
    device8 = device[:8]
    dev = device8
    lines = journal_lines(since=iso(t0), until=iso(t1))
    up, ack, ctrl, sett = [], [], [], []
    for ts, msg in lines:
        m = _UP_RE.search(msg)
        if m and m.group(2) == dev:
            up.append((m.group(1), int(m.group(3)), int(m.group(4))))
        m = _ACK_RE.search(msg)
        if m and m.group(1) == dev:
            ack.append(int(m.group(3)))          # MQTT 端到端控制时延(ms)
        m = _CTRL_RE.search(msg)
        if m and m.group(1) == dev:
            ctrl.append((int(m.group(2)), ts))    # HTTP 轮询到达(seq, 时间)
        m = _SET_RE.search(msg)
        if m and m.group(1) == dev:
            sett.append((int(m.group(3)), ts))    # 下发 seq 时间

    # HTTP 控制时延 = 首个轮询到该 seq 的时刻 - 下发时刻（绝对，服务器时钟）
    http_lat = []
    seen = set()
    for seq, ts_set in sett:
        for seq2, ts_ctrl in ctrl:
            if seq2 == seq and seq2 not in seen and ts_ctrl >= ts_set:
                http_lat.append((ts_ctrl - ts_set).total_seconds() * 1000.0)
                seen.add(seq)
                break

    lat = ack if kind == 'mqtt' else [round(x, 1) for x in http_lat]

    # 上行到达率（DB 秒级计数）+ journal 到达间隔
    recv_ms = sorted(x[2] for x in up if x[0] == ('MQTT-UP' if kind == 'mqtt' else 'HTTP-UP'))
    t0_ms = sorted(x[1] for x in up if x[0] == ('MQTT-UP' if kind == 'mqtt' else 'HTTP-UP'))
    jitter = None
    if len(recv_ms) > 2:
        drecv = [recv_ms[i] - recv_ms[i - 1] for i in range(1, len(recv_ms))]
        dt0 = [t0_ms[i] - t0_ms[i - 1] for i in range(1, len(t0_ms))]
        pairs = [(a, b) for a, b in zip(drecv, dt0) if b > 0 and a < 20000]
        jitter = [a - b for a, b in pairs]       # 网络抖动（相对差，不需时钟同步）

    def q(v, p):
        return round(statistics.quantiles(sorted(v), n=20)[int(p * 20) - 1], 1) if len(v) >= 20 \
            else (round(sorted(v)[int(len(v) * p)], 1) if v else None)

    db_rows = None
    try:
        conn = sqlite3.connect(SENSORS_DB)
        start = t0.strftime('%Y-%m-%d %H:%M:%S')
        end = t1.strftime('%Y-%m-%d %H:%M:%S')
        db_rows = conn.execute(
            'SELECT COUNT(*) FROM sensor_history WHERE esp32_id=? AND timestamp>=? AND timestamp<=?',
            (device, start, end)).fetchone()[0]
        conn.close()
    except Exception as e:
        print('  [db]', e)

    stat = {
        'phase': kind,
        'window_s': round((t1 - t0).total_seconds(), 1),
        'uplink_journal_samples': len(recv_ms),
        'db_rows': db_rows,
        'expected_rows': max(0, int((t1 - t0).total_seconds() * 1000 / INTERVAL_MS)),
    }
    if lat:
        stat['ctrl_latency_ms'] = {'n': len(lat), 'min': round(min(lat), 1),
                                   'p50': q(lat, 0.5), 'p95': q(lat, 0.95),
                                   'max': round(max(lat), 1)}
    if jitter:
        stat['uplink_jitter_ms'] = {'n': len(jitter), 'p50': q(jitter, 0.5),
                                    'p95': q(jitter, 0.95), 'max': round(max(jitter), 1)}
    return stat


def md_table(stats):
    out = []
    out.append('| 阶段 | 控制时延样本 | min(ms) | p50(ms) | p95(ms) | max(ms) | 上行样本 | DB 行 | 预期行 |')
    out.append('|---|---|---|---|---|---|---|---|---|')
    for s in stats:
        lat = s.get('ctrl_latency_ms')
        out.append(f"| {s['phase']} | {lat['n'] if lat else '-'} | "
                   f"{lat['min'] if lat else '-'} | {lat['p50'] if lat else '-'} | "
                   f"{lat['p95'] if lat else '-'} | {lat['max'] if lat else '-'} | "
                   f"{s['uplink_journal_samples']} | {s['db_rows'] or '-'} | {s['expected_rows']} |")
    out.append('')
    out.append('> 说明：控制时延 HTTP=下发→设备轮询到（含 ≤2s 轮询周期）；MQTT=下发→ack（端到端，'
               '服务器时钟）。上行抖动为相对差（无需时钟同步）；绝对上行时延需 NTP 标定。')
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser(description='5.1 HTTP vs MQTT A/B 对比实测')
    ap.add_argument('--device', default='04B247F9EA08')
    ap.add_argument('--minutes', type=int, default=30, help='每阶段分钟数（论文建议 ≥30）')
    ap.add_argument('--cycles', type=int, default=2, help='交替轮数（如 2=HTTP+MQTT 各一轮）')
    ap.add_argument('--pokes', type=int, default=20, help='每阶段灯带下发次数（0=不控制，只测上行；默认 20）')
    ap.add_argument('--poke-interval', type=int, default=5, help='最小下发间隔秒（按阶段时长自动均匀化）')
    ap.add_argument('--user', default='admin')
    ap.add_argument('--passwd', default='admin123')
    ap.add_argument('--capture-iface', default='', help='可选：tcpdump 抓包网卡（需 sudo/tshark）')
    args = ap.parse_args()

    device = args.device.upper()
    os.makedirs(RESULT_DIR, exist_ok=True)
    run_id = dt.datetime.now().strftime('%Y%m%d_%H%M%S')

    print('== 登录 ==')
    do_login(args.user, args.passwd)

    # phase 序列：HTTP, MQTT, HTTP, MQTT ... 共 cycles 轮（cycles=1 只跑 HTTP？改成按 cycles 配对）
    phases = []
    for c in range(args.cycles):
        phases.append('http')
        phases.append('mqtt')
    print(f'== 阶段序列: {phases}（每阶段 {args.minutes} 分钟）==')

    all_stats = []
    for i, kind in enumerate(phases):
        print(f'\n===== 阶段 {i + 1}/{len(phases)}: {kind.upper()} =====')
        print('  切换 transport ...')
        set_transport(device, kind)
        ok = wait_channel(device[:8], kind, timeout_s=90)
        if not ok:
            print(f'  !! 90s 内未在 {kind} 通道看到设备打点，继续尝试（将计入窗口）')
        time.sleep(10)                       # 链路稳定期
        t0 = dt.datetime.now()

        if args.capture_iface:
            pcap = f'{RESULT_DIR}/{run_id}_{kind}.pcap'
            subprocess.Popen(['sudo', '-S', 'tcpdump', '-i', args.capture_iface, '-nn', '-w', pcap,
                              'port', '8080', 'or', 'port', '1883'],
                             stdin=subprocess.PIPE,
                             stderr=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL)
            print(f'  [capture] -> {pcap}')

        # 控制时延采样：在整个阶段窗口内均匀分散（避免集中闪烁，也利于统计）
        poke_sent = 0
        if args.pokes > 0:
            phase_sec = args.minutes * 60
            max_n = max(0, int((phase_sec - 20) / max(args.poke_interval, 5)))
            n = min(args.pokes, max_n)
            if n <= 0:
                print('  阶段时长不足以按给定间隔下发，跳过控制采样')
            else:
                step = max(args.poke_interval, (phase_sec - 20) // n)
                for k in range(n):
                    if (dt.datetime.now() - t0).total_seconds() > phase_sec - 20:
                        break
                    poke_light(device, k % 2 == 0)
                    poke_sent += 1
                    time.sleep(step)
                print(f'  已均匀下发 {poke_sent} 次灯带命令（间隔≈{step}s），进入静默采集...')

        # 静默采集至阶段结束
        remain = args.minutes * 60 - (dt.datetime.now() - t0).total_seconds()
        if remain > 0:
            time.sleep(remain)
        t1 = dt.datetime.now()

        print('  汇总阶段统计 ...')
        st = phase_stats(device, t0, t1, kind)
        all_stats.append(st)
        print(json.dumps(st, ensure_ascii=False, indent=2))

    md = md_table(all_stats)
    print('\n================ 汇总 ================')
    print(md)

    with open(f'{RESULT_DIR}/{run_id}.md', 'w', encoding='utf-8') as f:
        f.write(md)
    with open(f'{RESULT_DIR}/{run_id}.json', 'w', encoding='utf-8') as f:
        json.dump(all_stats, f, ensure_ascii=False, indent=2)
    print(f'\n结果已存: {RESULT_DIR}/{run_id}.md / .json')


if __name__ == '__main__':
    main()

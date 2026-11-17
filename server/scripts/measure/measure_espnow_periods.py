#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP-NOW 上报周期扫描（T4）— 在 Orange Pi 上运行
===============================================
对每个上报周期（默认 1s / 5s / 30s）各采集 N 分钟，统计：
  - 上行到达率（实收 DATA 帧 / 理论帧数）
  - 到达间隔 p50/p95 与抖动（相对期望周期）
  - 序号缺口（网关侧 gaps 增量，反映空口丢包）
  - 下行 RTT（每 probe_every 秒经控制口发一条无害 probe 命令，节点回 ACK）

配置经网关控制口下行 `{"type":"config","send_interval":N}`，ESP-NOW 模式下秒级生效，
不必等 10 分钟维护窗口。

用法：
  python3 measure_espnow_periods.py --dev 04B247F9EA08 --minutes 10
烟测：
  python3 measure_espnow_periods.py --dev 04B247F9EA08 --minutes 1 --periods 5000
结果：/home/ubuntu/ab_results/v3/period_sweep_<ts>.{md,json}
"""
import argparse
import http.cookiejar
import json
import os
import select
import socket
import time
import urllib.request

CTRL = ('127.0.0.1', 8765)
BASE = 'http://127.0.0.1:8080'
GW_STATS = '/home/ubuntu/ab_results/espnow_gateway_stats.json'
OUT_DIR = '/home/ubuntu/ab_results/v3'

_CJ = http.cookiejar.CookieJar()
_OP = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_CJ))


def api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with _OP.open(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


def login():
    try:
        api('POST', '/api/login', {'username': 'admin', 'password': 'admin123'})
        return True
    except Exception as e:
        print('  [api] 登录失败:', e)
        return False


def write_server_period(dev, period_ms):
    """同步写入服务器配置库：否则节点维护窗口（每 10min）拉配置会把周期改回 5000ms"""
    try:
        api('POST', '/api/esp32/config/update',
            {'device_id': dev, 'send_interval': int(period_ms)})
    except Exception as e:
        print('  [api] 写库失败:', e)


def ctrl(req, timeout=5):
    s = socket.create_connection(CTRL, timeout=timeout)
    s.sendall((json.dumps(req) + '\n').encode())
    data = b''
    s.settimeout(timeout)
    try:
        while b'\n' not in data:
            c = s.recv(1024)
            if not c:
                break
            data += c
    except Exception:
        pass
    s.close()
    try:
        return json.loads(data.split(b'\n', 1)[0].decode())
    except Exception:
        return {}


def gw_snapshot(dev):
    try:
        with open(GW_STATS, 'r', encoding='utf-8') as f:
            d = json.load(f)
        n = (d.get('nodes') or {}).get(dev, {})
        return {'gaps': int(n.get('gaps', 0)), 'rx': int(n.get('rx', 0)),
                'crc_bad': int((d.get('stats') or {}).get('crc_bad', 0))}
    except Exception:
        return {'gaps': 0, 'rx': 0, 'crc_bad': 0}


def pct(v, p):
    if not v:
        return None
    v = sorted(v)
    return round(v[min(len(v) - 1, int(len(v) * p))], 2)


def run_period(ev, dev, period_ms, minutes, probe_every, pseq):
    """返回该周期的统计字典；pseq 为 probe 序号计数器（list，便于回传）"""
    ctrl({'op': 'send', 'dev': dev, 'seq': 1,
          'text': json.dumps({'type': 'config', 'send_interval': period_ms})})
    write_server_period(dev, period_ms)
    print(f'  [config] send_interval -> {period_ms} ms（已同步服务器库），稳定 6s 后开始采集')
    time.sleep(6)

    snap0 = gw_snapshot(dev)
    t0 = time.time()
    t_end = t0 + minutes * 60
    arrivals, seqs, rtts = [], [], []
    gaps = 0
    last_seq = None
    pending = {}
    next_probe = t0 + 2
    rxbuf = b''

    while time.time() < t_end:
        now = time.time()
        if now >= next_probe:
            pseq[0] += 1
            pending[pseq[0]] = time.time()
            ctrl({'op': 'send', 'dev': dev, 'seq': pseq[0],
                  'text': json.dumps({'type': 'probe'})})
            next_probe = now + probe_every
        r, _, _ = select.select([ev], [], [], 0.002)
        if not r:
            continue
        try:
            chunk = ev.recv(65536)
        except Exception:
            chunk = b''
        if not chunk:
            continue
        buf = rxbuf + chunk
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            if not line.strip():
                continue
            try:
                e = json.loads(line.decode('utf-8'))
            except Exception:
                continue
            if e.get('ev') == 'ack':
                sq = e.get('seq_acked')
                if sq in pending:
                    rtts.append((time.time() - pending.pop(sq)) * 1000.0)
            elif e.get('ev') == 'data':
                arrivals.append(time.time())
                s = e.get('seq')
                if s is not None:
                    if last_seq is not None and (s & 0xFFFF) != ((last_seq + 1) & 0xFFFF):
                        gaps += 1
                    last_seq = s
                    seqs.append(s)
        rxbuf = buf

    snap1 = gw_snapshot(dev)
    window = t_end - t0
    expected = window * 1000.0 / period_ms
    diffs = [arrivals[i] - arrivals[i - 1] for i in range(1, len(arrivals))]
    jitter = [d - period_ms / 1000.0 for d in diffs]
    return {
        'period_ms': period_ms, 'window_s': round(window, 1),
        'expected_frames': int(expected), 'received_frames': len(arrivals),
        'arrival_pct': round(100.0 * len(arrivals) / expected, 2) if expected else None,
        'interval_p50_s': pct(diffs, 0.5), 'interval_p95_s': pct(diffs, 0.95),
        'jitter_p50_ms': round(pct(jitter, 0.5), 1) if jitter else None,
        'jitter_p95_ms': round(pct(jitter, 0.95), 1) if jitter else None,
        'gw_gaps_delta': snap1['gaps'] - snap0['gaps'],
        'gw_rx_delta': snap1['rx'] - snap0['rx'],
        'gw_crc_bad_delta': snap1['crc_bad'] - snap0['crc_bad'],
        'probe_sent': len(pending) + len(rtts) if rtts or pending else 0,
        'rtt_n': len(rtts), 'rtt_p50_ms': pct(rtts, 0.5),
        'rtt_p95_ms': pct(rtts, 0.95),
        'rtt_max_ms': round(max(rtts), 1) if rtts else None,
    }


def md_table(rows):
    out = ['| 上报周期 | 期望帧 | 实收帧 | 到达率 | 间隔p50 | 间隔p95 | 抖动p50 | 抖动p95 | 网关缺口 | 丢包率 | RTT p50 | RTT p95 | RTT max |',
           '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for r in rows:
        loss = None
        base = r['gw_rx_delta'] + r['gw_gaps_delta']
        if base:
            loss = round(100.0 * r['gw_gaps_delta'] / base, 2)
        out.append(f"| {r['period_ms']} ms | {r['expected_frames']} | {r['received_frames']} | "
                   f"{r['arrival_pct']}% | {r['interval_p50_s']}s | {r['interval_p95_s']}s | "
                   f"{r['jitter_p50_ms']}ms | {r['jitter_p95_ms']}ms | {r['gw_gaps_delta']} | "
                   f"{loss if loss is not None else '-'}% | {r['rtt_p50_ms']}ms | "
                   f"{r['rtt_p95_ms']}ms | {r['rtt_max_ms']}ms |")
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser(description='ESP-NOW 上报周期扫描（T4）')
    ap.add_argument('--dev', required=True)
    ap.add_argument('--minutes', type=int, default=10, help='每个周期采集分钟数')
    ap.add_argument('--periods', default='1000,5000,30000')
    ap.add_argument('--probe-every', type=float, default=30.0)
    args = ap.parse_args()

    dev = args.dev.upper()
    periods = [int(x) for x in args.periods.split(',') if x.strip()]
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')

    ev = socket.create_connection(CTRL, timeout=5)
    ev.sendall(b'{"op":"subscribe"}\n')
    ev.setblocking(False)

    login()
    rows = []
    pseq = [int(time.time()) % 100000]
    try:
        for p in periods:
            print(f'== 周期 {p} ms（{args.minutes} min）==')
            r = run_period(ev, dev, p, args.minutes, args.probe_every, pseq)
            rows.append(r)
            print('  ' + json.dumps(r, ensure_ascii=False))
    finally:
        ctrl({'op': 'send', 'dev': dev, 'seq': 1,
              'text': json.dumps({'type': 'config', 'send_interval': 5000})})
        write_server_period(dev, 5000)
        print('  已恢复 send_interval=5000 ms（含服务器库）')
        ev.close()

    md = md_table(rows)
    print('\n================ 汇总 ================')
    print(md)
    with open(f'{OUT_DIR}/period_sweep_{ts}.md', 'w', encoding='utf-8') as f:
        f.write(f'# ESP-NOW 上报周期扫描（T4） {ts}\n\n设备 {dev}，每周期 {args.minutes} 分钟\n\n{md}\n')
    with open(f'{OUT_DIR}/period_sweep_{ts}.json', 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f'结果已存: {OUT_DIR}/period_sweep_{ts}.md / .json')


if __name__ == '__main__':
    main()

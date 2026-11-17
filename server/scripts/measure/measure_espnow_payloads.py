#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP-NOW 上行载荷扫描（T5）— 在 Orange Pi 上运行
===============================================
通过 `payload_pad`（0~80 字节）放大节点上行 JSON，逐档采集，统计：
  - 上行到达率（实收 DATA / 理论帧数）
  - 平均空口帧长（由网关 USB 字节增量反推：空口帧 = USB 帧 - 6）
  - 序号缺口 / 丢包率（网关 gaps 增量）
  - 下行 RTT 与 ACK 率（每 probe_every 秒发一条无害 probe 命令）

配置双写：空口下行 `{"type":"config","payload_pad":N}`（秒级生效）
        ＋ 服务器 `/api/esp32/config/update`（防止 10min 维护窗口 fetchConfig 覆盖）

用法：
  python3 measure_espnow_payloads.py --dev 04B247F9EA08 --minutes 8
  python3 measure_espnow_payloads.py --dev 04B247F9EA08 --minutes 2 --pads 0,80   # 烟测
结果：/home/ubuntu/ab_results/v3/payload_sweep_<ts>.{md,json}
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
        st = d.get('stats') or {}
        return {'gaps': int(n.get('gaps', 0)), 'rx': int(n.get('rx', 0)),
                'usb_rx': int(st.get('usb_rx_bytes', 0)),
                'frames': int(st.get('usb_frames', 0)),
                'data_events': int(st.get('data_events', 0)),
                'crc_bad': int(st.get('crc_bad', 0))}
    except Exception:
        return {'gaps': 0, 'rx': 0, 'usb_rx': 0, 'frames': 0, 'data_events': 0, 'crc_bad': 0}


def pct(v, p):
    if not v:
        return None
    v = sorted(v)
    return round(v[min(len(v) - 1, int(len(v) * p))], 2)


def set_pad(dev, pad):
    ctrl({'op': 'send', 'dev': dev, 'seq': 1,
          'text': json.dumps({'type': 'config', 'payload_pad': pad})})
    try:
        api('POST', '/api/esp32/config/update', {'device_id': dev, 'payload_pad': int(pad)})
    except Exception as e:
        print('  [api] 写库失败:', e)


def run_pad(ev, dev, pad, minutes, interval_ms, probe_every, pseq):
    set_pad(dev, pad)
    print(f'  [config] payload_pad -> {pad}B（已同步服务器库），稳定 6s 后开始采集')
    time.sleep(6)

    snap0 = gw_snapshot(dev)
    t0 = time.time()
    t_end = t0 + minutes * 60
    arrivals, rtts = [], []
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
        rxbuf = buf

    snap1 = gw_snapshot(dev)
    window = t_end - t0
    expected = window * 1000.0 / interval_ms
    d_usb = snap1['usb_rx'] - snap0['usb_rx']
    d_frames = snap1['frames'] - snap0['frames']
    d_data = snap1['data_events'] - snap0['data_events']
    # USB 帧 = 4 字节头 + type/len(3) + payload + 2 字节 CRC = payload + 6 ⇒ 空口帧 = USB 帧长 - 6
    air_frame = round(d_usb / d_frames - 6, 1) if d_frames else None
    diffs = [arrivals[i] - arrivals[i - 1] for i in range(1, len(arrivals))]
    return {
        'payload_pad': pad, 'window_s': round(window, 1),
        'expected_frames': int(expected), 'received_frames': len(arrivals),
        'arrival_pct': round(100.0 * len(arrivals) / expected, 2) if expected else None,
        'air_frame_bytes_avg': air_frame,
        'interval_p50_s': pct(diffs, 0.5), 'interval_p95_s': pct(diffs, 0.95),
        'gw_gaps_delta': gaps, 'gw_crc_bad_delta': snap1['crc_bad'] - snap0['crc_bad'],
        'loss_pct': round(100.0 * gaps / max(1, gaps + (snap1['rx'] - snap0['rx'])), 2),
        'rtt_n': len(rtts), 'rtt_p50_ms': pct(rtts, 0.5), 'rtt_p95_ms': pct(rtts, 0.95),
        'probe_sent': len(rtts) + len(pending),
    }


def md_table(rows):
    out = ['| payload_pad | 实测空口帧长 | 期望帧 | 实收 | 到达率 | 间隔p50 | 间隔p95 | 缺口 | 丢包率 | RTT p50 | RTT p95 | ACK率 |',
           '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for r in rows:
        ack_rate = None
        if r['probe_sent']:
            ack_rate = round(100.0 * r['rtt_n'] / r['probe_sent'], 1)
        out.append(f"| +{r['payload_pad']}B | {r['air_frame_bytes_avg']}B | {r['expected_frames']} | "
                   f"{r['received_frames']} | {r['arrival_pct']}% | {r['interval_p50_s']}s | "
                   f"{r['interval_p95_s']}s | {r['gw_gaps_delta']} | {r['loss_pct']}% | "
                   f"{r['rtt_p50_ms']}ms | {r['rtt_p95_ms']}ms | {ack_rate}% |")
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser(description='ESP-NOW 上行载荷扫描（T5）')
    ap.add_argument('--dev', required=True)
    ap.add_argument('--minutes', type=int, default=8, help='每档采集分钟数')
    ap.add_argument('--pads', default='0,40,80')
    ap.add_argument('--interval-ms', type=int, default=5000, help='上报周期（保持默认 5s）')
    ap.add_argument('--probe-every', type=float, default=30.0)
    args = ap.parse_args()

    dev = args.dev.upper()
    pads = [int(x) for x in args.pads.split(',') if x.strip()]
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')

    login()
    ev = socket.create_connection(CTRL, timeout=5)
    ev.sendall(b'{"op":"subscribe"}\n')
    ev.setblocking(False)

    rows = []
    pseq = [int(time.time()) % 100000]
    try:
        for pad in pads:
            print(f'== payload_pad {pad}B（{args.minutes} min）==')
            r = run_pad(ev, dev, pad, args.minutes, args.interval_ms, args.probe_every, pseq)
            rows.append(r)
            print('  ' + json.dumps(r, ensure_ascii=False))
    finally:
        set_pad(dev, 0)
        print('  已恢复 payload_pad=0（含服务器库）')
        ev.close()

    md = md_table(rows)
    print('\n================ 汇总 ================')
    print(md)
    with open(f'{OUT_DIR}/payload_sweep_{ts}.md', 'w', encoding='utf-8') as f:
        f.write(f'# ESP-NOW 上行载荷扫描（T5） {ts}\n\n设备 {dev}，'
                f'上报周期 {args.interval_ms}ms，每档 {args.minutes} 分钟\n\n{md}\n')
    with open(f'{OUT_DIR}/payload_sweep_{ts}.json', 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f'结果已存: {OUT_DIR}/payload_sweep_{ts}.md / .json')


if __name__ == '__main__':
    main()

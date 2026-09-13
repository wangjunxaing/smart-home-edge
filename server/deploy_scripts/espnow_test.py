#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP-NOW 稳定性/时延测试驱动（阶段二 5.4）— 在 Orange Pi 上运行
==============================================================
通过 usb_gateway.py 控制口（127.0.0.1:8765）下发命令并订阅事件，
统计「下行」时延/丢包与「上行」到达间隔/序号缺口/RSSI。

典型用法：
  # 1) 下行 RTT + 丢包（发 N 条，间隔 interval 秒）
  python3 espnow_test.py downlink --dev A1B2C3D4E5F6 --count 100 --interval 1

  # 2) 上行稳定性观测（只订阅，不发送；统计 DATA 事件到达率/间隔抖动/丢包）
  python3 espnow_test.py uplink --duration 600

  # 3) 长稳测试（默认 2 小时，含下行每 30s 一条 + 上行统计）
  python3 espnow_test.py soak --hours 2

  # 4) 干扰场景（与 WiFi/MQTT 并发）时由外部同时跑 measure_mqtt_espnow.py，再执行 soak

输出：控制台摘要 + JSON（/home/ubuntu/ab_results/espnow_<mode>_<ts>.json）
"""
import argparse
import json
import os
import socket
import statistics
import sys
import time

CTRL = ('127.0.0.1', 8765)
OUT_DIR = '/home/ubuntu/ab_results'


def ctrl_send(req: dict):
    s = socket.create_connection(CTRL, timeout=5)
    s.sendall((json.dumps(req) + '\n').encode())
    data = b''
    s.settimeout(5)
    try:
        while b'\n' not in data:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
    except Exception:
        pass
    s.close()
    try:
        return json.loads(data.split(b'\n', 1)[0].decode('utf-8'))
    except Exception:
        return {}


class EventStream:
    """裸 socket + 缓冲分行（避免 makefile+timeout 的失效问题）"""
    def __init__(self):
        self.s = socket.create_connection(CTRL, timeout=5)
        self.s.sendall((json.dumps({'op': 'subscribe'}) + '\n').encode())
        self.s.settimeout(0.02)          # 轮询粒度 20ms（0.2s 会污染 RTT 统计）
        self.buf = b''

    def poll(self):
        out = []
        try:
            chunk = self.s.recv(8192)
        except socket.timeout:
            return out
        except Exception:
            return out
        if not chunk:
            return out
        self.buf += chunk
        while b'\n' in self.buf:
            line, self.buf = self.buf.split(b'\n', 1)
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line.decode('utf-8')))
            except Exception:
                pass
        return out

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def pct(v, p):
    if not v:
        return None
    v = sorted(v)
    return round(v[min(len(v) - 1, int(len(v) * p))], 2)


def summarize(name, lat, sent, acked, uplink, rssi, gaps):
    out = {'mode': name, 'downlink_sent': sent, 'downlink_acked': acked,
           'downlink_loss_pct': round(100.0 * (sent - acked) / sent, 2) if sent else None,
           'rtt_ms': {'n': len(lat), 'min': min(lat) if lat else None,
                      'p50': pct(lat, 0.5), 'p95': pct(lat, 0.95),
                      'max': max(lat) if lat else None} if lat else None,
           'uplink': uplink, 'rssi': {'n': len(rssi), 'min': min(rssi) if rssi else None,
                                      'avg': round(sum(rssi) / len(rssi), 1) if rssi else None,
                                      'max': max(rssi) if rssi else None},
           'seq_gaps': gaps}
    print('\n================ 结果 ================')
    print(json.dumps(out, ensure_ascii=False, indent=2))
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = f'{OUT_DIR}/espnow_{name}_{time.strftime("%Y%m%d_%H%M%S")}.json'
    except OSError:                     # 本地（非 Pi）测试时退到临时目录
        import tempfile
        path = os.path.join(tempfile.gettempdir(),
                            f'espnow_{name}_{time.strftime("%Y%m%d_%H%M%S")}.json')
    with open(path, 'w', encoding='utf-8') as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)
    print('结果已存:', path)
    return out


def run(duration, send_count, interval, dev, do_send=True):
    ev = EventStream()
    pending = {}          # seq -> t_send
    lat, rssi = [], []
    sent = acked = 0
    gaps = 0
    last_seq = None
    up_arrivals = []
    t_end = time.time() + duration
    next_send = time.time()
    seq = 0
    while time.time() < t_end:
        now = time.time()
        if do_send and sent < send_count and now >= next_send:
            seq += 1
            ctrl_send({'op': 'send', 'dev': dev, 'text': f'test-{seq}', 'seq': seq})
            pending[seq] = now
            sent += 1
            next_send = now + interval
        for e in ev.poll():
            if e.get('ev') == 'ack':
                sq = e.get('seq_acked')
                if sq in pending:
                    lat.append((time.time() - pending.pop(sq)) * 1000.0)
                    acked += 1
                if e.get('rssi') is not None:
                    rssi.append(e['rssi'])
            elif e.get('ev') == 'data':
                up_arrivals.append(time.time())
                if last_seq is not None and (e.get('seq', 0) & 0xFFFF) != ((last_seq + 1) & 0xFFFF):
                    gaps += 1
                last_seq = e.get('seq', 0)
    up = None
    if len(up_arrivals) > 2:
        d = [up_arrivals[i] - up_arrivals[i - 1] for i in range(1, len(up_arrivals))]
        up = {'n': len(up_arrivals), 'rate_per_min': round(60.0 / (sum(d) / len(d)), 2),
              'interval_p50_s': pct(d, 0.5), 'interval_p95_s': pct(d, 0.95)}
    ev.close()
    return summarize('downlink' if do_send else 'uplink', lat, sent, acked, up, rssi, gaps)


def main():
    ap = argparse.ArgumentParser(description='ESP-NOW 稳定性/时延测试（5.4）')
    sub = ap.add_subparsers(dest='mode', required=True)
    p1 = sub.add_parser('downlink')
    p1.add_argument('--dev', required=True)
    p1.add_argument('--count', type=int, default=100)
    p1.add_argument('--interval', type=float, default=1.0)

    p2 = sub.add_parser('uplink')
    p2.add_argument('--duration', type=int, default=600)

    p3 = sub.add_parser('soak')
    p3.add_argument('--hours', type=float, default=2.0)
    p3.add_argument('--dev', default='')
    p3.add_argument('--send-every', type=float, default=30.0)

    args = ap.parse_args()
    if args.mode == 'downlink':
        run(args.count * args.interval + 10, args.count, args.interval, args.dev.upper())
    elif args.mode == 'uplink':
        run(args.duration, 0, 1.0, '', do_send=False)
    else:
        dur = args.hours * 3600
        cnt = int(dur / args.send_every) if args.dev else 0
        run(dur, cnt, args.send_every, args.dev.upper(), do_send=bool(args.dev))


if __name__ == '__main__':
    main()

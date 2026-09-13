#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP-NOW 多节点公平性统计（T8）— 在 Orange Pi 上运行
==================================================
同时观察多个 ESP-NOW 节点经同一网关上报的到达率/丢包/静默情况，
用于评估多节点并发时的公平性（ESP-NOW 无 CSMA 退避，TDMA 缺失时容易互相压制）。

数据源：网关落盘统计 `/home/ubuntu/ab_results/espnow_gateway_stats.json`
（usb_gateway 每 10s 刷新一次，含各节点 rx / gaps / rssi / last_ms）

用法：
  python3 measure_espnow_multinode.py --devs A1B2C3D4E5F6,B2C3D4E5F6A7 --minutes 20
结果：/home/ubuntu/ab_results/v3/multinode_<ts>.{md,json}
"""
import argparse
import json
import os
import time

GW_STATS = '/home/ubuntu/ab_results/espnow_gateway_stats.json'
OUT_DIR = '/home/ubuntu/ab_results/v3'


def read_stats():
    try:
        with open(GW_STATS, 'r', encoding='utf-8') as f:
            d = json.load(f)
        nodes = d.get('nodes') or {}
        return {k.upper(): {'rx': int(v.get('rx', 0)), 'gaps': int(v.get('gaps', 0)),
                            'rssi': v.get('rssi'), 'last_ms': v.get('last_ms', 0)}
                for k, v in nodes.items()}, d.get('stats') or {}
    except Exception:
        return {}, {}


def main():
    ap = argparse.ArgumentParser(description='ESP-NOW 多节点公平性统计（T8）')
    ap.add_argument('--devs', required=True, help='逗号分隔的设备 ID')
    ap.add_argument('--minutes', type=int, default=20)
    ap.add_argument('--interval-ms', type=int, default=5000, help='各节点上报周期')
    args = ap.parse_args()

    devs = [d.strip().upper() for d in args.devs.split(',') if d.strip()]
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')

    s0, _ = read_stats()
    t0 = time.time()
    t_end = t0 + args.minutes * 60
    samples = []
    while time.time() < t_end:
        time.sleep(15)
        s, g = read_stats()
        samples.append((time.time() - t0, s, g))
        line = ' '.join(f"{d[:8]}:rx={s.get(d, {}).get('rx', 0) - s0.get(d, {}).get('rx', 0)}"
                        for d in devs)
        print(f'  t={int(time.time() - t0)}s {line}', flush=True)
    s1, g1 = read_stats()

    window = time.time() - t0
    expected = window * 1000.0 / args.interval_ms
    rows = []
    for d in devs:
        a, b = s0.get(d, {}), s1.get(d, {})
        rx = max(0, b.get('rx', 0) - a.get('rx', 0))
        gaps = max(0, b.get('gaps', 0) - a.get('gaps', 0))
        rows.append({
            'device': d, 'rx': rx, 'gaps': gaps,
            'expected_frames': int(expected),
            'arrival_pct': round(100.0 * rx / expected, 2) if expected else None,
            'loss_pct': round(100.0 * gaps / max(1, rx + gaps), 2),
            'rssi': b.get('rssi'),
        })

    arrivals = [r['arrival_pct'] for r in rows if r['arrival_pct'] is not None]
    spread = round(max(arrivals) - min(arrivals), 2) if len(arrivals) > 1 else None

    md = ['| 节点 | 期望帧 | 实收 | 到达率 | 缺口 | 丢包率 | RSSI |',
          '|---|---|---|---|---|---|---|']
    for r in rows:
        md.append(f"| {r['device']} | {r['expected_frames']} | {r['rx']} | "
                  f"{r['arrival_pct']}% | {r['gaps']} | {r['loss_pct']}% | {r['rssi']} |")
    md.append('')
    md.append(f'> 窗口 {round(window, 1)}s，各节点上报周期 {args.interval_ms}ms；'
              f'节点间到达率极差 {spread}%（公平性指标，越小越好）')
    md.append('> 口径：到达率＝网关在该窗口内收到该节点的 DATA 帧数 ÷ 理论帧数；'
              '缺口＝网关序号不连续次数（空口丢包）')
    text = '\n'.join(md)
    print('\n================ T8 结果 ================')
    print(text)
    with open(f'{OUT_DIR}/multinode_{ts}.md', 'w', encoding='utf-8') as f:
        f.write(f'# ESP-NOW 多节点公平性（T8） {ts}\n\n{text}\n')
    with open(f'{OUT_DIR}/multinode_{ts}.json', 'w', encoding='utf-8') as f:
        json.dump({'rows': rows, 'spread_pct': spread, 'window_s': round(window, 1)},
                  f, ensure_ascii=False, indent=2)
    print(f'结果已存: {OUT_DIR}/multinode_{ts}.md / .json')


if __name__ == '__main__':
    main()

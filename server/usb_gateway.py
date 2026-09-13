#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USB Dongle 网关守护（阶段二 5.4）— 在 Orange Pi 上运行
======================================================
职责：
  1) 读 /dev/ttyACM0（ESP32-S3 网关 USB CDC）解析 USB 帧
  2) DATA/HELLO 事件 → 复用现有传感器接口 POST /api/esp32/sensors（Flask/DB/前端/自动化零改动）
  3) 统计各节点收包/RSSI/静默时间 → 状态写 JSON 文件（稳定性测试数据源）
  4) 提供本地控制口（127.0.0.1:8765，JSON 行协议）供测试脚本下发命令与订阅事件

控制口命令（JSON 行）：
  {"op":"send","dev":"<12hex>","text":"...","seq":123}   下发 CMD 到节点
  {"op":"subscribe"}                                     订阅事件流（DATA/ACK/STATUS）
  {"op":"stats"}                                         立即返回统计
事件推送（订阅后逐行）：
  {"ev":"data","dev":..,"seq":..,"payload":{...},"rssi":..}
  {"ev":"ack","dev":..,"seq_acked":..,"rssi":..,"status":..}
  {"ev":"status","raw":{...}}

本地无硬件自测：--simulate 会伪造 DATA/ACK 事件（用于验证测试脚本与统计口径）
"""
import argparse
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'deploy_scripts'))
from espnow_proto import (air_parse, ack_parse, cmd_build, usb_build, usb_parse_iter,
                          U_DATA_EVENT, U_ACK_EVENT, U_STATUS, U_HELLO_EVENT, T_ACK,
                          T_DATA, air_build)

API = 'http://127.0.0.1:8080/api/esp32/sensors'
CTRL_HOST, CTRL_PORT = '127.0.0.1', 8765
STATS_PATH = '/home/ubuntu/ab_results/espnow_gateway_stats.json'


class Gateway:
    def __init__(self, args):
        self.args = args
        self.nodes = {}          # dev -> {rx, rssi, last_ms, seq_last, gaps}
        self.stats = {'usb_frames': 0, 'data_events': 0, 'ack_events': 0,
                      'status_events': 0, 'crc_bad': 0, 'posted': 0, 'post_fail': 0,
                      'cmd_sent': 0, 'cmd_ack': 0,
                      'usb_rx_bytes': 0, 'usb_tx_bytes': 0,   # 流量对比用（ESP-NOW 走 USB，非 IP）
                      'started_at': time.time()}
        self.subs = []           # 订阅者 socket 列表
        self.lock = threading.Lock()
        self.ser = None

    # ---------- 上报到 Flask ----------
    def post_sensor(self, dev, payload: dict):
        if self.args.no_post:
            self.stats['posted'] += 1
            return
        body = dict(payload)
        body.setdefault('id', dev)
        body.setdefault('location', self.args.default_location)
        try:
            req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
            self.stats['posted'] += 1
        except Exception as e:
            self.stats['post_fail'] += 1
            if self.stats['post_fail'] % 20 == 1:
                print(f'[GW] POST 失败: {e}', flush=True)

    # ---------- 事件广播 ----------
    def publish(self, ev: dict):
        dead = []
        line = (json.dumps(ev, ensure_ascii=False) + '\n').encode()
        for s in list(self.subs):
            try:
                s.sendall(line)
            except Exception:
                dead.append(s)
        for d in dead:
            try:
                self.subs.remove(d)
            except ValueError:
                pass

    # ---------- 设备事件处理 ----------
    def on_air(self, raw: bytes):
        f = air_parse(raw)
        if f is None:
            self.stats['crc_bad'] += 1
            return
        dev = f['dev']
        n = self.nodes.setdefault(dev, {'rx': 0, 'rssi': 0, 'last_ms': 0,
                                        'seq_last': None, 'gaps': 0, 'first_seen': time.time()})
        now_ms = int(time.time() * 1000)
        n['rx'] += 1
        n['last_ms'] = now_ms
        if f['type'] == T_DATA:
            self.stats['data_events'] += 1
            # 序号缺口统计（上行丢包率）
            if n['seq_last'] is not None and (f['seq'] & 0xFFFF) != ((n['seq_last'] + 1) & 0xFFFF):
                n['gaps'] += 1
            n['seq_last'] = f['seq']
            try:
                payload = json.loads(f['payload'].decode('utf-8'))
            except Exception:
                payload = {}
            self.post_sensor(dev, payload)
            self.publish({'ev': 'data', 'dev': dev, 'seq': f['seq'], 'payload': payload})
        elif f['type'] == T_ACK:
            self.stats['ack_events'] += 1
            self.stats['cmd_ack'] += 1          # 节点对 CMD 的 ACK 回执数（与 cmd_sent 配对）
            a = ack_parse(f['payload'])
            if a:
                n['rssi'] = a['rssi']
                self.publish({'ev': 'ack', 'dev': dev, **a})

    def on_usb(self, fr: dict):
        self.stats['usb_frames'] += 1
        t, pl = fr['type'], fr['payload']
        if t in (U_DATA_EVENT, U_HELLO_EVENT, U_ACK_EVENT):
            self.on_air(pl)
        elif t == U_STATUS:
            self.stats['status_events'] += 1
            try:
                raw = json.loads(pl.decode('utf-8'))
            except Exception:
                raw = {}
            # 网关测的 RSSI 才是「节点→网关」链路真实信号强度，回填到节点统计
            for item in (raw.get('nodes') or []):
                dev = (item.get('id') or '').upper()
                if not dev:
                    continue
                nd = self.nodes.setdefault(dev, {'rx': 0, 'rssi': 0, 'last_ms': 0,
                                                 'seq_last': None, 'gaps': 0,
                                                 'first_seen': time.time()})
                if item.get('rssi'):
                    nd['rssi'] = item['rssi']
            self.publish({'ev': 'status', 'raw': raw})

    # ---------- 串口主循环 ----------
    def serial_loop(self):
        import serial
        buf = b''
        ser = serial.Serial(self.args.port, self.args.baud, timeout=0.2)
        self.ser = ser
        print(f'[GW] 串口已打开 {self.args.port} @ {self.args.baud}', flush=True)
        while True:
            try:
                # 有数据就立即返回（read(4096) 会等满 timeout，实测引入 ~200ms 批延迟）
                chunk = ser.read(ser.in_waiting or 1)
            except Exception as e:
                print(f'[GW] 串口异常: {e}，1s 后重试', flush=True)
                time.sleep(1)
                try:
                    ser.close(); ser = serial.Serial(self.args.port, self.args.baud, timeout=0.2)
                    self.ser = ser
                except Exception:
                    pass
                continue
            if not chunk:
                continue
            self.stats['usb_rx_bytes'] += len(chunk)
            buf += chunk
            frames, buf = usb_parse_iter(buf)
            for fr in frames:
                self.on_usb(fr)

    # ---------- 模拟模式（本地无硬件） ----------
    def simulate_loop(self):
        print('[GW] 模拟模式：伪造节点 DATA/ACK 事件（5s 周期）', flush=True)
        dev = 'A1B2C3D4E5F6'
        seq = 0
        while True:
            seq += 1
            payload = json.dumps({'id': dev, 'lux': 100 + seq % 10, 'temperature': 26.5,
                                  'humidity': 55.0, 'uptime_ms': seq * 5000}).encode()
            self.on_air(air_build(T_DATA, dev, seq, payload))
            if seq % 7 == 0:      # 模拟 1/7 丢包（不发 ACK）
                pass
            else:
                self.on_air(air_build(T_ACK, dev, seq, struct.pack('<HbB', seq, -55, 0)))
            time.sleep(5)

    # ---------- 控制口 ----------
    def control_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((CTRL_HOST, CTRL_PORT))
        srv.listen(8)
        print(f'[GW] 控制口 {CTRL_HOST}:{CTRL_PORT}', flush=True)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self.handle_ctrl, args=(conn,), daemon=True).start()

    def handle_ctrl(self, conn):
        f = conn.makefile('rwb')
        for line in f:
            try:
                req = json.loads(line.decode('utf-8'))
            except Exception:
                continue
            op = req.get('op')
            if op == 'send':
                dev = (req.get('dev') or '').upper()
                seq = int(req.get('seq') or 1)
                frame = cmd_build(dev, seq, req.get('text') or '')
                usb = usb_build(0x20, frame)
                if self.ser is not None:
                    self.ser.write(usb)
                    self.stats['usb_tx_bytes'] += len(usb)
                self.stats['cmd_sent'] += 1
                if self.args.simulate:      # 本地自测：模拟节点回 ACK
                    def _ack(d=dev, s=seq):
                        time.sleep(0.03)
                        self.publish({'ev': 'ack', 'dev': d, 'seq_acked': s,
                                      'rssi': -55, 'status': 0})
                    threading.Thread(target=_ack, daemon=True).start()
                f.write((json.dumps({'ok': True, 'sent': len(usb)}, ensure_ascii=False) + '\n').encode())
                f.flush()
            elif op == 'subscribe':
                self.subs.append(conn)
                f.write((json.dumps({'ok': True, 'sub': True}) + '\n').encode())
                f.flush()
                return                      # 该连接转为事件流
            elif op == 'stats':
                f.write((json.dumps({'ok': True, 'stats': self.stats, 'nodes': self.nodes},
                                    ensure_ascii=False) + '\n').encode())
                f.flush()
            elif op == 'quit':
                return
        try:
            conn.close()
        except Exception:
            pass

    def stats_dump_loop(self):
        while True:
            time.sleep(10)
            try:
                os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
                with self.lock:
                    data = {'ts': time.time(), 'stats': self.stats, 'nodes': self.nodes}
                with open(STATS_PATH, 'w', encoding='utf-8') as fp:
                    json.dump(data, fp, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def run(self):
        threading.Thread(target=self.control_loop, daemon=True).start()
        threading.Thread(target=self.stats_dump_loop, daemon=True).start()
        if self.args.simulate:
            self.simulate_loop()
        else:
            self.serial_loop()


def main():
    ap = argparse.ArgumentParser(description='ESP-NOW USB 网关守护（5.4）')
    ap.add_argument('--port', default='/dev/ttyACM0')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--default-location', default='未分类')
    ap.add_argument('--simulate', action='store_true', help='本地无硬件自测（伪造事件）')
    ap.add_argument('--no-post', action='store_true', help='不向 Flask 上报（本地测试用）')
    args = ap.parse_args()
    Gateway(args).run()


if __name__ == '__main__':
    main()

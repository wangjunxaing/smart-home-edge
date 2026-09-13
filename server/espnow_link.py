# -*- coding: utf-8 -*-
"""
ESP-NOW 链路模块（5.4，服务器侧）
================================
职责：
  1) 通过 usb_gateway.py 控制口（127.0.0.1:8765）把下行命令发给网关 → ESP-NOW → 节点
  2) 后台订阅网关事件流，统计"下发→节点 ACK"端到端时延并打印
     [ESPNOW-ACK] <dev8> seq=N latency_ms=..
     （与 [MQTT-ACK] 同格式，测量脚本共用一套解析）
  3) 缓存网关 STATUS（各节点 rx/RSSI/静默时长 + USB 字节计数）供接口/测试查询

发送失败（网关不可达/未连接）返回 False，调用方可回退或记日志。
"""
import json
import socket
import threading
import time

import link_state          # 链路状态：下行通道推断 + 回执确认（无业务依赖，避免循环导入）

CTRL_HOST, CTRL_PORT = '127.0.0.1', 8765

_lock = threading.Lock()
_pending = {}          # (dev, seq) -> send_ms
_PENDING_MAX = 256
_gateway_status = {}   # 最近一次 STATUS
_running = False
_started = False
_stats = {'sent': 0, 'send_fail': 0, 'ack': 0, 'ack_timeout': 0, 'up': 0}


def _now_ms():
    return int(time.time() * 1000)


def _ctrl(req, timeout=3, read_reply=True):
    s = socket.create_connection((CTRL_HOST, CTRL_PORT), timeout=timeout)
    try:
        s.sendall((json.dumps(req, ensure_ascii=False) + '\n').encode())
        if not read_reply:
            return None
        s.settimeout(timeout)
        data = b''
        while b'\n' not in data:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        return json.loads(data.split(b'\n', 1)[0].decode('utf-8'))
    finally:
        s.close()


def send_cmd(device_id, payload):
    """下发命令给 ESP-NOW 设备（payload 为 dict，需含 type/seq）"""
    seq = payload.get('seq')
    try:
        _ctrl({'op': 'send', 'dev': device_id, 'seq': seq,
               'text': json.dumps(payload, ensure_ascii=False)})
    except Exception as e:
        _stats['send_fail'] += 1
        print(f"[ESPNOW] 下发失败（网关不可达？）: {e}")
        return False
    _stats['sent'] += 1
    if seq is not None:
        with _lock:
            _pending[(device_id, seq)] = _now_ms()
            while len(_pending) > _PENDING_MAX:
                _pending.pop(next(iter(_pending)), None)
    return True


def get_gateway_status():
    with _lock:
        return dict(_gateway_status)


def get_stats():
    with _lock:
        return dict(_stats)


def _event_loop():
    """订阅网关事件流：计算 ACK 时延 + 缓存 STATUS（断线自动重连）"""
    while _running:
        try:
            s = socket.create_connection((CTRL_HOST, CTRL_PORT), timeout=5)
            s.sendall((json.dumps({'op': 'subscribe'}) + '\n').encode())
            s.settimeout(1.0)
            buf = b''
            print('[ESPNOW] 已订阅网关事件流')
            while _running:
                try:
                    chunk = s.recv(8192)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line.decode('utf-8'))
                    except Exception:
                        continue
                    _handle_event(ev)
            s.close()
        except Exception:
            pass
        time.sleep(3)


def _handle_event(ev):
    global _gateway_status
    kind = ev.get('ev')
    if kind == 'ack':
        dev = ev.get('dev', '')
        seq = ev.get('seq_acked')
        link_state.notify_ack(dev, seq)        # 通知下行可靠性模块（清除待回执）
        with _lock:
            sent = _pending.pop((dev, seq), None)
        if sent is not None:
            _stats['ack'] += 1
            print(f"[ESPNOW-ACK] {dev[:8]} seq={seq} latency_ms={_now_ms() - sent} "
                  f"rssi={ev.get('rssi')}", flush=True)
        else:
            _stats['ack'] += 1
            print(f"[ESPNOW-ACK] {dev[:8]} seq={seq} latency_ms=-1 rssi={ev.get('rssi')}",
                  flush=True)
    elif kind == 'data':
        _stats['up'] += 1
        link_state.mark_up(ev.get('dev', ''), 'espnow')   # 供下行通道选择
        payload = ev.get('payload') or {}
        if payload.get('t0_ms') is not None:
            cur = payload.get('current_ma')
            cur_s = f' cur={cur}' if cur is not None else ''
            print(f"[ESPNOW-UP] {ev.get('dev', '')[:8]} t0={payload.get('t0_ms')} "
                  f"recv_ms={_now_ms()}{cur_s}", flush=True)
    elif kind == 'status':
        with _lock:
            _gateway_status = ev.get('raw') or {}


def start_link():
    """启动事件订阅线程（app 启动时调用，幂等）"""
    global _running, _started
    if _started:
        return
    _started = True
    _running = True
    threading.Thread(target=_event_loop, daemon=True).start()
    print('[ESPNOW] 链路模块已启动（控制口 127.0.0.1:8765）')

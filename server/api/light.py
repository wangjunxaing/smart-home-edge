# -*- coding: utf-8 -*-
"""
灯带控制模块 — Blueprint
========================
WS2812 灯带的"期望状态"托管：网页下发 → ESP32 每 2 秒轮询 → 按 seq 序号应用。
（短期轮询方案，阶段二迁移 MQTT 后改为即时推送）

状态持久化: server/data/light_state.json
  {device_id: {seq, on, r, g, b, brightness, updated_at}}
"""
import os
import json
import time
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify

from core.auth import login_required, admin_required
from core.config import LIGHT_STATE
from link.downlink import send_downlink   # 统一下行路由（mqtt / espnow）

light_bp = Blueprint('light', __name__)

STATE_PATH = str(LIGHT_STATE)
_lock = threading.Lock()
_cache = None
_http_seen = {}     # device_id -> 已轮询到的最大 seq（HTTP 控制到达日志去重）

DEFAULT_STATE = {'seq': 0, 'on': False, 'r': 255, 'g': 255, 'b': 255,
                 'brightness': 128, 'led_count': 10}
MAX_LED_COUNT = 60     # 与固件 LED_STRIP_COUNT 一致（两串 30 串联）


def _load():
    global _cache
    if _cache is None:
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache


def _save():
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(_cache, f, ensure_ascii=False, indent=2)


# ==================== ESP32 设备端接口 ====================

@light_bp.route('/api/esp32/light/state', methods=['GET'])
def api_light_state_device():
    device_id = request.args.get('device_id', '')
    with _lock:
        state = dict(_load().get(device_id, DEFAULT_STATE))
    # 论文数据点：HTTP 控制到达时刻（设备轮询到新 seq 的第一时间）
    seq = state.get('seq', 0)
    if seq != _http_seen.get(device_id, -1):
        _http_seen[device_id] = seq
        print(f"[HTTP-CTRL] {device_id[:8]} seq={seq} t_ms={int(time.time()*1000)}")
    return jsonify(state)


# ==================== 管理端接口（需登录） ====================

def set_light_state(device_id, on=None, r=None, g=None, b=None, brightness=None,
                    led_count=None):
    """更新灯带期望状态（路由与 LLM 工具共用），返回更新后状态 dict"""
    if not device_id:
        return None
    with _lock:
        all_states = _load()
        cur = dict(all_states.get(device_id, DEFAULT_STATE))
        # 5.13：只在实际要改状态时才存"修改前快照"（供撤销），纯查询不留快照
        if any(v is not None for v in (on, r, g, b, brightness, led_count)):
            try:
                from core import audit
                audit.save_snapshot(device_id, 'light', dict(cur))
            except Exception as e:
                print(f'[灯带] 快照保存失败（不影响控制）: {e}')
        for key, val in (('r', r), ('g', g), ('b', b), ('brightness', brightness)):
            if val is not None:
                try:
                    cur[key] = max(0, min(255, int(val)))
                except (TypeError, ValueError):
                    pass
        if led_count is not None:
            try:
                cur['led_count'] = max(1, min(MAX_LED_COUNT, int(led_count)))
            except (TypeError, ValueError):
                pass
        if on is not None:
            cur['on'] = bool(on)
        cur['seq'] = int(cur.get('seq', 0)) + 1   # 每次修改递增，设备据此判断"有新状态"
        cur['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        all_states[device_id] = cur
        _save()
    # v1.9.0 统一下行路由：mqtt → broker；espnow → USB 网关 → ESP-NOW
    try:
        send_downlink(device_id, {
            'type': 'light', 'seq': cur['seq'],
            'on': cur['on'], 'r': cur['r'], 'g': cur['g'], 'b': cur['b'],
            'brightness': cur['brightness'], 'led_count': cur['led_count'],
        })
    except Exception as e:
        print(f"[灯带] 下行失败: {e}")
    return dict(cur)


@light_bp.route('/api/admin/light/set', methods=['POST'])
@admin_required
def api_light_set():
    data = request.get_json() or {}
    device_id = data.get('device_id', '')
    if not device_id:
        return jsonify({'status': 'error', 'message': '缺少设备ID'})

    cur = set_light_state(device_id, on=data.get('on'), r=data.get('r'),
                          g=data.get('g'), b=data.get('b'),
                          brightness=data.get('brightness'),
                          led_count=data.get('led_count'))
    if cur is None:
        return jsonify({'status': 'error', 'message': '缺少设备ID'})

    print(f"[灯带] {device_id[:8]} -> on={cur['on']} "
          f"rgb=({cur['r']},{cur['g']},{cur['b']}) 亮度{cur['brightness']} seq={cur['seq']}")
    return jsonify({'status': 'ok', 'seq': cur['seq']})


@light_bp.route('/api/admin/light/state', methods=['GET'])
@admin_required
def api_light_state_admin():
    device_id = request.args.get('device_id', '')
    with _lock:
        state = dict(_load().get(device_id, DEFAULT_STATE))
    return jsonify(state)
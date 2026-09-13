# -*- coding: utf-8 -*-
"""
红外遥控模块 — Blueprint
========================
NEC 码的学习、存储与下发。

设备轮询模型（与灯带相同，阶段二迁移 MQTT 后改即时推送）:
  ESP32 每 2 秒 GET /api/esp32/ir/pending → {seq: 待发射序号, learn: 学习模式, data: NEC 码}
  学习模式下 ESP32 收到遥控码 → POST /api/esp32/ir/report

状态持久化: server/ir_state.json
  devices: {device_id: {learn_until, send_seq, send_data, learned}}
  codes:   [{name, data, added_at}]   已命名的遥控码库
"""
import os
import json
import time
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify

from auth import login_required, admin_required
from downlink import send_downlink   # 统一下行路由（mqtt / espnow）

ir_bp = Blueprint('ir', __name__)

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ir_state.json')
LEARN_TIMEOUT_S = 60          # 学习模式 60 秒后自动关闭
_lock = threading.Lock()
_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
        _cache.setdefault('devices', {})
        _cache.setdefault('codes', [])
    return _cache


def _save():
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(_cache, f, ensure_ascii=False, indent=2)


def _dev(device_id):
    return _load()['devices'].setdefault(device_id, {
        'learn_until': 0, 'send_seq': 0, 'send_data': 0, 'learned': None
    })


# ==================== ESP32 设备端接口 ====================

@ir_bp.route('/api/esp32/ir/pending', methods=['GET'])
def api_ir_pending():
    device_id = request.args.get('device_id', '')
    with _lock:
        dev = _dev(device_id)
        return jsonify({
            'seq': dev['send_seq'],
            'learn': time.time() < dev.get('learn_until', 0),
            'data': dev['send_data']
        })


@ir_bp.route('/api/esp32/ir/report', methods=['POST'])
def api_ir_report():
    data = request.get_json() or {}
    device_id = data.get('id', '')
    code = data.get('data')
    if device_id and isinstance(code, int):
        with _lock:
            dev = _dev(device_id)
            if time.time() < dev.get('learn_until', 0):   # 仅学习窗口内接收
                dev['learned'] = {'data': code,
                                  'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
                _save()
                print(f"[红外] {device_id[:8]} 学习到 0x{code:08X}")
    return jsonify({'status': 'ok'})


# ==================== 管理端接口（需登录） ====================

@ir_bp.route('/api/admin/ir/learn', methods=['POST'])
@admin_required
def api_ir_learn():
    data = request.get_json() or {}
    device_id = data.get('device_id', '')
    if not device_id:
        return jsonify({'status': 'error', 'message': '缺少设备ID'})
    with _lock:
        dev = _dev(device_id)
        dev['learn_until'] = time.time() + LEARN_TIMEOUT_S
        dev['learned'] = None
        _save()
    return jsonify({'status': 'ok', 'seconds': LEARN_TIMEOUT_S})


@ir_bp.route('/api/admin/ir/learned', methods=['GET'])
@admin_required
def api_ir_learned():
    device_id = request.args.get('device_id', '')
    with _lock:
        dev = _dev(device_id)
        return jsonify({'learned': dev.get('learned'),
                        'learning': time.time() < dev.get('learn_until', 0)})


def send_ir_code(device_id, code):
    """下发红外 NEC 码（路由与 LLM 工具共用），返回 seq；参数非法返回 None"""
    if not device_id or not isinstance(code, int):
        return None
    with _lock:
        dev = _dev(device_id)
        dev['send_seq'] += 1
        dev['send_data'] = code & 0xFFFFFFFF
        _save()
        seq = dev['send_seq']
    # v1.9.0 统一下行路由
    try:
        send_downlink(device_id, {
            'type': 'ir', 'seq': seq, 'data': code & 0xFFFFFFFF,
        })
    except Exception as e:
        print(f"[红外] 下行失败: {e}")
    print(f"[红外] {device_id[:8]} 下发 0x{code:08X} seq={seq}")
    return seq


@ir_bp.route('/api/admin/ir/send', methods=['POST'])
@admin_required
def api_ir_send():
    data = request.get_json() or {}
    device_id = data.get('device_id', '')
    code = data.get('data')
    seq = send_ir_code(device_id, code)
    if seq is None:
        return jsonify({'status': 'error', 'message': '参数不完整'})
    return jsonify({'status': 'ok', 'seq': seq})


@ir_bp.route('/api/admin/ir/codes', methods=['GET'])
@admin_required
def api_ir_codes():
    with _lock:
        return jsonify({'codes': list(_load()['codes'])})


@ir_bp.route('/api/admin/ir/codes/save', methods=['POST'])
@admin_required
def api_ir_code_save():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()[:20]
    code = data.get('data')
    if not name or not isinstance(code, int):
        return jsonify({'status': 'error', 'message': '参数不完整'})
    with _lock:
        codes = _load()['codes']
        codes[:] = [c for c in codes if c['name'] != name]   # 同名覆盖
        codes.append({'name': name, 'data': code & 0xFFFFFFFF,
                      'added_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
        _save()
    return jsonify({'status': 'ok'})


@ir_bp.route('/api/admin/ir/codes/delete', methods=['POST'])
@admin_required
def api_ir_code_delete():
    data = request.get_json() or {}
    name = data.get('name', '')
    with _lock:
        codes = _load()['codes']
        codes[:] = [c for c in codes if c['name'] != name]
        _save()
    return jsonify({'status': 'ok'})
# -*- coding: utf-8 -*-
"""
TTS 语音播报模块 — Blueprint（5.3，WT3000T-MO1）
=================================================
职责：把中文文本转成 **UTF-8** 十六进制串（WT3000T 默认 UTF-8 编码），供设备取走后经 UART 发给 TTS 模块播报。
- HTTP 模式：设备轮询 GET /api/esp32/tts/pending（1~2s）
- MQTT 模式：POST 时即通过 mqtt_bridge.publish_cmd 推送 {"type":"tts","seq":N,"hex":"..."}

状态保存在内存（设备重启后靠 seq 重新取最新一条；如需持久化可后续存 DB）。
文本可带官方控制标签：[m3]艾佳女声 / [m51]艾诚男声 / [s5]语速 / [t5]音调。
"""
from datetime import datetime

from flask import Blueprint, request, jsonify

from core.auth import admin_required
from link.downlink import send_downlink   # 统一下行路由（mqtt / espnow）

tts_bp = Blueprint('tts', __name__)

MAX_TEXT_CHARS = 60          # 单次播报上限（GB2312 约 ≤120 字节，远小于协议 255 字节）
_state = {}                  # device_id -> {'seq', 'hex', 'text', 'at'}


@tts_bp.route('/api/admin/tts/speak', methods=['POST'])
@admin_required
def api_tts_speak():
    """管理端：让指定设备播报一段中文文本"""
    data = request.get_json() or {}
    device_id = (data.get('device_id') or '').strip()
    text = (data.get('text') or '').strip()
    if not device_id:
        return jsonify({'status': 'error', 'message': '缺少设备ID'})
    if not text:
        return jsonify({'status': 'error', 'message': '缺少文本'})
    text = text[:MAX_TEXT_CHARS]
    try:
        gb = text.encode('utf-8')
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'UTF-8 转码失败: {e}'})
    if not gb:
        return jsonify({'status': 'error', 'message': '文本为空'})

    st = _state.setdefault(device_id, {'seq': 0, 'hex': '', 'text': '', 'at': ''})
    st['seq'] = int(st.get('seq', 0)) + 1
    st['hex'] = gb.hex()
    st['text'] = text
    st['at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    pushed = False
    try:
        pushed = bool(send_downlink(device_id, {
            'type': 'tts', 'seq': st['seq'], 'hex': st['hex'],
        }))
    except Exception as e:
        print(f"[TTS] 下行失败: {e}")
    print(f"[TTS] {device_id[:8]} seq={st['seq']} \"{text}\" "
          f"({len(gb)}B, mqtt推送={pushed})")
    return jsonify({'status': 'ok', 'seq': st['seq'], 'bytes': len(gb), 'pushed': pushed})


@tts_bp.route('/api/esp32/tts/pending', methods=['GET'])
def api_tts_pending():
    """设备端轮询：返回最新一条待播报文本（GB2312 hex + seq）"""
    device_id = request.args.get('device_id', '')
    st = _state.get(device_id) or {'seq': 0, 'hex': ''}
    return jsonify({'seq': st.get('seq', 0), 'hex': st.get('hex', '')})


@tts_bp.route('/api/admin/tts/state', methods=['GET'])
@admin_required
def api_tts_state():
    return jsonify({'states': _state})

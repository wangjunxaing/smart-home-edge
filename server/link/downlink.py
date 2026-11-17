# -*- coding: utf-8 -*-
"""
统一下行路由（v1.9.10：通道自适应 + 可靠性）
==========================================
按设备**当前实际可用**的通道下发：

  - 默认走 MQTT（主通道）；
  - 当节点检测到网络不通、自动降级到 ESP-NOW 后，服务器依据"最近一次上报来自哪条通道"
    自动把下行改走 ESP-NOW（无需人工干预）；
  - 网络稳定恢复、节点切回 MQTT 后，自动回到 MQTT。

同时提供 ACK 确认与自动重发（实现见 `link_state`）。

light.py / ir.py / tts.py 共用本模块，不再各自判断传输类型。

5.13 操作审计：**所有下行都在 `send_downlink()` 收口记录**——
这比在每个 API 里埋点可靠得多（新增调用方自动获得审计，不会漏）。
来源/操作者由调用方传入；网页请求未显式传时自动从 session 取用户名。
"""
import threading
import time

from core.config_manager import get_device_transport
from link.mqtt_bridge import publish_cmd as mqtt_publish_cmd
import link.espnow_link as espnow_link
import link.link_state as link_state

_seq_lock = threading.Lock()
_seq = [0]


def _next_seq():
    with _seq_lock:
        _seq[0] = (_seq[0] + 1) % 1000000
        return _seq[0]


def _transport_for(device_id):
    """该设备当前应使用的下行通道（依据最近上报来源，回落到服务器配置）"""
    return link_state.effective_transport(device_id, get_device_transport(device_id))


def _send_mqtt(device_id, payload):
    mqtt_publish_cmd(device_id, payload)
    return True


def _send_espnow(device_id, payload):
    return espnow_link.send_cmd(device_id, payload)


def start():
    """注册发送器、启动重发线程（app 启动时调用，幂等）"""
    link_state.register({'mqtt': _send_mqtt, 'espnow': _send_espnow}, _transport_for)
    print('[下行] 统一路由就绪：默认 MQTT；节点自动降级后改走 ESP-NOW，恢复后自动回到 MQTT')


def _current_actor():
    """当前操作者：网页请求取 session 用户名；非请求上下文（自动化/自愈）返回空。"""
    try:
        from flask import has_request_context, session
        if has_request_context():
            return session.get('username') or ''
    except Exception:
        pass
    return ''


def _resolve_source(source):
    """确定本次下行的来源：
       1) 调用方显式传入 → 用它
       2) contextvars 里由 tool_use / automation / self_heal 设置 → 用它
       3) 网页请求上下文 → 'web'
       4) 兜底 'unknown'
    """
    if source and source != 'web':
        return source
    try:
        from core import audit
        s, _a = audit.current_context()
        if s:
            return s
    except Exception:
        pass
    try:
        from flask import has_request_context
        if has_request_context():
            return 'web'
    except Exception:
        pass
    return source or 'unknown'


def send_downlink(device_id, payload, source='web', actor=None):
    """下发命令；返回实际使用的通道名（'mqtt' | 'espnow'），失败返回空串。

    5.13：在此统一记操作审计（来源 = web/voice/ai_tool/automation/system）。
    审计失败只打日志，**绝不影响下行本身**。
    """
    if not payload.get('seq'):
        payload = dict(payload)
        payload['seq'] = _next_seq()

    t0 = time.time()
    ch = link_state.send(device_id, payload)
    elapsed_ms = int((time.time() - t0) * 1000)

    try:
        from core import audit
        src = _resolve_source(source)
        act = actor
        if act is None:
            _s, ctx_actor = audit.current_context()
            act = ctx_actor if ctx_actor is not None else _current_actor()
        audit.log_operation(src, act, device_id, payload, 'ok' if ch else 'fail',
                            ch or '', elapsed_ms)
    except Exception as e:
        print(f'[下行] 审计记录异常（已忽略）: {type(e).__name__}: {e}')

    return ch

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
"""
import threading

from config_manager import get_device_transport
from mqtt_bridge import publish_cmd as mqtt_publish_cmd
import espnow_link
import link_state

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


def send_downlink(device_id, payload):
    """下发命令；返回实际使用的通道名（'mqtt' | 'espnow'），失败返回空串"""
    if not payload.get('seq'):
        payload = dict(payload)
        payload['seq'] = _next_seq()
    return link_state.send(device_id, payload)

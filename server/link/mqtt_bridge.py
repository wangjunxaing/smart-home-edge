# -*- coding: utf-8 -*-
"""
MQTT 桥接模块 — 订阅 ESP32 上报 + 下行命令发布（阶段二 5.1）
============================================================
架构（HTTP 上报保留为回退，MQTT 灰度接入）:
  ESP32 --publish /esp32/{id}/sensors--> Mosquitto(:1883) --> mqtt_bridge 订阅
     --> 复用 store_sensor_reading 写库（与 HTTP 上报同一路径）
  下行: 应用层 publish_cmd() --> /esp32/{id}/cmd --> ESP32 订阅即时执行
  遗嘱: 设备连接时设置 LWT /esp32/{id}/lwt（"offline"）--> 桥标记离线

Topic 约定:
  /esp32/{id}/sensors   设备 → 服务器  传感器 JSON（与 HTTP POST body 同构）
  /esp32/{id}/cmd       服务器 → 设备  控制命令 JSON
  /esp32/{id}/ack       设备 → 服务器  控制执行确认（含 seq/ts，时延实测用）
  /esp32/{id}/lwt       设备遗嘱       "offline"
"""
import json
import time
import threading
from datetime import datetime

import paho.mqtt.client as mqtt

from api.sensors import store_sensor_reading
from core.database import get_sensors_db
import link.link_state as link_state          # 链路状态：下行通道推断 + 回执确认（无业务依赖，避免循环导入）

BROKER_HOST = '127.0.0.1'
BROKER_PORT = 1883
CLIENT_ID = 'pi-mqtt-bridge'

_client = None
_started = False
_running = False

# 下行命令发送时刻表：(device_id, seq) -> 发送毫秒时间戳
# ack 回执到达时计算"下发→设备确认"端到端控制时延（论文 HTTP vs MQTT 对比用）
_pending_cmd = {}
_PENDING_MAX = 256

# 可选回调（由 app 层注册，如 automation/前端通知）
on_sensor_cb = None      # fn(device_id, data)
on_offline_cb = None     # fn(device_id)


def _now_ms():
    return int(time.time() * 1000)


# ==================== MQTT 回调 ====================

def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[MQTT] 已连接 broker {BROKER_HOST}:{BROKER_PORT}")
        client.subscribe('/esp32/+/sensors', qos=1)
        client.subscribe('/esp32/+/lwt', qos=1)
        client.subscribe('/esp32/+/ack', qos=1)
        print("[MQTT] 已订阅 sensors/lwt/ack")
    else:
        print(f"[MQTT] 连接失败 rc={rc}")


def _on_message(client, userdata, msg):
    topic = msg.topic
    parts = topic.split('/')
    # /esp32/{id}/{kind}
    if len(parts) < 4 or parts[1] != 'esp32':
        return
    device_id = parts[2]
    kind = parts[3]

    if kind == 'sensors':
        try:
            data = json.loads(msg.payload.decode('utf-8'))
            if not isinstance(data, dict):
                return
            data.setdefault('id', device_id)
            _, err = store_sensor_reading(data)
            if err:
                print(f"[MQTT] 写入失败 {device_id[:8]}: {err}")
            else:
                # 论文数据点：MQTT 上行接收时刻（设备侧 t0_ms 已随包，
                # 需时钟标定后换算绝对时延；对比 HTTP 数据点见 sensors.py）
                link_state.mark_up(device_id, 'mqtt')   # 供下行通道选择：MQTT 通道当前活跃
                if data.get('t0_ms') is not None:
                    cur = data.get('current_ma')
                    cur_s = f' cur={cur}' if cur is not None else ''
                    print(f"[MQTT-UP] {device_id[:8]} t0={data['t0_ms']} "
                          f"recv_ms={_now_ms()}{cur_s}")
                    # 5.7：若刚下发过重启且 t0 归零 → 判定"重启归来"（服务端自证重启成功）
                    try:
                        from core.self_heal import note_uplink
                        note_uplink(device_id, data.get('t0_ms'))
                    except Exception:
                        pass
                if on_sensor_cb:
                    try:
                        on_sensor_cb(device_id, data)
                    except Exception:
                        pass
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"[MQTT] 非法 JSON 来自 {device_id[:8]}: {msg.payload[:80]}")

    elif kind == 'ack':
        # 控制回执：计算"下发→设备 ack"端到端控制时延（服务器时钟，无需时钟同步）
        try:
            data = json.loads(msg.payload.decode('utf-8'))
            seq = data.get('seq')
            link_state.notify_ack(device_id, seq)      # 通知下行可靠性模块（清除待回执）
            key = (device_id, seq)
            sent = _pending_cmd.pop(key, None)
            if sent is not None:
                lat = _now_ms() - sent
                print(f"[MQTT-ACK] {device_id[:8]} seq={seq} "
                      f"latency_ms={lat} dev_t_ms={data.get('t_ms')}")
            else:
                print(f"[MQTT-ACK] {device_id[:8]} seq={seq} (无对应下发记录)")
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"[MQTT] 非法 ack 来自 {device_id[:8]}: {msg.payload[:80]}")

    elif kind == 'lwt':
        # 设备异常断线（遗嘱）→ 标记离线（把 last_update_ts 拨回 1 分钟前）
        try:
            conn = get_sensors_db()
            c = conn.cursor()
            old_ts = datetime.now().timestamp() - 60
            c.execute("UPDATE sensor_latest SET last_update_ts=? WHERE esp32_id=?",
                      (datetime.fromtimestamp(old_ts).isoformat(), device_id))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[MQTT] 遗嘱处理失败: {e}")
        print(f"[MQTT] 设备离线(遗嘱): {device_id[:8]}")
        if on_offline_cb:
            try:
                on_offline_cb(device_id)
            except Exception:
                pass


# ==================== 对外接口 ====================

def publish_cmd(device_id, payload):
    """服务器 → 设备：下发控制命令（dict 转 JSON）

    若 payload 含 seq：记录发送时刻，收到设备 ack 时打印端到端时延
    （[MQTT-ACK] ... latency_ms=...，论文 HTTP vs MQTT 控制时延对比数据源）。
    """
    if _client is None or not _client.is_connected():
        return False
    topic = f'/esp32/{device_id}/cmd'
    try:
        seq = payload.get('seq')
        msg = dict(payload)
        if seq is not None:
            msg.setdefault('ts', _now_ms())   # 服务器下发时刻（毫秒）
            _pending_cmd[(device_id, seq)] = _now_ms()
            # 防止表无限增长：只保留最近 _PENDING_MAX 条
            while len(_pending_cmd) > _PENDING_MAX:
                _pending_cmd.pop(next(iter(_pending_cmd)), None)
        _client.publish(topic, json.dumps(msg, ensure_ascii=False), qos=1)
        return True
    except Exception as e:
        print(f"[MQTT] 命令发布失败: {e}")
        return False


def _loop():
    global _running
    while _running:
        try:
            _client.loop_forever()
        except Exception as e:
            print(f"[MQTT] 循环异常，5s 后重连: {e}")
            time.sleep(5)
            try:
                _client.reconnect()
            except Exception:
                pass


def start_bridge():
    """启动 MQTT 桥后台线程（app 启动时调用，幂等）"""
    global _client, _started, _running
    if _started:
        return
    _started = True
    _running = True

    _client = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
    _client.on_connect = _on_connect
    _client.on_message = _on_message
    _client.reconnect_delay_set(1, 30)   # 断线重连退避

    try:
        _client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    except Exception as e:
        print(f"[MQTT] 初始连接失败（将后台重试）: {e}")

    threading.Thread(target=_loop, daemon=True).start()
    print(f"[MQTT] 桥已启动（{BROKER_HOST}:{BROKER_PORT}）")

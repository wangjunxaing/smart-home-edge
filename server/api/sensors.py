# -*- coding: utf-8 -*-
"""
传感器数据模块 — Blueprint
==========================
ESP32 数据上报 / 最新数据 / 历史查询 / CSV 导出 / 异常记录 / 配置管理。
历史数据聚合可选择调用 C 加速程序 (sensor_history) 提效 2x+。
"""
import os
import io
import csv
import time
import json
import subprocess
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify

from core.database import get_sensors_db
from core.config_manager import (VALID_LOCATIONS, VALID_TRANSPORTS, DEFAULT_CONFIG,
                                 load_device_config, save_device_config, get_device_alias)
from core.auth import admin_required
from core.config import (SENSOR_ERROR_LOG, SENSOR_HISTORY_BIN, SENSORS_DB)

sensors_bp = Blueprint('sensors', __name__)

SENSOR_ERROR_LOG = str(SENSOR_ERROR_LOG)
_latest_cache = {}

# C 加速程序路径
_HISTORY_BIN = str(SENSOR_HISTORY_BIN)


def _call_c_history(esp32_id, hours, location):
    """调用 C 加速程序进行历史数据聚合，失败时回退 Python 实现"""
    try:
        args = [_HISTORY_BIN, SENSORS_DB_PATH,
                esp32_id or '', str(hours), location or '']
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as e:
        print(f"[C加速] 历史聚合 C 程序调用失败: {e}")
    return None


SENSORS_DB_PATH = str(SENSORS_DB)


# ==================== ESP32 配置 API ====================

@sensors_bp.route('/api/esp32/config', methods=['GET'])
def api_esp32_config():
    device_id = request.args.get('device_id', '')
    config = load_device_config(device_id)
    return jsonify(config)


@sensors_bp.route('/api/esp32/config/update', methods=['POST'])
@admin_required
def api_esp32_config_update():
    data = request.get_json()
    if not data or 'device_id' not in data:
        return jsonify({'status': 'error', 'message': '缺少设备ID'})
    device_id = data['device_id']
    config = load_device_config(device_id)
    if 'send_interval' in data:
        val = int(data['send_interval'])
        if 1000 <= val <= 60000:
            config['send_interval'] = val
    if 'location' in data and data['location'] in VALID_LOCATIONS:
        config['location'] = data['location']
    if 'transport' in data and data['transport'] in VALID_TRANSPORTS:
        config['transport'] = data['transport']
    if 'payload_pad' in data:
        try:
            pad = int(data['payload_pad'])
        except (TypeError, ValueError):
            pad = 0
        config['payload_pad'] = max(0, min(80, pad))   # T5 上行载荷扫描（填充字节数）
    save_device_config(device_id, config)
    print(f"[配置] 更新 {device_id[:8]}: {config}")
    return jsonify({'status': 'ok'})


@sensors_bp.route('/api/esp32/config/locations', methods=['GET'])
def api_esp32_locations():
    return jsonify({'locations': VALID_LOCATIONS})


@sensors_bp.route('/api/esp32/config/alias', methods=['POST'])
@admin_required
def api_esp32_alias():
    data = request.get_json()
    if not data or 'device_id' not in data:
        return jsonify({'status': 'error', 'message': '缺少设备ID'})
    device_id = data['device_id']
    alias = data.get('alias', '').strip()
    if len(alias) > 20:
        alias = alias[:20]
    config = load_device_config(device_id)
    config['alias'] = alias
    save_device_config(device_id, config)
    print(f"[别名] 设置 {device_id[:8]} → '{alias}'")
    return jsonify({'status': 'ok'})


# ==================== 传感器数据接收 ====================

def _as_int(v, default=-1):
    """把上报值安全转成 int（缺字段/非法值 → default）"""
    if v is None:
        return default
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


# 5.8 毫米波远程诊断：带 mm_rx 的上报若"没解析出人感"就打印一行诊断（限频 30s/设备）
_mm_diag_ts = {}

# location 回查缓存（device_id -> location），避免 ESP-NOW 每条上报都查库
_loc_cache = {}


def _lookup_location(esp32_id):
    """ESP-NOW 精简 payload 不带 location 时，按设备 ID 从 device_config 回查。

    位置对每个节点是固定的，缓存 5 分钟即可；查不到返回 '未分类'。
    """
    if not esp32_id:
        return '未分类'
    now = time.time()
    hit = _loc_cache.get(esp32_id)
    if hit and now - hit[1] < 300:
        return hit[0]
    loc = '未分类'
    try:
        conn = get_sensors_db()
        row = conn.execute('SELECT location FROM device_config WHERE device_id = ?',
                           (esp32_id,)).fetchone()
        conn.close()
        if row and row['location']:
            loc = row['location']
    except Exception:
        pass
    _loc_cache[esp32_id] = (loc, now)
    return loc


def _mm_diag(esp32_id, data):
    """诊断含义：
    rx=0                → 串口完全没字节：接线/供电/波特率问题
    rx>0, frames=0      → 收到乱码：波特率不匹配（LD2410C 也有 115200 版本）
    frames>0, person<0  → 帧能解析但状态字段异常：协议版本差异
    """
    if 'mm_rx' not in data:
        return
    person = _as_int(data.get('person'), -1)
    if person >= 0:
        return
    now = time.time()
    if now - _mm_diag_ts.get(esp32_id, 0) < 30:
        return
    _mm_diag_ts[esp32_id] = now
    rx = _as_int(data.get('mm_rx'), -1)
    frames = _as_int(data.get('mm_frames'), -1)
    bad = _as_int(data.get('mm_bad'), -1)
    baud = _as_int(data.get('mm_baud'), -1)
    tries = _as_int(data.get('mm_tries'), -1)
    if rx <= 0:
        # rx=0 说明该节点根本没装毫米波（如 04B），属正常，不必刷日志
        return
    if frames == 0:
        hint = f'有字节但无有效帧（波特率={baud}，已试{tries}种）→ 检查帧格式/接线'
    else:
        hint = '帧已解析但无有效目标状态 → 协议字段差异'
    ver = _as_int(data.get('mm_ver'), -1)
    hexs = data.get('mm_hex') or ''
    print(f"[毫米波诊断] {esp32_id[:8]} rx={rx} frames={frames} bad={bad} "
          f"baud={baud} tries={tries} proto={ver}")
    if hexs:
        # 关键证据：模块到底吐的是什么字节。aa ff 03 00 → LD2450；f4 f3 f2 f1 → LD2410C
        print(f"[毫米波原始] {esp32_id[:8]} {hexs}")
    if rx > 0 and hexs.lower().startswith('aaff'):
        print(f"[毫米波诊断] {esp32_id[:8]} 已识别为 LD2450 帧头 aa ff 03 00，"
              f"若仍无有效帧需检查长度/帧尾 55 cc 或波特率")
    elif rx > 0 and hexs.lower().startswith('f4f3'):
        print(f"[毫米波诊断] {esp32_id[:8]} 已识别为 LD2410C 帧头 f4 f3 f2 f1")


def store_sensor_reading(data):
    """处理一条 ESP32 上报数据（HTTP 路由与 MQTT 桥共用），写入 latest + history。
    返回 (esp32_id, error)；error 为 None 表示成功。"""
    esp32_id = data.get("id", "unknown")
    # location：ESP-NOW 兜底通道为了控制 payload ≤200B 不再上报 location；
    # 网关转发的兜底值也是"未分类" → 两者都视为"需要按 device_id 回查"。
    # （2026-11-14：网关 setdefault 补"未分类"导致这里不走回查，历史数据落到未分类）
    location = data.get("location")
    if not location or location == '未分类':
        location = _lookup_location(esp32_id)
    if location not in VALID_LOCATIONS:
        location = "未分类"

    now_str = datetime.now().strftime("%H:%M:%S")
    now_ts = datetime.now().isoformat()
    local_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    _mm_diag(esp32_id, data)      # 5.8 毫米波远程诊断（仅当带 mm_rx 且人感无效时打印）

    temperature = data.get("temperature")
    humidity = data.get("humidity")
    lux = data.get("lux")
    rssi = data.get("rssi")

    # 5.8 毫米波（未安装该模块的节点不带这些字段 → 统一落 -1）
    mm = {
        'person': _as_int(data.get('person'), -1),
        'mm_state': _as_int(data.get('mm_state'), -1),
        'mm_move_dist': _as_int(data.get('mm_move_dist'), -1),
        'mm_still_dist': _as_int(data.get('mm_still_dist'), -1),
        'mm_detect_dist': _as_int(data.get('mm_detect_dist'), -1),
        'mm_move_energy': _as_int(data.get('mm_move_energy'), -1),
        'mm_still_energy': _as_int(data.get('mm_still_energy'), -1),
        # 5.8.2 LD2450 平面坐标（2D）；-99999 表示该节点不是 LD2450
        'mm_proto': _as_int(data.get('mm_proto'), -1),
        'mm_x': _as_int(data.get('mm_x'), -99999),
        'mm_y': _as_int(data.get('mm_y'), -99999),
        'mm_speed': _as_int(data.get('mm_speed'), -99999),
    }

    # 5.14 能耗：INA219 电流（mA）。-1 = 该节点未接 INA219（如 68FE）
    current_ma = None
    try:
        v = data.get('current_ma')
        if v is not None:
            current_ma = float(v)
            if current_ma < 0:
                current_ma = -1.0
    except (TypeError, ValueError):
        current_ma = -1.0

    try:
        conn = get_sensors_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO sensor_latest
            (esp32_id, location, temperature, humidity, lux, rssi, last_update, last_update_ts,
             person, mm_state, mm_move_dist, mm_still_dist, mm_detect_dist,
             mm_move_energy, mm_still_energy, mm_proto, mm_x, mm_y, mm_speed, current_ma)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (esp32_id, location,
              temperature if temperature is not None else -1.0,
              humidity if humidity is not None else -1.0,
              lux if lux is not None else -1.0,
              rssi if rssi is not None else -100.0,
              now_str, now_ts,
              mm['person'], mm['mm_state'], mm['mm_move_dist'], mm['mm_still_dist'],
              mm['mm_detect_dist'], mm['mm_move_energy'], mm['mm_still_energy'],
              mm['mm_proto'], mm['mm_x'], mm['mm_y'], mm['mm_speed'],
              current_ma if current_ma is not None else -1.0))

        cursor.execute(
            "INSERT INTO sensor_history (esp32_id, location, temperature, humidity, lux, timestamp, "
            "person, mm_state, mm_move_dist, mm_still_dist, mm_detect_dist, "
            "mm_move_energy, mm_still_energy, mm_proto, mm_x, mm_y, mm_speed, current_ma) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (esp32_id, location,
             temperature if temperature is not None else -1.0,
             humidity if humidity is not None else -1.0,
             lux if lux is not None else -1.0,
             local_now,
             mm['person'], mm['mm_state'], mm['mm_move_dist'], mm['mm_still_dist'],
             mm['mm_detect_dist'], mm['mm_move_energy'], mm['mm_still_energy'],
             mm['mm_proto'], mm['mm_x'], mm['mm_y'], mm['mm_speed'],
             current_ma if current_ma is not None else -1.0)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Database] 写入失败: {e}")
        return esp32_id, str(e)
    return esp32_id, None


@sensors_bp.route('/api/esp32/sensors', methods=['POST'])
def api_esp32_sensors():
    data = request.get_json()
    if data:
        esp32_id, err = store_sensor_reading(data)
        if err:
            return jsonify({'status': 'error', 'message': err}), 500
        # 论文数据点：HTTP 上行接收时刻（设备侧 t0_ms 已随包，对比 MQTT 见 mqtt_bridge）
        if data.get('t0_ms') is not None:
            cur = data.get('current_ma')
            cur_s = f' cur={cur}' if cur is not None else ''
            print(f"[HTTP-UP] {esp32_id[:8]} t0={data['t0_ms']} "
                  f"recv_ms={int(time.time() * 1000)}{cur_s}")
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': '无数据'}), 400


@sensors_bp.route('/api/esp32/sensors/error', methods=['POST'])
def api_esp32_sensors_error():
    data = request.get_json()
    if not data:
        return jsonify({'status': 'error', 'message': '无数据'}), 400
    esp32_id = data.get("id", "unknown")
    location = data.get("location", "未分类")
    error_type = data.get("error", "未知异常")
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    log_line = f"[{now_str}] [{esp32_id[:8]}][{location}] {error_type}\n"
    try:
        with open(SENSOR_ERROR_LOG, 'a', encoding='utf-8') as f:
            f.write(log_line)
        print(f"[异常记录] {log_line.strip()}")
    except Exception as e:
        print(f"[异常记录] 写入失败: {e}")
    return jsonify({'status': 'ok'})


@sensors_bp.route('/api/esp32/led', methods=['POST'])
def esp32_led():
    data = request.get_json()
    print(f"[ESP32] LED 指令: {data}")
    return jsonify({'status': 'ok'})


# ==================== 最新数据查询 ====================

@sensors_bp.route('/api/esp32/sensors/latest', methods=['GET'])
def api_esp32_sensors_latest():
    target_location = request.args.get('location', '卧室')
    if target_location not in VALID_LOCATIONS:
        target_location = '卧室'

    cache_key = f"latest_{target_location}"
    cached_t = _latest_cache.get(cache_key + '_t', 0)
    if time.time() - cached_t < 2 and cache_key in _latest_cache:
        return jsonify(_latest_cache[cache_key])

    try:
        conn = get_sensors_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM sensor_latest')
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"[Database] 读取最新数据失败: {e}")
        rows = []

    devices = {}
    now_dt = datetime.now()
    for row in rows:
        is_online = False
        if row['last_update_ts']:
            try:
                last_ts = datetime.fromisoformat(row['last_update_ts'])
                is_online = (now_dt - last_ts).total_seconds() < 30
            except Exception:
                is_online = False

        devices[row['esp32_id']] = {
            'lux': row['lux'], 'temperature': row['temperature'],
            'humidity': row['humidity'], 'location': row['location'],
            'last_update': row['last_update'], 'last_update_ts': row['last_update_ts'],
            'online': is_online, 'alias': get_device_alias(row['esp32_id']),
            'rssi': row['rssi'] if row['rssi'] is not None else -100.0,
            # 5.8 毫米波（-1 = 该节点未安装或未收到有效帧）
            'person': row['person'] if 'person' in row.keys() else -1,
            'mm_state': row['mm_state'] if 'mm_state' in row.keys() else -1,
            'mm_move_dist': row['mm_move_dist'] if 'mm_move_dist' in row.keys() else -1,
            'mm_still_dist': row['mm_still_dist'] if 'mm_still_dist' in row.keys() else -1,
            'mm_detect_dist': row['mm_detect_dist'] if 'mm_detect_dist' in row.keys() else -1,
            'mm_move_energy': row['mm_move_energy'] if 'mm_move_energy' in row.keys() else -1,
            'mm_still_energy': row['mm_still_energy'] if 'mm_still_energy' in row.keys() else -1,
            # 5.8.2 LD2450 平面坐标（2D，-99999 = 不是 LD2450）
            'mm_proto': row['mm_proto'] if 'mm_proto' in row.keys() else -1,
            'mm_x': row['mm_x'] if 'mm_x' in row.keys() else -99999,
            'mm_y': row['mm_y'] if 'mm_y' in row.keys() else -99999,
            'mm_speed': row['mm_speed'] if 'mm_speed' in row.keys() else -99999,
            # 5.14 能耗：INA219 电流（mA，-1 = 该节点未接 INA219）
            'current_ma': row['current_ma'] if 'current_ma' in row.keys() else -1.0,
        }

    online_count = sum(1 for d in devices.values() if d.get('online'))

    if not devices:
        return jsonify({
            'devices': {}, 'online_count': 0,
            'avg_temperature': -1.0, 'avg_humidity': -1.0, 'avg_lux': -1.0,
            'last_update': None, 'locations': VALID_LOCATIONS,
            'current_location': target_location, 'location_counts': {},
            'filtered_count': 0, 'alerts': []
        })

    filtered_devices = {k: v for k, v in devices.items()
                        if v.get('location') == target_location and v.get('online', False)}

    temps = [d['temperature'] for d in filtered_devices.values() if d['temperature'] >= 0]
    hums = [d['humidity'] for d in filtered_devices.values() if d['humidity'] >= 0]
    luxs = [d['lux'] for d in filtered_devices.values() if d['lux'] >= 0]

    avg_temp = sum(temps) / len(temps) if temps else -1.0
    avg_hum = sum(hums) / len(hums) if hums else -1.0
    avg_lux = sum(luxs) / len(luxs) if luxs else -1.0

    latest_ts = max([d['last_update'] for d in devices.values()
                     if d.get('last_update')], default=None)

    location_counts = {}
    for d in devices.values():
        if d.get('online'):
            loc = d.get('location', '未分类')
            location_counts[loc] = location_counts.get(loc, 0) + 1

    alerts = []
    if avg_lux >= 0:
        if avg_temp > 40:
            alerts.append("温度 %.1f°C 超过40°C" % avg_temp)
        if avg_temp < 0:
            alerts.append("温度 %.1f°C 低于0°C" % avg_temp)
        if avg_hum > 85:
            alerts.append("湿度 %.1f%% 超过85%%" % avg_hum)
        if avg_hum < 20:
            alerts.append("湿度 %.1f%% 低于20%%" % avg_hum)

    result = {
        'devices': devices, 'online_count': online_count,
        'avg_temperature': round(avg_temp, 1), 'avg_humidity': round(avg_hum, 1),
        'avg_lux': round(avg_lux, 1), 'last_update': latest_ts,
        'locations': VALID_LOCATIONS, 'current_location': target_location,
        'location_counts': location_counts, 'filtered_count': len(filtered_devices),
        'alerts': alerts
    }

    _latest_cache[cache_key] = result
    _latest_cache[cache_key + '_t'] = time.time()
    return jsonify(result)


# ==================== 历史数据查询 (C 加速 + Python 回退) ====================

def _history_python(esp32_id, hours, location, start_time_str):
    """Python 实现的历史数据聚合（C 程序不可用时的回退）"""
    from collections import defaultdict

    if hours <= 1:
        group_minutes = 1
    elif hours <= 3:
        group_minutes = 5
    elif hours <= 6:
        group_minutes = 10
    elif hours <= 12:
        group_minutes = 15
    else:
        group_minutes = 30

    conn = get_sensors_db()
    cursor = conn.cursor()
    conditions = []
    params = []

    if esp32_id:
        conditions.append("esp32_id = ?")
        params.append(esp32_id)
    if location:
        conditions.append("location = ?")
        params.append(location)
    conditions.append("timestamp >= ?")
    params.append(start_time_str)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    cursor.execute(
        'SELECT temperature, humidity, lux, timestamp'
        ' FROM sensor_history WHERE ' + where_clause + ' AND timestamp IS NOT NULL'
        ' ORDER BY timestamp ASC', params
    )
    rows = cursor.fetchall()
    conn.close()

    groups = defaultdict(list)
    for row in rows:
        ts = row['timestamp']
        if not ts:
            continue
        try:
            parts = ts.split(' ')
            h, m, _ = parts[1].split(':')
            m_group = int(int(m) / group_minutes) * group_minutes
            bucket_key = f"{parts[0]} {h}:{m_group:02d}:00"
        except Exception:
            bucket_key = ts
        groups[bucket_key].append(row)

    full_result = []
    start_dt = datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S')
    start_minute = int(start_dt.minute / group_minutes) * group_minutes
    aligned_start = start_dt.replace(minute=start_minute, second=0, microsecond=0)
    current = aligned_start
    end_dt = datetime.now()
    while current <= end_dt:
        m = int(current.minute / group_minutes) * group_minutes
        bucket_key = f"{current.strftime('%Y-%m-%d')} {current.strftime('%H')}:{m:02d}:00"

        if bucket_key in groups:
            items = groups[bucket_key]
            temps = [r['temperature'] for r in items
                     if r['temperature'] is not None and r['temperature'] >= 0]
            hums = [r['humidity'] for r in items
                    if r['humidity'] is not None and r['humidity'] >= 0]
            luxs = [r['lux'] for r in items
                    if r['lux'] is not None and r['lux'] >= 0]
            full_result.append({
                'temperature': round(sum(temps) / len(temps), 1) if temps else None,
                'humidity': round(sum(hums) / len(hums), 1) if hums else None,
                'lux': round(sum(luxs) / len(luxs), 1) if luxs else None,
                'timestamp': bucket_key, 'sample_count': len(items),
                'group_minutes': group_minutes
            })
        else:
            full_result.append({
                'temperature': None, 'humidity': None, 'lux': None,
                'timestamp': bucket_key, 'sample_count': 0,
                'group_minutes': group_minutes, 'gap': True
            })
        current += timedelta(minutes=group_minutes)

    return full_result, len(rows), len(groups)


@sensors_bp.route('/api/esp32/sensors/history', methods=['GET'])
def api_esp32_sensors_history():
    esp32_id = request.args.get('id', '')
    hours = request.args.get('hours', '1')
    location = request.args.get('location', '')
    try:
        hours = int(hours)
    except Exception:
        hours = 1

    start_time_str = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')

    # 尝试 C 加速
    result = _call_c_history(esp32_id, hours, location)
    if result is not None:
        return jsonify(result)

    # Python 回退
    full_result, raw_count, group_count = _history_python(esp32_id, hours, location, start_time_str)
    print(f"[历史] 查询到 {raw_count} 条数据 → {len(full_result)} 个窗口 "
          f"({group_count} 有数据)")
    return jsonify(full_result)


# ==================== CSV 导出 ====================

@sensors_bp.route('/api/esp32/sensors/export', methods=['GET'])
def api_esp32_sensors_export():
    esp32_id = request.args.get('id', '')
    location = request.args.get('location', '')
    hours = request.args.get('hours', '24')
    try:
        hours = int(hours)
    except Exception:
        hours = 24

    conn = get_sensors_db()
    cursor = conn.cursor()
    conditions = []
    params = []
    if esp32_id:
        conditions.append("esp32_id = ?")
        params.append(esp32_id)
    if location:
        conditions.append("location = ?")
        params.append(location)
    start_time = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    conditions.append("timestamp >= ?")
    params.append(start_time)
    where_clause = " AND ".join(conditions) if conditions else "1=1"

    cursor.execute(
        'SELECT esp32_id, location, temperature, humidity, lux, timestamp'
        ' FROM sensor_history WHERE ' + where_clause + ' ORDER BY timestamp ASC',
        params
    )
    rows = cursor.fetchall()
    conn.close()

    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(['时间', '设备ID', '位置', '温度(°C)', '湿度(%RH)', '光照(lux)'])
    for row in rows:
        writer.writerow([
            row['timestamp'], row['esp32_id'][:8] + '...', row['location'],
            f"{row['temperature']:.1f}" if row['temperature'] is not None
            and row['temperature'] >= 0 else 'N/A',
            f"{row['humidity']:.1f}" if row['humidity'] is not None
            and row['humidity'] >= 0 else 'N/A',
            f"{row['lux']:.1f}" if row['lux'] is not None
            and row['lux'] >= 0 else 'N/A'
        ])

    output = si.getvalue()
    si.close()

    from flask import Response
    return Response(output, mimetype='text/csv',
                    headers={'Content-Disposition':
                             'attachment; filename=sensor_data.csv'})

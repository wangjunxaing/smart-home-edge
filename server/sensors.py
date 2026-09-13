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

from database import get_sensors_db
from config_manager import (VALID_LOCATIONS, VALID_TRANSPORTS, DEFAULT_CONFIG,
                            load_device_config, save_device_config, get_device_alias)
from auth import admin_required

sensors_bp = Blueprint('sensors', __name__)

SENSOR_ERROR_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sensor_errors.log')
_latest_cache = {}

# C 加速程序路径
_HISTORY_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sensor_history')


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


SENSORS_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sensors_history.db')


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

def store_sensor_reading(data):
    """处理一条 ESP32 上报数据（HTTP 路由与 MQTT 桥共用），写入 latest + history。
    返回 (esp32_id, error)；error 为 None 表示成功。"""
    esp32_id = data.get("id", "unknown")
    location = data.get("location", "未分类")
    if location not in VALID_LOCATIONS:
        location = "未分类"

    now_str = datetime.now().strftime("%H:%M:%S")
    now_ts = datetime.now().isoformat()
    local_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    temperature = data.get("temperature")
    humidity = data.get("humidity")
    lux = data.get("lux")
    rssi = data.get("rssi")

    try:
        conn = get_sensors_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO sensor_latest
            (esp32_id, location, temperature, humidity, lux, rssi, last_update, last_update_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (esp32_id, location,
              temperature if temperature is not None else -1.0,
              humidity if humidity is not None else -1.0,
              lux if lux is not None else -1.0,
              rssi if rssi is not None else -100.0,
              now_str, now_ts))

        cursor.execute(
            "INSERT INTO sensor_history (esp32_id, location, temperature, humidity, lux, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (esp32_id, location,
             temperature if temperature is not None else -1.0,
             humidity if humidity is not None else -1.0,
             lux if lux is not None else -1.0,
             local_now)
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
            'rssi': row['rssi'] if row['rssi'] is not None else -100.0
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

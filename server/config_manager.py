# -*- coding: utf-8 -*-
"""
ESP32 动态配置管理模块
=====================
设备配置持久化到 SQLite device_config 表，支持 send_interval / location / alias /
transport（v1.9.0：mqtt 长连接 / espnow 无线内网，可切换；历史值 http 自动归一为 mqtt）。
"""
from database import get_sensors_db

VALID_LOCATIONS = ['卧室', '客厅', '厨房', '室外', '书房', '未分类']
# v1.9.0 起 HTTP 数据通道已移除（HTTP 仅作配置/OTA 控制面）；输入 'http' 归一为 'mqtt'
VALID_TRANSPORTS = ['mqtt', 'espnow']
DEFAULT_CONFIG = {'send_interval': 5000, 'transport': 'mqtt', 'payload_pad': 0}
PAYLOAD_PAD_MAX = 80      # 上行 JSON 填充上限（ESP-NOW 单帧 250B，留余量）


def _normalize_transport(value):
    if not value:
        return 'mqtt'
    v = str(value).strip().lower()
    if v == 'http':          # 历史配置兼容
        return 'mqtt'
    return v if v in VALID_TRANSPORTS else 'mqtt'


def _row_config(row):
    """把 DB 行转成配置 dict（兼容无 transport / payload_pad 列的旧库）"""
    cfg = {
        'send_interval': row['send_interval'],
        'location': row['location'],
        'alias': row['alias'] or '',
        'transport': 'mqtt',
        'payload_pad': 0,
    }
    try:
        cfg['transport'] = _normalize_transport(row['transport'])
    except (IndexError, KeyError):
        pass
    try:
        cfg['payload_pad'] = int(row['payload_pad'] or 0)
    except (IndexError, KeyError, TypeError, ValueError):
        pass
    return cfg


def load_device_config(device_id):
    """从数据库读取设备配置"""
    try:
        conn = get_sensors_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM device_config WHERE device_id = ?', (device_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return _row_config(row)
    except Exception as e:
        print(f"[配置] 读取失败: {e}")
    return dict(DEFAULT_CONFIG)


def save_device_config(device_id, config):
    """保存设备配置到数据库 (upsert)"""
    transport = _normalize_transport(config.get('transport'))
    try:
        pad = int(config.get('payload_pad', 0) or 0)
    except (TypeError, ValueError):
        pad = 0
    pad = max(0, min(PAYLOAD_PAD_MAX, pad))
    try:
        conn = get_sensors_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO device_config
                (device_id, send_interval, location, alias, transport, payload_pad)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (device_id, config.get('send_interval', 5000),
              config.get('location', '未分类'), config.get('alias', ''),
              transport, pad))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[配置] 写入失败: {e}")


def get_device_alias(device_id):
    """获取设备别名"""
    config = load_device_config(device_id)
    return config.get('alias', '')


def get_device_transport(device_id):
    """获取设备传输模式（v1.9.0）：'mqtt' | 'espnow'，缺省 mqtt"""
    return load_device_config(device_id).get('transport', 'mqtt')

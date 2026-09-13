# -*- coding: utf-8 -*-
"""
自动化规则引擎模块 — Blueprint + 后台线程（阶段一 v1 雏形）
==========================================================
阈值联动：定时读取 sensor_latest，命中规则条件则执行动作（灯带/告警）。

规则模型（automation.db / automation_rules）:
  id, name, enabled, location, metric, operator, threshold,
  action, device_id, cooldown

动作 v1（当前可控设备有限，先支持灯带 + 告警）:
  light_on / light_off — 控制灯带（device_id 缺省自动选第一个在线设备）
  notify              — 仅记录触发日志（告警占位，阶段二接 MQTT 推送）

引擎: 每 ENGINE_INTERVAL 秒检查一次，命中后进入冷却（防抖），
条件恢复后自动解除冷却。触发记录写入 automation_logs。

API（均需登录）:
  GET    /api/admin/automation           规则列表 + 引擎状态
  POST   /api/admin/automation           新增规则
  PUT    /api/admin/automation/<id>      更新规则（含启停）
  DELETE /api/admin/automation/<id>      删除规则
  GET    /api/admin/automation/logs      最近触发记录
"""
import os
import time
import sqlite3
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify

from database import get_sensors_db
from auth import login_required, admin_required
from light import set_light_state

automation_bp = Blueprint('automation', __name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'automation.db')
ENGINE_INTERVAL = 15        # 引擎检查周期（秒）
DEFAULT_COOLDOWN = 300      # 默认冷却（秒），防止频繁触发
VALID_METRICS = {'lux': 'lux', 'temperature': 'temperature', 'humidity': 'humidity'}
VALID_ACTIONS = {'light_on', 'light_off', 'notify'}

_last_trigger = {}          # rule_id -> timestamp（防抖）
_engine_started = False
_engine_running = False
_last_check = None


# ==================== 数据库 ====================

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_automation_db():
    """幂等初始化规则库"""
    conn = _get_conn()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS automation_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            location TEXT DEFAULT '卧室',
            metric TEXT NOT NULL,
            operator TEXT DEFAULT '>',
            threshold REAL NOT NULL,
            action TEXT DEFAULT 'notify',
            device_id TEXT DEFAULT '',
            cooldown INTEGER DEFAULT 300,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS automation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER,
            rule_name TEXT,
            metric TEXT,
            value REAL,
            action TEXT,
            result TEXT,
            created_at TEXT
        )
    ''')
    conn.commit()
    conn.close()
    print("[规则引擎] 数据库初始化完成")


def _load_rules(enabled_only=False):
    try:
        conn = _get_conn()
        c = conn.cursor()
        sql = 'SELECT * FROM automation_rules'
        if enabled_only:
            sql += ' WHERE enabled = 1'
        c.execute(sql)
        rows = c.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _save_rule(data):
    """新增规则，返回 (rule_id, error)"""
    name = (data.get('name') or '').strip()
    metric = data.get('metric', '')
    threshold = data.get('threshold')
    action = data.get('action', 'notify')
    location = data.get('location', '卧室')
    operator = data.get('operator', '>')

    if not name or len(name) > 30:
        return None, '规则名必填且不超过30字'
    if metric not in VALID_METRICS:
        return None, f'metric 必须是 {list(VALID_METRICS)} 之一'
    if action not in VALID_ACTIONS:
        return None, f'action 必须是 {list(VALID_ACTIONS)} 之一'
    if operator not in ('>', '<'):
        return None, 'operator 必须是 > 或 <'
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return None, 'threshold 必须是数字'
    cooldown = int(data.get('cooldown', DEFAULT_COOLDOWN))
    cooldown = max(10, min(cooldown, 86400))

    conn = _get_conn()
    c = conn.cursor()
    c.execute('''
        INSERT INTO automation_rules
        (name, enabled, location, metric, operator, threshold, action, device_id, cooldown, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (name, 1 if data.get('enabled', True) else 0, location, metric, operator,
          threshold, action, (data.get('device_id') or '').strip(), cooldown,
          datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    rid = c.lastrowid
    conn.close()
    print(f"[规则] 新增 #{rid} {name}: {location} {metric} {operator} {threshold} -> {action}")
    return rid, None


def _log_trigger(rule, value, result):
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute('''
            INSERT INTO automation_logs (rule_id, rule_name, metric, value, action, result, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (rule['id'], rule['name'], rule['metric'], value, rule['action'], result,
              datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[规则] 日志写入失败: {e}")


# ==================== 引擎 ====================

def _avg_metric(location, metric):
    """指定位置在线设备某指标的平均值；无有效数据返回 None"""
    try:
        conn = get_sensors_db()
        cursor = conn.cursor()
        cursor.execute('SELECT temperature, humidity, lux, last_update_ts, location FROM sensor_latest')
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return None

    now = datetime.now()
    values = []
    for row in rows:
        if row['location'] != location:
            continue
        online = False
        if row['last_update_ts']:
            try:
                online = (now - datetime.fromisoformat(row['last_update_ts'])).total_seconds() < 30
            except Exception:
                online = False
        if not online:
            continue
        v = row[metric]
        if v is not None and v >= 0:
            values.append(v)
    if not values:
        return None
    return sum(values) / len(values)


def _first_online_device():
    try:
        conn = get_sensors_db()
        cursor = conn.cursor()
        cursor.execute('SELECT esp32_id, last_update_ts FROM sensor_latest')
        rows = cursor.fetchall()
        conn.close()
        now = datetime.now()
        for row in rows:
            if row['last_update_ts']:
                try:
                    if (now - datetime.fromisoformat(row['last_update_ts'])).total_seconds() < 30:
                        return row['esp32_id']
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _execute_action(rule, value):
    action = rule['action']
    if action == 'light_on':
        dev = rule.get('device_id') or _first_online_device()
        if not dev:
            return '无在线设备，未执行'
        cur = set_light_state(dev, on=True)
        return f"灯带 {dev[:8]} 已开启" if cur else '灯带控制失败'
    if action == 'light_off':
        dev = rule.get('device_id') or _first_online_device()
        if not dev:
            return '无在线设备，未执行'
        cur = set_light_state(dev, on=False)
        return f"灯带 {dev[:8]} 已关闭" if cur else '灯带控制失败'
    return '已记录告警（notify）'


def _check_once():
    for rule in _load_rules(enabled_only=True):
        value = _avg_metric(rule['location'], rule['metric'])
        if value is None:
            continue
        hit = (value > rule['threshold']) if rule['operator'] == '>' else (value < rule['threshold'])
        rid = rule['id']
        if hit:
            now = time.time()
            if rid in _last_trigger and now - _last_trigger[rid] < rule['cooldown']:
                continue  # 冷却中，防抖
            _last_trigger[rid] = now
            result = _execute_action(rule, value)
            _log_trigger(rule, value, result)
            print(f"[规则] {rule['name']} 触发: {rule['metric']}={value:.1f} -> {result}")
        else:
            _last_trigger.pop(rid, None)  # 条件恢复，解除冷却


def _engine_loop():
    global _last_check
    while _engine_running:
        try:
            _check_once()
        except Exception as e:
            print(f"[规则引擎] 检查异常: {e}")
        _last_check = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        time.sleep(ENGINE_INTERVAL)


def start_automation_engine():
    """启动规则引擎后台线程（app 启动时调用，幂等）"""
    global _engine_started, _engine_running
    if _engine_started:
        return
    init_automation_db()
    _engine_started = True
    _engine_running = True
    threading.Thread(target=_engine_loop, daemon=True).start()
    print(f"[规则引擎] 已启动（周期 {ENGINE_INTERVAL}s）")


# ==================== API 路由 ====================

@automation_bp.route('/api/admin/automation', methods=['GET'])
@admin_required
def api_automation_list():
    rules = _load_rules()
    for r in rules:
        r['enabled'] = bool(r['enabled'])
        r['in_cooldown'] = (r['id'] in _last_trigger and
                            time.time() - _last_trigger[r['id']] < r['cooldown'])
    return jsonify({
        'rules': rules,
        'engine': {
            'running': _engine_running,
            'interval': ENGINE_INTERVAL,
            'last_check': _last_check,
            'valid_metrics': list(VALID_METRICS.keys()),
            'valid_actions': list(VALID_ACTIONS),
        }
    })


@automation_bp.route('/api/admin/automation', methods=['POST'])
@admin_required
def api_automation_create():
    data = request.get_json() or {}
    rid, err = _save_rule(data)
    if err:
        return jsonify({'status': 'error', 'message': err}), 400
    return jsonify({'status': 'ok', 'id': rid})


@automation_bp.route('/api/admin/automation/<int:rid>', methods=['PUT'])
@admin_required
def api_automation_update(rid):
    data = request.get_json() or {}
    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM automation_rules WHERE id = ?', (rid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error', 'message': '规则不存在'}), 404
    r = dict(row)

    if 'enabled' in data:
        r['enabled'] = 1 if data['enabled'] else 0
    for key in ('name', 'location', 'metric', 'operator', 'action', 'device_id'):
        if key in data and data[key] not in (None, ''):
            r[key] = str(data[key]).strip()
    if 'threshold' in data:
        try:
            r['threshold'] = float(data['threshold'])
        except (TypeError, ValueError):
            conn.close()
            return jsonify({'status': 'error', 'message': 'threshold 必须是数字'}), 400
    if 'cooldown' in data:
        try:
            r['cooldown'] = max(10, min(int(data['cooldown']), 86400))
        except (TypeError, ValueError):
            pass

    c.execute('''
        UPDATE automation_rules SET name=?, enabled=?, location=?, metric=?,
        operator=?, threshold=?, action=?, device_id=?, cooldown=? WHERE id=?
    ''', (r['name'], r['enabled'], r['location'], r['metric'], r['operator'],
          r['threshold'], r['action'], r['device_id'], r['cooldown'], rid))
    conn.commit()
    conn.close()
    _last_trigger.pop(rid, None)   # 规则修改后重置冷却
    print(f"[规则] 更新 #{rid}")
    return jsonify({'status': 'ok'})


@automation_bp.route('/api/admin/automation/<int:rid>', methods=['DELETE'])
@admin_required
def api_automation_delete(rid):
    conn = _get_conn()
    c = conn.cursor()
    c.execute('DELETE FROM automation_rules WHERE id = ?', (rid,))
    conn.commit()
    conn.close()
    _last_trigger.pop(rid, None)
    print(f"[规则] 删除 #{rid}")
    return jsonify({'status': 'ok'})


@automation_bp.route('/api/admin/automation/logs', methods=['GET'])
@admin_required
def api_automation_logs():
    limit = min(int(request.args.get('limit', 20)), 100)
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute('SELECT * FROM automation_logs ORDER BY id DESC LIMIT ?', (limit,))
        rows = c.fetchall()
        conn.close()
        return jsonify({'logs': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

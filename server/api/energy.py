# -*- coding: utf-8 -*-
"""
能耗统计与用电画像（5.14）
=========================
数据源：`sensor_history.current_ma`（INA219 0.1Ω 采样，随传感器每 5s 落库一条）。
        只有接 INA219 的节点有值（当前 04B；68FE 为 -1）。

电量计算（论文口径）：
    功率 P(W)  = I(mA) × V(V) / 1000
    电量 E(kWh)= Σ P × Δt / 3600
    采样间隔 Δt = 5 s（固件上报周期；实际用条数×5s 近似，
    对丢包/重连造成的间隔偏差，日级统计误差 < 1%）

为什么电压取 5V：ESP32 开发板由 USB/DC 5V 供电，INA219 串在 5V 回路，
3.3V 是板载 LDO 降压后的结果，不参与电量计算。

⚠ 数据起点：2026-11-14 17:43 才开始落库（此前只上报不存储），
  历史用电无法回溯；相关图表需积累数日后才有意义。
"""
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

from core.auth import admin_required
from core.database import get_sensors_db

energy_bp = Blueprint('energy', __name__)

VOLTAGE_V = 5.0              # 供电电压（V）
SAMPLE_INTERVAL_S = 5        # 采样间隔（s）
ENERGY_DATA_START = '2026-11-14 17:43'   # 数据落库起点（用于前端提示）


def _kwh_from_ma(avg_ma, samples):
    """由平均电流与样本数估算电量（kWh）。"""
    if not avg_ma or avg_ma <= 0 or not samples:
        return 0.0
    seconds = samples * SAMPLE_INTERVAL_S
    watt = avg_ma * VOLTAGE_V / 1000.0
    return watt * seconds / 3600.0


def _query_range(start, end, device_id=None):
    """按时间范围取电流样本的聚合值。"""
    conn = get_sensors_db()
    sql = ('SELECT esp32_id, count(*) n, avg(current_ma) a, max(current_ma) mx, '
           'min(current_ma) mi FROM sensor_history '
           'WHERE current_ma > 0 AND timestamp >= ? AND timestamp < ?')
    args = [start, end]
    if device_id:
        sql += ' AND esp32_id = ?'
        args.append(device_id)
    sql += ' GROUP BY esp32_id'
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def _hourly_curve(day_start, day_end, device_id=None):
    """某天 24 小时的用电曲线（按小时聚合）。"""
    conn = get_sensors_db()
    sql = ("SELECT strftime('%H', timestamp) h, count(*) n, avg(current_ma) a "
           "FROM sensor_history WHERE current_ma > 0 AND timestamp >= ? AND timestamp < ?")
    args = [day_start, day_end]
    if device_id:
        sql += ' AND esp32_id = ?'
        args.append(device_id)
    sql += ' GROUP BY h ORDER BY h'
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out[r['h']] = (r['n'], r['a'])
    hours = []
    for h in range(24):
        key = '%02d' % h
        n, a = out.get(key, (0, 0))
        hours.append({
            'hour': key,
            'avg_ma': round(a, 1) if a else 0,
            'power_w': round(a * VOLTAGE_V / 1000.0, 3) if a else 0,
            'kwh': round(_kwh_from_ma(a, n), 5),
            'samples': n,
        })
    return hours


def _daily_curve(days):
    """近 N 天每天的用电量。"""
    today = datetime.now().date()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        ds = d.strftime('%Y-%m-%d 00:00:00')
        de = (d + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00')
        rows = _query_range(ds, de)
        total_kwh, total_samples, avg_sum = 0.0, 0, 0.0
        for r in rows:
            total_kwh += _kwh_from_ma(r['a'], r['n'])
            total_samples += r['n']
        avg_ma = 0.0
        if total_samples:
            conn = get_sensors_db()
            row = conn.execute(
                'SELECT avg(current_ma) a FROM sensor_history '
                'WHERE current_ma > 0 AND timestamp >= ? AND timestamp < ?',
                (ds, de)).fetchone()
            conn.close()
            avg_ma = row['a'] or 0.0
        out.append({
            'date': d.strftime('%Y-%m-%d'),
            'kwh': round(total_kwh, 4),
            'avg_ma': round(avg_ma, 1),
            'samples': total_samples,
        })
    return out


@energy_bp.route('/api/admin/energy/summary', methods=['GET'])
@admin_required
def api_energy_summary():
    """能耗总览：今日/本周/本月电量、平均功率、各设备排行、近 7 天曲线。"""
    try:
        days = max(1, min(int(request.args.get('days', 7)), 90))
    except (TypeError, ValueError):
        days = 7

    now = datetime.now()
    today_start = now.strftime('%Y-%m-%d 00:00:00')
    tomorrow_start = (now + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00')
    week_start = (now - timedelta(days=6)).strftime('%Y-%m-%d 00:00:00')
    month_start = now.strftime('%Y-%m-01 00:00:00')

    def total(start, end):
        rows = _query_range(start, end)
        kwh = 0.0
        samples = 0
        for r in rows:
            kwh += _kwh_from_ma(r['a'], r['n'])
            samples += r['n']
        conn = get_sensors_db()
        row = conn.execute(
            'SELECT avg(current_ma) a, max(current_ma) mx FROM sensor_history '
            'WHERE current_ma > 0 AND timestamp >= ? AND timestamp < ?',
            (start, end)).fetchone()
        conn.close()
        avg_ma = (row['a'] or 0.0) if row else 0.0
        peak_ma = (row['mx'] or 0.0) if row else 0.0
        return kwh, samples, avg_ma, peak_ma

    kwh_today, n_today, ma_today, peak_today = total(today_start, tomorrow_start)
    kwh_week, n_week, ma_week, _ = total(week_start, tomorrow_start)
    kwh_month, n_month, ma_month, _ = total(month_start, tomorrow_start)

    # 各设备用电排行（近 days 天）
    range_start = (now - timedelta(days=days - 1)).strftime('%Y-%m-%d 00:00:00')
    rows = _query_range(range_start, tomorrow_start)
    devices = []
    for r in rows:
        devices.append({
            'device_id': r['esp32_id'],
            'kwh': round(_kwh_from_ma(r['a'], r['n']), 4),
            'avg_ma': round(r['a'] or 0.0, 1),
            'peak_ma': round(r['mx'] or 0.0, 1),
            'samples': r['n'],
        })
    devices.sort(key=lambda x: x['kwh'], reverse=True)

    return jsonify({
        'status': 'ok',
        'voltage_v': VOLTAGE_V,
        'sample_interval_s': SAMPLE_INTERVAL_S,
        'data_start': ENERGY_DATA_START,
        'today': {
            'kwh': round(kwh_today, 4),
            'avg_ma': round(ma_today, 1),
            'avg_power_w': round(ma_today * VOLTAGE_V / 1000.0, 3),
            'peak_ma': round(peak_today, 1),
            'samples': n_today,
        },
        'week': {'kwh': round(kwh_week, 4), 'avg_ma': round(ma_week, 1), 'samples': n_week},
        'month': {'kwh': round(kwh_month, 4), 'avg_ma': round(ma_month, 1), 'samples': n_month},
        'devices': devices,
        'daily': _daily_curve(min(days, 30)),
    })


@energy_bp.route('/api/admin/energy/daily', methods=['GET'])
@admin_required
def api_energy_daily():
    """某一天的 24 小时用电曲线。参数 date=YYYY-MM-DD（默认今天）。"""
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'status': 'error', 'message': 'date 格式应为 YYYY-MM-DD'}), 400

    day_start = d.strftime('%Y-%m-%d 00:00:00')
    day_end = (d + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00')
    device_id = request.args.get('device_id') or None

    hours = _hourly_curve(day_start, day_end, device_id)
    rows = _query_range(day_start, day_end, device_id)
    kwh, samples = 0.0, 0
    detail = []
    for r in rows:
        k = _kwh_from_ma(r['a'], r['n'])
        kwh += k
        samples += r['n']
        detail.append({
            'device_id': r['esp32_id'],
            'kwh': round(k, 4),
            'avg_ma': round(r['a'] or 0.0, 1),
            'peak_ma': round(r['mx'] or 0.0, 1),
            'samples': r['n'],
        })

    return jsonify({
        'status': 'ok',
        'date': date_str,
        'kwh': round(kwh, 4),
        'samples': samples,
        'hours': hours,
        'devices': detail,
    })


@energy_bp.route('/api/admin/energy/realtime', methods=['GET'])
@admin_required
def api_energy_realtime():
    """实时功率（各节点最新电流与瞬时功率）。"""
    conn = get_sensors_db()
    rows = conn.execute(
        'SELECT esp32_id, location, current_ma, last_update FROM sensor_latest').fetchall()
    conn.close()
    out = []
    total_w = 0.0
    for r in rows:
        ma = r['current_ma'] if r['current_ma'] is not None else -1.0
        w = (ma * VOLTAGE_V / 1000.0) if ma > 0 else 0.0
        total_w += w
        out.append({
            'device_id': r['esp32_id'],
            'location': r['location'],
            'current_ma': round(ma, 1),
            'power_w': round(w, 3),
            'last_update': r['last_update'],
            'has_ina219': ma > 0,
        })
    return jsonify({
        'status': 'ok',
        'voltage_v': VOLTAGE_V,
        'total_power_w': round(total_w, 3),
        'devices': out,
    })

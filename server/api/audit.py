# -*- coding: utf-8 -*-
"""
操作审计 API（5.13）
====================
- GET  /api/admin/audit         分页查询（按来源/设备/动作/结果/时间筛选）
- GET  /api/admin/audit/stats   概览统计（总量/今日/失败/已撤销/按来源分布）
- POST /api/admin/action/undo   撤销可逆动作（灯带 / 米家插座）

审计数据的写入在 `link/downlink.send_downlink()` 统一收口；
本模块只负责查询与撤销。
"""
import json

from flask import Blueprint, request, jsonify

from core.auth import admin_required
from core import audit

audit_bp = Blueprint('audit', __name__)


@audit_bp.route('/api/admin/audit', methods=['GET'])
@admin_required
def api_query():
    """分页查询操作日志。

    参数：source / device_id / action / result / since / limit / offset
    """
    try:
        limit = int(request.args.get('limit', 50))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = int(request.args.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0

    data = audit.query(
        source=request.args.get('source') or None,
        device_id=request.args.get('device_id') or None,
        action=request.args.get('action') or None,
        result=request.args.get('result') or None,
        since=request.args.get('since') or None,
        limit=limit,
        offset=offset,
    )
    # params 是 JSON 字符串，前端展示时解析成对象更友好
    for it in data['items']:
        try:
            it['params_obj'] = json.loads(it.get('params') or '{}')
        except Exception:
            it['params_obj'] = {}
    return jsonify({'status': 'ok', **data, 'limit': limit, 'offset': offset})


@audit_bp.route('/api/admin/audit/stats', methods=['GET'])
@admin_required
def api_stats():
    return jsonify({'status': 'ok', **audit.stats()})


@audit_bp.route('/api/admin/action/undo', methods=['POST'])
@admin_required
def api_undo():
    """撤销一条可逆操作。

    请求：{"id": <operation_log.id>}
    可逆动作：灯带（开关/颜色/亮度/数量）、米家插座（开关）
    """
    data = request.get_json() or {}
    try:
        op_id = int(data.get('id'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': '缺少或非法的 id'}), 400

    op = audit.get_operation(op_id)
    if not op:
        return jsonify({'status': 'error', 'message': f'操作 {op_id} 不存在'}), 404
    if op.get('undone'):
        return jsonify({'status': 'error', 'message': '该操作已被撤销过'}), 400

    action = op.get('action') or ''
    device_id = op.get('device_id') or ''

    # 可逆动作白名单（防止误撤销 reboot/ota 等不可逆操作）
    if action == 'light_set':
        return _undo_light(op, device_id)
    if action == 'mi_control':
        return _undo_mi(op, device_id)
    return jsonify({
        'status': 'error',
        'message': f'动作 {action} 不支持撤销（仅灯带/米家插座等可逆动作支持）'
    }), 400


def _undo_light(op, device_id):
    from api.light import set_light_state
    snap = audit.take_snapshot(device_id, 'light')
    if not snap:
        return jsonify({'status': 'error', 'message': '没有可用的状态快照，无法撤销'}), 400
    st = snap['state'] or {}
    try:
        from core import audit as _audit
        with _audit.audit_context('web', 'undo'):
            cur = set_light_state(
                device_id,
                on=st.get('on'),
                r=st.get('r'), g=st.get('g'), b=st.get('b'),
                brightness=st.get('brightness'),
                led_count=st.get('led_count'))
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'恢复灯带失败: {e}'}), 500
    undo_id = audit.log_operation('web', 'undo', device_id,
                                  {'type': 'light', 'undo': True, **st}, 'ok',
                                  'undo', 0, undo_of=op['id'])
    audit.mark_undone(op['id'], undo_id)
    return jsonify({'status': 'ok', 'message': '已恢复到操作前状态',
                    'restored': st, 'undo_log_id': undo_id})


def _undo_mi(op, device_id):
    """米家插座撤销：device_id 形如 'oit_cb7' 或 'oit_cb7:8'。"""
    try:
        from api.mi import set_mi_target
    except Exception:
        return jsonify({'status': 'error', 'message': '米家模块不可用'}), 500
    snap = audit.take_snapshot(device_id, 'mi')
    if not snap:
        return jsonify({'status': 'error', 'message': '没有可用的状态快照，无法撤销'}), 400
    st = snap['state'] or {}
    key = str(device_id).split(':')[0]
    target = 'main'
    if ':' in str(device_id):
        target = str(device_id).split(':', 1)[1]
    try:
        from core import audit as _audit
        with _audit.audit_context('web', 'undo'):
            set_mi_target(key, bool(st.get('on')), target=target, force=True)
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'恢复插座失败: {e}'}), 500
    undo_id = audit.log_operation('web', 'undo', device_id,
                                  {'type': 'mi', 'undo': True, **st}, 'ok',
                                  'undo', 0, undo_of=op['id'])
    audit.mark_undone(op['id'], undo_id)
    return jsonify({'status': 'ok', 'message': '已恢复到操作前状态',
                    'restored': st, 'undo_log_id': undo_id})

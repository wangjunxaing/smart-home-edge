# -*- coding: utf-8 -*-
"""
离线告警与自愈 / 通知配置 — Blueprint（5.7）
===========================================
接口
----
- `GET  /api/esp32/alerts`            告警摘要（公开只读，页面轮询）
- `GET  /api/admin/selfheal`          自愈详情 + 参数
- `POST /api/admin/device/reboot`     管理员手动远控重启
- `POST /api/admin/selfheal/check`    立即执行一轮巡检（调试/演示用）
- `POST /api/admin/selfheal/clear`    清除某设备告警状态
- `GET  /api/admin/notify/config`     查看通知配置（密钥掩码）
- `POST /api/admin/notify/config`     保存通知配置
- `POST /api/admin/notify/test`       发送测试通知（联调用）
"""
from flask import Blueprint, request, jsonify

from core.auth import admin_required
from core import notifier
from core import self_heal

selfheal_bp = Blueprint('selfheal', __name__)


@selfheal_bp.route('/api/esp32/alerts', methods=['GET'])
def api_alerts():
    """告警摘要（页面轮询）"""
    return jsonify(self_heal.snapshot())


@selfheal_bp.route('/api/admin/selfheal', methods=['GET'])
@admin_required
def api_selfheal_detail():
    return jsonify({'ok': True, 'selfheal': self_heal.snapshot(),
                    'notify': notifier.config_masked()})


@selfheal_bp.route('/api/admin/device/reboot', methods=['POST'])
@admin_required
def api_device_reboot():
    data = request.get_json(silent=True) or {}
    did = (data.get('device_id') or '').strip()
    if not did:
        return jsonify({'ok': False, 'error': '缺少 device_id'}), 400
    ch = self_heal.manual_reboot(did)
    return jsonify({'ok': bool(ch), 'channel': ch,
                    'message': f'已通过 {ch} 下发重启' if ch else '下发失败（MQTT 与 ESP-NOW 均不可用）'})


@selfheal_bp.route('/api/admin/selfheal/check', methods=['POST'])
@admin_required
def api_selfheal_check():
    self_heal.check_once()
    return jsonify({'ok': True, 'selfheal': self_heal.snapshot()})


@selfheal_bp.route('/api/admin/selfheal/clear', methods=['POST'])
@admin_required
def api_selfheal_clear():
    data = request.get_json(silent=True) or {}
    did = (data.get('device_id') or '').strip()
    if not did:
        return jsonify({'ok': False, 'error': '缺少 device_id'}), 400
    self_heal.clear_device(did)
    return jsonify({'ok': True, 'selfheal': self_heal.snapshot()})


@selfheal_bp.route('/api/admin/selfheal/simulate', methods=['POST'])
@admin_required
def api_selfheal_simulate():
    """**仅供联调/论文演示**：临时把某设备伪装为"离线"若干秒，用于安全地演示
    完整的自动自愈链路（不动真实硬件）。到时自动失效，也可提前撤销。

    请求体：{"device_id": "...", "seconds": 90}；seconds=0 表示立即撤销。
    """
    data = request.get_json(silent=True) or {}
    did = (data.get('device_id') or '').strip()
    if not did:
        return jsonify({'ok': False, 'error': '缺少 device_id'}), 400
    try:
        seconds = float(data.get('seconds', 90))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'seconds 必须是数字'}), 400
    if seconds < 0 or seconds > 600:
        return jsonify({'ok': False, 'error': 'seconds 取值范围 0~600'}), 400
    self_heal.simulate_offline(did, seconds)
    return jsonify({'ok': True, 'until': self_heal.simulated_until(did),
                    'message': (f'已模拟 {did} 离线 {seconds:.0f}s' if seconds > 0
                                else f'已撤销 {did} 的模拟离线')})


# ==================== 通知配置 ====================

@selfheal_bp.route('/api/admin/notify/config', methods=['GET'])
@admin_required
def api_notify_get():
    return jsonify({'ok': True, 'config': notifier.config_masked()})


@selfheal_bp.route('/api/admin/notify/config', methods=['POST'])
@admin_required
def api_notify_save():
    data = request.get_json(silent=True) or {}
    cur = notifier.load_config()
    # 掩码值表示"保持原值不变"，避免前端回显覆盖真实密钥
    for ch in ('feishu', 'wecom', 'dingtalk'):
        if isinstance(data.get(ch), dict):
            for k in ('webhook', 'secret'):
                if data[ch].get(k) == '***已配置***':
                    data[ch][k] = (cur.get(ch) or {}).get(k, '')
    if isinstance(data.get('email'), dict) and data['email'].get('password') == '***已配置***':
        data['email']['password'] = (cur.get('email') or {}).get('password', '')
    notifier.save_config(data)
    return jsonify({'ok': True, 'config': notifier.config_masked()})


@selfheal_bp.route('/api/admin/notify/test', methods=['POST'])
@admin_required
def api_notify_test():
    data = request.get_json(silent=True) or {}
    title = data.get('title') or '0-Smart 测试通知'
    text = data.get('text') or ('这是一条测试告警，用于验证通知渠道配置。\n'
                                '收到即表示渠道可用。')
    results = notifier.notify(title, text, level='info', force=True)
    ok = any(v[0] for v in results.values() if isinstance(v, tuple))
    return jsonify({'ok': ok, 'results': {k: {'ok': v[0], 'msg': v[1]}
                                          for k, v in results.items()}})

# -*- coding: utf-8 -*-
"""
系统可观测性接口（5.5）— Blueprint
==================================
  GET /api/health       公开：只给状态与计数（供外部探活、前端顶部指示灯）
  GET /api/admin/system 管理员：主机/服务/节点/链路明细（仪表盘"服务器状态"卡片）

设计说明：公开接口**故意不暴露**磁盘用量、网络字节、进程等敏感细节；
需要明细必须登录且为 admin（复用 core.auth.admin_required）。
"""
from flask import Blueprint, jsonify

from core.auth import admin_required
from core import metrics

system_bp = Blueprint('system', __name__)


@system_bp.route('/api/health', methods=['GET'])
def api_health():
    return jsonify(metrics.health_summary())


@system_bp.route('/api/admin/system', methods=['GET'])
@admin_required
def api_admin_system():
    return jsonify(metrics.system_snapshot())

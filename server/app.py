#! /usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orange Pi 服务器主程序 — 环境监控与 AI 智能家居控制中心
======================================================
运行环境: Orange Pi 5 Pro (RK3588), Python 3, Flask
监听端口: 8080

模块化架构（目录重排后）:
  core/            基础设施
    config.py         — 集中式路径配置（唯一路径来源）
    database.py       — 数据库连接与表初始化
    config_manager.py — ESP32 动态配置管理
    auth.py           — 用户认证 (Blueprint)
    llm_manager.py    — 大模型 UDS 代理 (Blueprint)
    tool_use.py       — LLM 工具调用注册表与执行
  api/             业务 Blueprint
    sensors.py        — 传感器数据 API
    light.py / ir.py / tts.py   — 灯带 / 红外 / 语音播报
    automation.py     — 自动化规则引擎
    firmware.py       — OTA 固件托管（签名 + 审计）
    speech.py         — Vosk 语音识别 + 麦克风控制
    camera.py         — USB 摄像头视频流
    system.py         — 可观测性（/api/health、/api/admin/system）
  link/            链路层
    mqtt_bridge.py    — MQTT 桥
    espnow_link.py    — ESP-NOW 链路（网关控制口）
    usb_gateway.py    — ESP-NOW USB 网关守护（独立进程）
    downlink.py       — 统一下行路由（通道自适应 + ACK 重发）
    link_state.py     — 链路状态与下发可靠性
  native/          C/C++ 源码与编译产物（llm_proxy / sensor_history / rkllm-demo）
"""
import os
import sys
import signal
import threading
from flask import (Flask, request, render_template, redirect,
                   url_for, session, send_from_directory, Response)

# ---- 内部模块 ----
from core.config import ensure_dirs, SERVER_DIR
from core.database import init_db
from core.auth import auth_bp, login_required
from core.llm_manager import llm_bp, start_proxy, LLM_PROXY_PATH, LLM_DEMO_PATH
from api.camera import get_camera, generate_frames, release_camera
from api.sensors import sensors_bp
from api.speech import speech_bp, init_vosk
from api.firmware import firmware_bp
from api.light import light_bp
from api.ir import ir_bp
from api.automation import automation_bp, start_automation_engine
from api.tts import tts_bp
from api.system import system_bp
from api.mi import mi_bp, start_refresh_thread
from api.selfheal import selfheal_bp
from api.energy import energy_bp
from api.audit import audit_bp
from core.audit import init_db as init_audit_db
from core.self_heal import start as start_self_heal
from link.mqtt_bridge import start_bridge as start_mqtt_bridge
from link.espnow_link import start_link as start_espnow_link
from link.downlink import start as start_downlink

# 静态资源根目录（与 app.py 同级，避免受 CWD 影响）
_STATIC_DIR = str(SERVER_DIR / 'static')
_TEMPLATE_DIR = str(SERVER_DIR / 'templates')

# ==================== Flask 应用初始化 ====================
app = Flask(__name__, static_folder=_STATIC_DIR, template_folder=_TEMPLATE_DIR)
app.secret_key = os.environ.get('SMART_SECRET_KEY', 'smart-home-dev-secret-change-me')

app.register_blueprint(auth_bp)
app.register_blueprint(llm_bp)
app.register_blueprint(sensors_bp)
app.register_blueprint(speech_bp)
app.register_blueprint(firmware_bp)
app.register_blueprint(light_bp)
app.register_blueprint(ir_bp)
app.register_blueprint(automation_bp)
app.register_blueprint(tts_bp)
app.register_blueprint(system_bp)
app.register_blueprint(mi_bp)
app.register_blueprint(selfheal_bp)
app.register_blueprint(energy_bp)
app.register_blueprint(audit_bp)        # 5.13 操作审计


# ==================== 页面路由 ====================

@app.route('/')
def index():
    return redirect(url_for('register_page'))


@app.route('/register.html')
def register_page():
    return render_template('register.html')


@app.route('/main.html')
@login_required
def main_page():
    return render_template('main.html', username=session.get('username'),
                           is_admin=session.get('role') == 'admin')


@app.route('/reg_result.html')
def reg_result_page():
    status = request.args.get('status', 'error')
    msg = request.args.get('msg', '')
    return render_template('reg_result.html', status=status, msg=msg)


# ==================== 静态文件服务 ====================

@app.route('/css/<path:filename>')
def serve_css(filename):
    return send_from_directory(os.path.join(_STATIC_DIR, 'css'), filename)


@app.route('/js/<path:filename>')
def serve_js(filename):
    return send_from_directory(os.path.join(_STATIC_DIR, 'js'), filename)


@app.route('/img/<path:filename>')
def serve_img(filename):
    return send_from_directory(os.path.join(_STATIC_DIR, 'img'), filename)


# ==================== 摄像头视频流 ====================

@app.route('/video_feed')
def video_feed():
    if not session.get('logged_in'):
        return "Please login first", 401
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


# ==================== 启动流程 ====================

def signal_handler(sig, frame):
    print("\n[Server] 收到关闭信号，正在停止服务器...")
    release_camera()
    sys.exit(0)


if __name__ == '__main__':
    ensure_dirs()               # data/ firmware/ keys/ 等运行期目录（幂等）
    init_db()
    init_audit_db()             # 5.13 操作审计表（operation_log / action_snapshot）
    init_vosk()
    start_automation_engine()   # 自动化规则引擎（后台线程，幂等）
    start_mqtt_bridge()         # MQTT 桥（5.1：订阅 ESP32 MQTT 上报）
    start_espnow_link()         # ESP-NOW 链路（5.4：网关控制口事件订阅/下行）
    start_refresh_thread()      # 米家 serviceToken 自动续期（5.6：passToken 长期凭据）
    start_self_heal()           # 离线告警与远控重启自愈（5.7）
    start_downlink()            # 统一下行路由（通道自适应 + ACK 确认重发）

    # 优先使用 C++ llm_proxy；回退到检查 llm_demo 直接启动
    if os.path.isfile(LLM_PROXY_PATH):
        print("[LLM] 检测到 C++ 代理，后台启动...")
        threading.Thread(target=start_proxy, daemon=True).start()
    elif os.path.isfile(LLM_DEMO_PATH):
        print("[LLM] llm_proxy 未编译但检测到 llm_demo")
        print("[LLM] 请运行 make 编译代理后重启以启用 AI 对话")
    else:
        print("[LLM] 未检测到大模型文件，跳过加载")

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 50)
    print("[Server] Python HTTP 服务器启动")
    print("[Server] 监听端口: 8080")
    print("=" * 50)
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
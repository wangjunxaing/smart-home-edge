# -*- coding: utf-8 -*-
"""
大模型集成模块 — UDS 代理方案 + Blueprint
========================================
通过 C++ llm_proxy 管理 PTY 与 llm_demo 的交互。
Python 侧只负责 UDS 客户端连接和 SSE 流式响应。

架构:
  Flask → UDS (/tmp/llm_proxy.sock) → C++ proxy → PTY → llm_demo
  C++ proxy 负责: PTY 管理、输出过滤、chat log 写入
"""
import os
import json
import socket
import time
import threading
from flask import Blueprint, request, jsonify, Response, stream_with_context

from core.config import (LLM_PROXY_BIN, RKLLM_DEMO_BIN, LLM_MODEL,
                         RKLLM_LIB_DIR, LLM_CHAT_LOG)

llm_bp = Blueprint('llm', __name__)

SOCK_PATH = '/tmp/llm_proxy.sock'
LLM_PROXY_PATH = str(LLM_PROXY_BIN)
LLM_DEMO_PATH = str(RKLLM_DEMO_BIN)
LLM_MODEL_PATH = str(LLM_MODEL)
CHAT_LOG_PATH = str(LLM_CHAT_LOG)

# 状态
proxy_started = False
llm_lock = threading.Lock()


def _uds_request(cmd_obj, timeout=45):
    """通过 UDS 发送 JSON 请求并逐帧读取响应（生成器）"""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(SOCK_PATH)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        yield {"type": "error", "msg": f"代理未启动: {e}"}
        return

    try:
        payload = json.dumps(cmd_obj, ensure_ascii=False) + '\n'
        sock.sendall(payload.encode('utf-8'))

        buf = b''
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    if not line:
                        continue
                    try:
                        frame = json.loads(line.decode('utf-8'))
                        yield frame
                    except json.JSONDecodeError:
                        continue
                    if frame.get('type') in ('done', 'error'):
                        return
            except socket.timeout:
                break
    finally:
        sock.close()


def _ask_llm_stream(prompt):
    """向大模型发送问题，生成器方式流式返回 (通过 UDS 代理)"""
    for frame in _uds_request({"cmd": "chat", "msg": prompt}):
        tp = frame.get('type', '')
        if tp == 'token':
            yield f"data: {frame.get('text', '')}\n\n"
        elif tp == 'error':
            yield f"data: [ERROR] {frame.get('msg', '代理错误')}\n\n"
        elif tp == 'done':
            return


def _ask_llm_full(prompt, timeout=45):
    """非流式：完整收集模型回复文本（工具决策轮使用，需解析 [TOOL] 标记）"""
    parts = []
    for frame in _uds_request({"cmd": "chat", "msg": prompt}, timeout=timeout):
        tp = frame.get('type', '')
        if tp == 'token':
            parts.append(frame.get('text', ''))
        elif tp == 'error':
            parts.append(f"[ERROR] {frame.get('msg', '代理错误')}")
        elif tp == 'done':
            break
    return ''.join(parts)


def chat_with_tools(message):
    """工具调用闭环（双层意图解析 + 模板回复）：
    规则优先命中 → 直接执行；否则 LLM 决策轮解析 [TOOL] 协议；
    工具执行后由模板生成回复（确定性 100%），LLM 只负责纯闲聊。
    工具协议由 llm_demo 的 system prompt 内置（JSON 工具模式）。"""
    from core.tool_use import (extract_tool_calls, execute_tool,
                               parse_intent, format_reply)

    yield "data: [START]\n\n"

    # ---- 第 0 层：规则引擎优先（稳定兜底，1.5B 模型自觉性不足）----
    ruled = parse_intent(message)
    if ruled:
        name, args = ruled
        result = execute_tool(name, args)
        print(f"[规则] {name} -> {str(result)[:100]}")
        yield f"data: [TOOL] 正在调用 {name}...\n\n"
        yield f"data: {format_reply(name, result, message)}\n\n"
        yield "data: [DONE]\n\n"
        return

    # ---- 第一轮：LLM 决策轮（system prompt 已内置工具协议）----
    # 2026-11-16：xLAM 推理约 3 秒，给用户一个"正在思考"的反馈，避免白屏
    yield "data: [THINK] 正在思考...\n\n"
    decision = _ask_llm_full(message)
    calls = extract_tool_calls(decision)

    if not calls:
        # 纯闲聊：LLM 输出即最终回复
        if decision.strip():
            yield f"data: {decision}\n\n"
        yield "data: [DONE]\n\n"
        return

    # ---- 执行 LLM 请求的工具（只取第一个，防止误触发多个）----
    call = calls[0]
    name = call.get('name', '')
    args = call.get('args') or {}
    result = execute_tool(name, args)
    print(f"[LLM工具] {name} -> {str(result)[:100]}")
    yield f"data: [TOOL] 正在调用 {name}...\n\n"
    yield f"data: {format_reply(name, result, message)}\n\n"
    yield "data: [DONE]\n\n"


def start_proxy():
    """启动 C++ llm_proxy 进程（后台 daemon）"""
    global proxy_started
    if proxy_started:
        return

    import sys
    import subprocess

    if not os.path.isfile(LLM_PROXY_PATH):
        print("[LLM] llm_proxy 未编译，跳过启动")
        return

    if not os.path.isfile(LLM_DEMO_PATH):
        print("[LLM] llm_demo 不存在，跳过启动")
        return

    lib_dir = str(RKLLM_LIB_DIR)
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = lib_dir + ':' + env.get('LD_LIBRARY_PATH', '')
    env['RKLLM_LOG_LEVEL'] = '1'

    print("[LLM] 启动 C++ PTY 代理...")
    try:
        subprocess.Popen(
            [LLM_PROXY_PATH, LLM_DEMO_PATH, LLM_MODEL_PATH, CHAT_LOG_PATH],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT
        )
        proxy_started = True
        print("[LLM] 代理进程已启动，等待就绪...")
        # 等待代理就绪
        for _ in range(30):
            time.sleep(2)
            for frame in _uds_request({"cmd": "status"}, timeout=5):
                if frame.get('type') == 'status' and frame.get('ready'):
                    print("[LLM] 代理就绪，大模型可用")
                    return
        print("[LLM] 代理启动超时")
    except Exception as e:
        print(f"[LLM] 代理启动失败: {e}")


def get_llm_status():
    """查询模型状态 (非流式)"""
    for frame in _uds_request({"cmd": "status"}, timeout=5):
        if frame.get('type') == 'status':
            return 'running' if frame.get('ready') else 'stopped'
    return 'stopped'


# ==================== LLM API 路由 ====================

@llm_bp.route('/api/llm/chat', methods=['POST'])
def api_llm_chat():
    data = request.get_json()
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': '消息不能为空'}), 400

    print(f"[LLM] 收到问题: {message[:50]}...")

    def generate():
        for chunk in chat_with_tools(message):
            yield chunk

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@llm_bp.route('/api/llm/status', methods=['GET'])
def api_llm_status():
    return jsonify({'status': get_llm_status()})


@llm_bp.route('/api/llm/start', methods=['POST'])
def api_llm_start():
    threading.Thread(target=start_proxy, daemon=True).start()
    return jsonify({'success': True, 'message': '代理启动中...'})


@llm_bp.route('/api/llm/restart', methods=['POST'])
def api_llm_restart():
    # 通过 UDS 发送 restart 命令
    for frame in _uds_request({"cmd": "restart"}, timeout=10):
        if frame.get('type') == 'done':
            return jsonify({'success': True, 'message': '模型重启中...'})
    # 如果代理不在运行，尝试直接启动
    global proxy_started
    proxy_started = False
    threading.Thread(target=start_proxy, daemon=True).start()
    return jsonify({'success': True, 'message': '代理重启中...'})
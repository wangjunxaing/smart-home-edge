# -*- coding: utf-8 -*-
"""
集中式路径配置（server/ 目录重排后新增）
========================================
所有运行期路径统一从这里取，避免各模块各自用
``os.path.dirname(os.path.abspath(__file__))`` 拼相对路径 ——
模块一旦移动目录，那种写法就会静默指向错误位置。

目录约定（根 = ``server/``）::

    server/
      app.py            Flask 入口
      core/             database / auth / config_manager / llm_manager / tool_use
      api/              Flask Blueprint（sensors / light / ir / tts / firmware / ...）
      link/             MQTT 桥 + ESP-NOW 链路 + 统一下行路由
      native/           C/C++ 源码与编译产物（llm_proxy / sensor_history / rkllm-demo）
      data/             运行时数据（SQLite、状态 json、日志）
      models/           大模型文件（.rkllm / vosk）
      deploy/           systemd 单元、net_watchdog 加固配置、SSH 部署密钥
      scripts/          测量脚本（measure）/ 运维脚本（ops）
      static/ templates/  前端资源
"""
import os
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]

# ---- 目录 ----
DATA_DIR = SERVER_DIR / 'data'
NATIVE_DIR = SERVER_DIR / 'native'
MODELS_DIR = SERVER_DIR / 'models'
SCRIPTS_DIR = SERVER_DIR / 'scripts'
FIRMWARE_DIR = SERVER_DIR / 'firmware'      # 运行时生成（OTA bin / manifest / audit.log）
KEYS_DIR = SERVER_DIR / 'keys'              # 运行时生成（OTA 签名私钥，不入库）
OTA_PRIV_KEY = KEYS_DIR / 'ota_rsa_priv.pem'
AUDIO_DEBUG_DIR = SERVER_DIR / 'audio_debug'
ESPNOW_PROTO_DIR = SCRIPTS_DIR / 'measure'  # espnow_proto.py 所在目录（网关与测量脚本共用）

# ---- 服务器外部的落盘位置（与测量脚本/运维约定一致）----
AB_RESULTS_DIR = Path('/home/ubuntu/ab_results')
GATEWAY_STATS_PATH = AB_RESULTS_DIR / 'espnow_gateway_stats.json'

# ---- 运行时数据 ----
USERS_DB = DATA_DIR / 'users.db'
SENSORS_DB = DATA_DIR / 'sensors_history.db'
AUTOMATION_DB = DATA_DIR / 'automation.db'
LIGHT_STATE = DATA_DIR / 'light_state.json'
IR_STATE = DATA_DIR / 'ir_state.json'
VOICE_LOG = DATA_DIR / 'voice_log.txt'
SENSOR_ERROR_LOG = DATA_DIR / 'sensor_errors.log'
LLM_CHAT_LOG = DATA_DIR / 'llm_chat.log'

# ---- 米家（5.6）：设备表 + 云端会话凭据 ----
MI_DEVICES = DATA_DIR / 'mi_devices.json'
MI_SESSION = DATA_DIR / 'mi_session.json'

# ---- 离线告警与自愈（5.7）----
ALERTS_STATE = DATA_DIR / 'alerts.json'          # 告警事件与设备自愈状态
NOTIFY_CONFIG = DATA_DIR / 'notify_config.json'  # 通知渠道配置（含 webhook/授权码）

# ---- 本地大模型 ----
# 6.1: 已切换到 xLAM-2-3B（专训 function calling，实测 94% 工具调用准确率）
LLM_MODEL = MODELS_DIR / 'xLAM-2-3B-fc-r_w8a8_rk3588.rkllm'
VOSK_MODEL = MODELS_DIR / 'vosk-model' / 'vosk-model-small-cn-0.22'

# ---- 原生组件（native/Makefile 的产物） ----
LLM_PROXY_BIN = NATIVE_DIR / 'llm_proxy'
SENSOR_HISTORY_BIN = NATIVE_DIR / 'sensor_history'
RKLLM_DEMO_BIN = NATIVE_DIR / 'rkllm-demo' / 'llm_demo'
RKLLM_LIB_DIR = NATIVE_DIR / 'rkllm-demo' / 'lib'


def ensure_dirs():
    """首次运行时补齐必须存在的目录（幂等）"""
    for d in (DATA_DIR, FIRMWARE_DIR, KEYS_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            print(f'[配置] 目录创建失败 {d}: {e}')

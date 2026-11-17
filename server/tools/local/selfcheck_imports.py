# -*- coding: utf-8 -*-
"""
自检：所有 Python 模块能否正常导入、路径常量是否指向真实位置、5.5 指标采集能否跑通。

跨平台：脚本位于 `server/tools/local/`，用 `__file__` 反推服务器根目录，
因此在 Windows 与香橙派（`/home/ubuntu/server`）上都能直接运行：

    python tools/local/selfcheck_imports.py            # 本机（第三方库缺失时自动打桩）
    /home/ubuntu/ai_env/bin/python3 tools/local/selfcheck_imports.py   # Pi（用真实依赖）

缺少 flask/paho/cv2/vosk/serial 时注入最小桩对象，让"导入与装配"检查在没有依赖的机器上也能跑。
"""
import importlib
import sys
import types
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2]      # tools/local/ → server/
sys.path.insert(0, str(SERVER))


class _Any:
    """任意属性都能取到，且可调用（桩对象）"""

    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Any()

    def __getattr__(self, name):
        return _Any()

    def __iter__(self):
        return iter(())

    def __getitem__(self, k):
        return _Any()


def stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# ---- 第三方依赖：能用真的就用真的，装不上才打桩（Pi 上跑时不会覆盖真实库）----
stubbed = []


def need(name):
    """返回 True 表示该依赖缺失、需要打桩"""
    try:
        importlib.import_module(name)
        return False
    except ImportError:
        return True


if need('flask'):
    stub('flask', Flask=_Any, Blueprint=_Any, request=_Any(), session=_Any(),
         jsonify=_Any, Response=_Any, send_file=_Any, redirect=_Any,
         render_template=_Any, url_for=_Any, send_from_directory=_Any,
         stream_with_context=_Any)
    stubbed.append('flask')
if need('paho.mqtt.client'):
    paho = stub('paho'); mqtt_pkg = stub('paho.mqtt'); stub('paho.mqtt.client', Client=_Any)
    paho.mqtt = mqtt_pkg
    stubbed.append('paho')
if need('cv2'):
    stub('cv2', VideoCapture=_Any, CAP_PROP_FRAME_WIDTH=0, CAP_PROP_FRAME_HEIGHT=0,
         IMWRITE_JPEG_QUALITY=0, imencode=_Any)
    stubbed.append('cv2')
if need('vosk'):
    stub('vosk', Model=_Any, KaldiRecognizer=_Any, SetLogLevel=_Any, Recognizer=_Any)
    stubbed.append('vosk')
if need('serial'):
    stub('serial', Serial=_Any, SerialException=Exception)
    stubbed.append('serial')

print(f'依赖来源：{"全部为真实依赖" if not stubbed else "以下缺失已打桩: " + ", ".join(stubbed)}')

MODULES = [
    'core.config', 'core.database', 'core.config_manager', 'core.auth',
    'core.tool_use', 'core.llm_manager', 'core.metrics',
    'api.light', 'api.ir', 'api.tts', 'api.sensors', 'api.automation',
    'api.firmware', 'api.camera', 'api.speech', 'api.system',
    'link.link_state', 'link.mqtt_bridge', 'link.espnow_link', 'link.downlink',
    'link.usb_gateway',
]

fail = 0
for name in MODULES:
    try:
        importlib.import_module(name)
        print(f'[OK]   {name}')
    except Exception as e:
        fail += 1
        print(f'[FAIL] {name}: {type(e).__name__}: {e}')

print('\n---- 关键路径检查 ----')
cfg = importlib.import_module('core.config')
EXPECT = {
    'USERS_DB': SERVER / 'data' / 'users.db',
    'SENSORS_DB': SERVER / 'data' / 'sensors_history.db',
    'AUTOMATION_DB': SERVER / 'data' / 'automation.db',
    'LIGHT_STATE': SERVER / 'data' / 'light_state.json',
    'IR_STATE': SERVER / 'data' / 'ir_state.json',
    'LLM_CHAT_LOG': SERVER / 'data' / 'llm_chat.log',
    'VOICE_LOG': SERVER / 'data' / 'voice_log.txt',
    'LLM_MODEL': SERVER / 'models' / 'Qwen2.5-1.5B-Instruct_W8A8_RK3588.rkllm',
    'VOSK_MODEL': SERVER / 'models' / 'vosk-model' / 'vosk-model-small-cn-0.22',
    'LLM_PROXY_BIN': SERVER / 'native' / 'llm_proxy',
    'SENSOR_HISTORY_BIN': SERVER / 'native' / 'sensor_history',
    'RKLLM_DEMO_BIN': SERVER / 'native' / 'rkllm-demo' / 'llm_demo',
    'RKLLM_LIB_DIR': SERVER / 'native' / 'rkllm-demo' / 'lib',
    'FIRMWARE_DIR': SERVER / 'firmware',
    'KEYS_DIR': SERVER / 'keys',
    'OTA_PRIV_KEY': SERVER / 'keys' / 'ota_rsa_priv.pem',
    'ESPNOW_PROTO_DIR': SERVER / 'scripts' / 'measure',
    'AUDIO_DEBUG_DIR': SERVER / 'audio_debug',
}
for attr, want in EXPECT.items():
    got = getattr(cfg, attr)
    same = str(got) == str(want)
    if not same:
        fail += 1
    print(f'[{"OK" if same else "FAIL"}] {attr} = {got}')

print('\n---- 模块内派生路径 ----')
derived = [
    ('core.database', 'DATABASE', SERVER / 'data' / 'users.db'),
    ('core.database', 'SENSORS_DB', SERVER / 'data' / 'sensors_history.db'),
    ('api.light', 'STATE_PATH', SERVER / 'data' / 'light_state.json'),
    ('api.ir', 'STATE_PATH', SERVER / 'data' / 'ir_state.json'),
    ('api.automation', 'DB_PATH', SERVER / 'data' / 'automation.db'),
    ('api.sensors', 'SENSORS_DB_PATH', SERVER / 'data' / 'sensors_history.db'),
    ('api.sensors', 'SENSOR_ERROR_LOG', SERVER / 'data' / 'sensor_errors.log'),
    ('api.sensors', '_HISTORY_BIN', SERVER / 'native' / 'sensor_history'),
    ('api.firmware', 'MANIFEST_PATH', SERVER / 'firmware' / 'manifest.json'),
    ('api.firmware', 'OTA_PRIV', SERVER / 'keys' / 'ota_rsa_priv.pem'),
    ('api.speech', 'VOSK_MODEL_PATH', SERVER / 'models' / 'vosk-model' / 'vosk-model-small-cn-0.22'),
    ('api.speech', 'VOICE_LOG_PATH', SERVER / 'data' / 'voice_log.txt'),
    ('core.llm_manager', 'LLM_PROXY_PATH', SERVER / 'native' / 'llm_proxy'),
    ('core.llm_manager', 'LLM_DEMO_PATH', SERVER / 'native' / 'rkllm-demo' / 'llm_demo'),
    ('core.llm_manager', 'LLM_MODEL_PATH', SERVER / 'models' / 'Qwen2.5-1.5B-Instruct_W8A8_RK3588.rkllm'),
    ('core.llm_manager', 'CHAT_LOG_PATH', SERVER / 'data' / 'llm_chat.log'),
    ('link.usb_gateway', 'STATS_PATH', '/home/ubuntu/ab_results/espnow_gateway_stats.json'),
]
for mod, attr, want in derived:
    got = getattr(importlib.import_module(mod), attr)
    # Windows 上 Path('/home/...') 的分隔符会显示成反斜杠，比较时归一化
    same = str(got).replace('\\', '/') == str(want).replace('\\', '/')
    if not same:
        fail += 1
    print(f'[{"OK" if same else "FAIL"}] {mod}.{attr} = {got}')

print('\n---- 测量脚本的数据库路径解析 ----')
import importlib.util

for script in ('measure_ab.py', 'measure_ab_v2.py', 'measure_mqtt_espnow.py'):
    path = SERVER / 'scripts' / 'measure' / script
    spec = importlib.util.spec_from_file_location('_probe_' + script[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        resolved = Path(mod.SENSORS_DB).resolve()
        want = (SERVER / 'data' / 'sensors_history.db').resolve()
        same = resolved == want
        if not same:
            fail += 1
        print(f'[{"OK" if same else "FAIL"}] {script} -> {resolved}')
    except SystemExit:
        print(f'[SKIP] {script}（导入即执行，已跳过）')
    except Exception as e:
        fail += 1
        print(f'[FAIL] {script}: {type(e).__name__}: {e}')

print('\n---- 5.5 可观测性：指标采集不炸（缺 psutil/非 Linux 时优雅降级） ----')
try:
    m = importlib.import_module('core.metrics')
    host = m.host_metrics()
    assert isinstance(host, dict), 'host_metrics 应返回 dict'
    assert 'psutil' in host and 'temperature_c' in host, '缺关键字段'
    print(f'[OK]   host_metrics: psutil={host["psutil"]} cpu={host.get("cpu_percent")} '
          f'mem={"有" if host.get("memory") else "无"} disk={"有" if host.get("disk") else "无"} '
          f'temp={host.get("temperature_c")} uptime={host.get("uptime_s")}')
    svc = m.services_status()
    assert set(svc) == set(m.SERVICES), '服务列表不符'
    print(f'[OK]   services_status: {svc}（非 systemd 环境为 None 属预期）')
    link = m.link_status()
    assert 'downlink_stats' in link and 'gateway' in link, '链路状态缺字段'
    print(f'[OK]   link_status 字段: {sorted(link)}')
    h = m.health_summary()
    assert h['status'] in ('ok', 'degraded') and 'devices_online' in h, 'health 概要字段不符'
    print(f'[OK]   health_summary: {h}')
    snap = m.system_snapshot()
    assert {'host', 'services', 'devices', 'link'} <= set(snap), 'snapshot 缺顶层字段'
    print(f'[OK]   system_snapshot 顶层字段: {sorted(snap)}')
    # 判定阈值可调（5.7 自愈会复用）
    devs = m.devices_status(offline_after_s=120)
    assert devs['offline_after_s'] == 120, '阈值未透传'
    print(f'[OK]   devices_status 阈值透传 + {len(devs["devices"])} 台设备记录')
except Exception as e:
    fail += 1
    print(f'[FAIL] core.metrics 功能自检: {type(e).__name__}: {e}')

print('\n---- espnow_proto 可导入（网关依赖） ----')
try:
    importlib.import_module('espnow_proto')
    print('[OK]   espnow_proto（经 link/usb_gateway 注入 sys.path）')
except Exception as e:
    fail += 1
    print(f'[FAIL] espnow_proto: {type(e).__name__}: {e}')

print('\n---- app.py 装配检查（Flask 桩） ----')
try:
    app_mod = importlib.import_module('app')          # __name__ != '__main__'，不会真启动
    assert hasattr(app_mod, 'app'), 'app 对象缺失'
    print('[OK]   app.py 导入并完成 Blueprint 注册')
except Exception as e:
    fail += 1
    print(f'[FAIL] app.py: {type(e).__name__}: {e}')

print(f'\n结果：{"全部通过" if fail == 0 else str(fail) + " 项失败"}')
sys.exit(1 if fail else 0)

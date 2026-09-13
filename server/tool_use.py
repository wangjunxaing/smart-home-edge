# -*- coding: utf-8 -*-
"""
LLM 工具调用模块 — 工具注册表、文本协议解析与执行
==================================================
协议（阶段一 v1）：LLM 输出 [TOOL]{"name":"...","args":{...}}[/TOOL] 标记，
Python 侧解析执行后，把工具结果注入下一轮 prompt，由 LLM 组织最终回复。

工具集 v1：
  get_sensor_data — 查询指定位置最新环境数据（温度/湿度/光照/在线状态）
  set_light       — 控制灯带开关、颜色、亮度
  send_ir         — 红外发射 NEC 遥控码
  get_time        — 获取当前日期时间

阶段三演进：本模块的注册表结构可直接升级为 ReAct 智能体的 action 集合。
"""
import json
import re
from datetime import datetime

from database import get_sensors_db
from config_manager import VALID_LOCATIONS, get_device_alias
from light import set_light_state
from ir import send_ir_code

TOOL_TAG_RE = re.compile(r'\[TOOL\](.*?)\[/TOOL\]', re.DOTALL)


# ==================== 工具实现 ====================

def _first_device_id():
    """返回第一个已知设备 ID（sensor_latest 优先，其次 device_config）"""
    try:
        conn = get_sensors_db()
        cursor = conn.cursor()
        cursor.execute('SELECT esp32_id FROM sensor_latest ORDER BY last_update_ts DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        if row:
            return row['esp32_id']
    except Exception:
        pass
    try:
        conn = get_sensors_db()
        cursor = conn.cursor()
        cursor.execute('SELECT device_id FROM device_config LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        if row:
            return row['device_id']
    except Exception:
        pass
    return None


def _get_sensor_data(location='卧室'):
    """查询指定位置的最新环境数据"""
    if location not in VALID_LOCATIONS:
        location = '卧室'
    try:
        conn = get_sensors_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM sensor_latest')
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        return {'error': f'数据库读取失败: {e}'}

    now = datetime.now()
    devices = []
    for row in rows:
        if row['location'] != location:
            continue
        online = False
        if row['last_update_ts']:
            try:
                online = (now - datetime.fromisoformat(row['last_update_ts'])).total_seconds() < 30
            except Exception:
                online = False
        devices.append({
            'device_id': row['esp32_id'],
            'alias': get_device_alias(row['esp32_id']) or '',
            'temperature_c': row['temperature'] if row['temperature'] is not None and row['temperature'] >= 0 else None,
            'humidity_pct': row['humidity'] if row['humidity'] is not None and row['humidity'] >= 0 else None,
            'lux': row['lux'] if row['lux'] is not None and row['lux'] >= 0 else None,
            'online': online,
        })
    if not devices:
        return {'location': location, 'message': f'{location}暂无设备数据'}
    return {'location': location, 'device_count': len(devices), 'devices': devices}


def _set_light(device_id=None, on=None, r=None, g=None, b=None, brightness=None):
    """控制灯带；device_id 缺省时自动选择第一个设备"""
    if not device_id:
        device_id = _first_device_id()
    if not device_id:
        return {'error': '没有可用设备'}
    cur = set_light_state(device_id, on=on, r=r, g=g, b=b, brightness=brightness)
    if cur is None:
        return {'error': '灯带控制失败'}
    return {
        'status': 'ok',
        'device_id': device_id,
        'on': cur['on'],
        'r': cur['r'], 'g': cur['g'], 'b': cur['b'],
        'brightness': cur['brightness'],
        'seq': cur['seq'],
    }


def _send_ir(device_id=None, code=None):
    """红外发射 NEC 码；device_id 缺省时自动选择第一个设备"""
    if code is None:
        return {'error': '缺少红外码参数 code'}
    if not device_id:
        device_id = _first_device_id()
    if not device_id:
        return {'error': '没有可用设备'}
    try:
        code = int(code)
    except (TypeError, ValueError):
        return {'error': f'红外码必须是整数，收到: {code}'}
    seq = send_ir_code(device_id, code)
    if seq is None:
        return {'error': '红外发送失败'}
    return {'status': 'ok', 'device_id': device_id, 'code': code, 'seq': seq}


def _get_time():
    now = datetime.now()
    weekdays = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
    return {
        'datetime': now.strftime('%Y-%m-%d %H:%M:%S'),
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M:%S'),
        'weekday': weekdays[now.weekday()],
    }


# ==================== 工具注册表 ====================

TOOL_DEFS = [
    {
        'name': 'get_sensor_data',
        'desc': '查询指定位置最新环境数据（温度/湿度/光照/设备在线状态）',
        'params': '{"location": "卧室"}，location 可选值: 卧室/客厅/厨房/室外/书房/未分类',
        'func': _get_sensor_data,
    },
    {
        'name': 'set_light',
        'desc': '控制灯带开关、颜色、亮度（用户说开灯/关灯/调亮度/换颜色时使用）',
        'params': '{"device_id": "可选，省略自动选择", "on": true, "r": 255, "g": 255, "b": 255, "brightness": 128}，r/g/b/brightness 范围 0-255',
        'func': _set_light,
    },
    {
        'name': 'send_ir',
        'desc': '通过红外发射指定 NEC 遥控码（控制空调/电视等红外设备）',
        'params': '{"device_id": "可选，省略自动选择", "code": 十进制整数 NEC 码}',
        'func': _send_ir,
    },
    {
        'name': 'get_time',
        'desc': '获取当前日期和时间（用户问时间/日期/星期时使用）',
        'params': '{}',
        'func': _get_time,
    },
]

TOOLS = {d['name']: d for d in TOOL_DEFS}

TOOL_PROMPT = (
    '你是智能家居助手"小智"。需要环境数据或控制设备时，先只输出一行工具调用：\n'
    '[TOOL]{"name":"工具名","args":{参数}}[/TOOL]\n\n'
    '工具映射：\n'
    '- 问温度/湿度/光照/环境 → get_sensor_data，args: {"location":"卧室"}，location可选：卧室/客厅/厨房/室外/书房\n'
    '- 问时间/日期/星期 → get_time，args: {}\n'
    '- 开灯/关灯/调亮度/换颜色 → set_light，args: {"on":true,"brightness":200}，关灯用 {"on":false}\n'
    '- 发送红外遥控码 → send_ir，args: {"code":12345678}\n\n'
    '规则：需要工具就先只输出工具调用这一行，不要解释；其他情况直接正常回答。'
)


# ==================== 规则意图解析（稳定兜底） ====================
# 1.5B 端侧模型对工具协议的自觉遵循不可靠（会回避调用/编造数据），
# 因此 v1 采用"规则引擎优先 + LLM 决策兜底"的双层架构：
#   规则命中 → 直接执行工具（100% 稳定）
#   规则未命中 → 交给 LLM 决策轮（输出 [TOOL] 协议）
#   都未命中 → 纯闲聊
# 论文可写：规则与 LLM 双通道意图解析的对比与融合。

def parse_intent(message):
    """规则意图解析。命中返回 (tool_name, args)；未命中返回 None"""
    m = message or ''

    # 1. 灯控制（明确动词，优先级最高）
    if any(k in m for k in ('开灯', '打开灯', '把灯打开', '开一下灯', '亮灯')):
        args = {'on': True}
        b = re.search(r'亮度调到?(\d+)', m) or re.search(r'调到?(\d+)', m)
        if b:
            args['brightness'] = int(b.group(1))
        return ('set_light', args)
    if any(k in m for k in ('关灯', '关掉灯', '把灯关', '灭灯', '灯关掉')):
        return ('set_light', {'on': False})
    if '灯' in m and any(k in m for k in ('调', '亮度', '暗', '亮')):
        args = {'on': True}
        b = re.search(r'亮度调到?(\d+)', m) or re.search(r'调到?(\d+)', m)
        if b:
            args['brightness'] = int(b.group(1))
        return ('set_light', args)

    # 2. 时间查询
    if any(k in m for k in ('几点', '现在时间', '日期', '星期', '今天几号', '什么时间', '几号')):
        return ('get_time', {})

    # 3. 环境数据查询
    if any(k in m for k in ('亮度', '温度', '湿度', '光照', '环境', '多少度', '几度', '多亮', '多热', '多冷')):
        loc = '卧室'
        for l in VALID_LOCATIONS:
            if l in m:
                loc = l
                break
        return ('get_sensor_data', {'location': loc})

    return None


def format_reply(name, result, message=''):
    """工具执行结果 → 用户可见回复（模板生成，确定性 100%，无幻觉）"""
    if name == 'get_sensor_data':
        if result.get('error'):
            return f"查询失败：{result['error']}"
        loc = result.get('location', '')
        devs = result.get('devices', [])
        if not devs:
            return f"{loc}暂无设备数据。"
        parts = []
        for i, d in enumerate(devs):
            if not d.get('online'):
                continue
            desc = []
            if d.get('temperature_c') is not None:
                desc.append(f"温度 {d['temperature_c']:.1f}°C")
            if d.get('humidity_pct') is not None:
                desc.append(f"湿度 {d['humidity_pct']:.1f}%")
            if d.get('lux') is not None:
                desc.append(f"光照 {d['lux']:.1f} lux")
            if desc:
                alias = (d.get('alias') or '').strip()
                label = alias if alias else (f"设备{i + 1}" if len(devs) > 1 else '')
                if label:
                    parts.append(f"{label}：{'，'.join(desc)}")
                else:
                    parts.append('，'.join(desc))
        if not parts:
            return f"{loc}当前没有在线设备。"
        return f"{loc}的{('；'.join(parts))}。"

    if name == 'set_light':
        if result.get('error'):
            return f"灯带控制失败：{result['error']}"
        if result.get('on'):
            return f"已为你打开灯（亮度 {result['brightness']}）。"
        return '已为你关灯。'

    if name == 'get_time':
        if result.get('error'):
            return f"获取时间失败：{result['error']}"
        return f"现在是 {result['datetime']}，{result['weekday']}。"

    if name == 'send_ir':
        if result.get('error'):
            return f"红外发送失败：{result['error']}"
        return f"已发送红外码 0x{result['code']:08X}。"

    return f"已执行 {name}。"


# ==================== 解析与执行 ====================

def extract_tool_calls(text):
    """从 LLM 输出中提取工具调用列表（容错解析）"""
    calls = []
    for m in TOOL_TAG_RE.finditer(text or ''):
        raw = m.group(1).strip()
        obj = None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            try:
                obj = json.loads(raw.replace("'", '"'))
            except json.JSONDecodeError:
                continue
        if isinstance(obj, dict) and obj.get('name'):
            calls.append(obj)
    return calls


def execute_tool(name, args=None):
    """执行工具，返回可 JSON 序列化的结果 dict"""
    tool = TOOLS.get(name)
    if not tool:
        return {'error': f'未知工具: {name}'}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {'error': f'工具 {name} 参数必须是 JSON 对象，收到: {args}'}
    try:
        result = tool['func'](**args)
    except TypeError as e:
        return {'error': f'工具 {name} 参数错误: {e}'}
    except Exception as e:
        return {'error': f'工具 {name} 执行失败: {e}'}
    return result


def format_tool_result(name, result):
    """工具结果 → 注入 prompt 的文本"""
    return f"[工具 {name}] 结果: {json.dumps(result, ensure_ascii=False)}"

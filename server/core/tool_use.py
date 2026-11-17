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

from core.database import get_sensors_db
from core.config_manager import VALID_LOCATIONS, get_device_alias
from api.light import set_light_state
from api.ir import send_ir_code

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

def _get_light_status(device_id=None):
    """查询灯带当前状态（开关/颜色/亮度/设备ID）

    2026-11-16 新增：补上"感知"能力——模型可先查状态再决定是否操作。
    单步可用（回答"灯现在什么状态"）；多步链式（先查后决）见 PLAN 6.4。
    """
    try:
        from api.light import _load as _load_light, _first_device  # type: ignore
    except Exception:
        try:
            from api.light import _load as _load_light  # type: ignore
            _first_device = None
        except Exception as e:
            return {'error': f'灯带模块不可用: {e}'}
    try:
        states = _load_light()
    except Exception as e:
        return {'error': f'读取灯带状态失败: {e}'}

    dev = device_id
    if not dev:
        # 优先选在线设备，否则取第一个有状态的
        try:
            from core.tool_use import _first_device_id
            dev = _first_device_id()
        except Exception:
            dev = None
    if not dev and states:
        dev = next(iter(states.keys()), None)
    if not dev or dev not in states:
        return {'error': '暂无灯带设备状态', 'devices': list(states.keys())}

    st = states.get(dev) or {}
    return {
        'device_id': dev,
        'on': bool(st.get('on')),
        'r': st.get('r'), 'g': st.get('g'), 'b': st.get('b'),
        'brightness': st.get('brightness'),
        'led_count': st.get('led_count'),
        'updated_at': st.get('updated_at'),
        'seq': st.get('seq'),
    }

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
    {
        'name': 'get_light_status',
        'desc': '查询灯带当前状态（开关/颜色/亮度/设备ID）。用户问"灯亮着吗/现在什么状态"时使用',
        'params': '{"device_id": "可选，省略自动选择"}',
        'func': _get_light_status,
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

    # 0. 查询类意图（必须优先于控制类！否则"灯亮着吗"会被当成"调亮度"）
    #    2026-11-16 实测踩坑："灯亮着吗" 因含"灯"+"亮" 命中了调亮度规则，误开灯
    if any(k in m for k in ('灯亮着吗', '灯关着吗', '灯的状态', '灯什么状态',
                            '灯现在什么', '灯开着吗', '灯是开', '灯是关')):
        return ('get_light_status', {})
    if any(k in m for k in ('现在灯', '灯呢', '灯怎样')):
        return ('get_light_status', {})

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

    # 1.5 场景语义规则（2026-11-16 补：xLAM 对"我要睡觉了"会误开灯，改由规则兜底）
    #     高频隐含意图 → 确定性映射，避免交给 3B 模型猜
    if any(k in m for k in ('睡觉', '晚安', '困了', '要睡了', '休息了', '去睡')):
        return ('set_light', {'on': False})
    if any(k in m for k in ('起床', '早安', '醒了', '起床了')):
        return ('set_light', {'on': True, 'brightness': 128})
    if any(k in m for k in ('回家', '回来了', '到家了')):
        return ('set_light', {'on': True, 'brightness': 128})
    if any(k in m for k in ('出门', '要走了', '我走了', '离开家')):
        return ('set_light', {'on': False})

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
    if name == 'get_light_status':
        if result.get('error'):
            return f"查询失败：{result['error']}"
        on = result.get('on')
        bri = result.get('brightness')
        rgb = (result.get('r'), result.get('g'), result.get('b'))
        desc = '开着' if on else '关着'
        extra = ''
        if on:
            parts = []
            if bri is not None:
                parts.append(f'亮度 {bri}')
            if all(v is not None for v in rgb):
                parts.append(f'颜色 RGB{tuple(rgb)}')
            if parts:
                extra = '（' + '，'.join(parts) + '）'
        return f"灯带当前{desc}{extra}。"

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
    """从 LLM 输出中提取工具调用列表（容错解析，支持两种协议）

    协议 A（v1，Qwen 等）：[TOOL]{"name":"...","args":{...}}[/TOOL]
    协议 B（xLAM 专训）：[{"name":"...","arguments":{...}}]  （裸 JSON 数组/对象）

    6.1 换 xLAM 后必须同时支持，避免"机制换了模型就调不动"。
    """
    calls = []
    txt = text or ''

    # 协议 A：[TOOL]...[/TOOL]
    for m in TOOL_TAG_RE.finditer(txt):
        raw = m.group(1).strip()
        obj = _try_json(raw)
        if isinstance(obj, dict) and obj.get('name'):
            calls.append(_normalize_call(obj))
    if calls:
        return calls

    # 协议 B：裸 JSON（xLAM）
    #   先用 _fix_loose_json 修复缺引号等问题，再做 raw_decode 提取
    fixed = _fix_loose_json(txt)
    for obj in _iter_json_candidates(fixed):
        if isinstance(obj, list):
            for it in obj:
                if isinstance(it, dict) and (it.get('name') or it.get('tool')):
                    calls.append(_normalize_call(it))
        elif isinstance(obj, dict) and (obj.get('name') or obj.get('tool')):
            calls.append(_normalize_call(obj))
        if calls:
            break
    return calls


def _try_json(raw):
    """尝试解析 JSON（容忍单引号、缺 key 引号、尾部杂字符）。"""
    if not raw:
        return None
    for attempt in (raw, raw.replace("'", '"'), _fix_loose_json(raw)):
        try:
            return json.loads(attempt)
        except Exception:
            pass
    # 截取最外层 {...} 或 [...]
    for open_ch, close_ch in (('{', '}'), ('[', ']')):
        i, j = raw.find(open_ch), raw.rfind(close_ch)
        if 0 <= i < j:
            try:
                return json.loads(raw[i:j + 1])
            except Exception:
                pass
    return None


def _iter_json_candidates(txt):
    """用 JSONDecoder.raw_decode 从每个 '{' 或 '[' 起点尝试解析出完整 JSON。

    比正则可靠：能正确处理嵌套（如 {"name":..,"arguments":{}} 的内层花括号）。
    """
    decoder = json.JSONDecoder()
    n = len(txt)
    i = 0
    seen = 0
    while i < n and seen < 8:
        ch = txt[i]
        if ch in '{[':
            try:
                obj, end = decoder.raw_decode(txt[i:])
            except Exception:
                i += 1
                continue
            if isinstance(obj, (dict, list)):
                yield obj
                seen += 1
                i += max(1, end)
                continue
        i += 1


def _fix_loose_json(raw):
    """修复常见小模型 JSON 错误（实测 xLAM 会犯的两类）。

    1) 缺 key 引号：  {"name: get_sensor_data", "arguments": {...}}
       -> {"name": "get_sensor_data", "arguments": {...}}
    2) 整个 key:value 挤在一个引号里（上面那种就是）
    3) 单引号字符串

    注意：修复只求"能解析出工具调用"，解析不出就安全返回 None（不误执行）。
    """
    import re as _re
    fixed = raw.replace("'", '"')

    # 类型 1：{"后缀 "name: value" 形式（key 和 value 挤在一个引号里）
    #   匹配  { "word: 任意字符"  ->  { "word": "任意字符"
    fixed = _re.sub(r'\{\s*"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^"]*?)"',
                    r'{"\1": "\2"', fixed)

    # 类型 2：未加引号的 key  {"name: value  -> {"name": "value
    fixed = _re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*',
                    r'\1"\2": ', fixed)
    return fixed


def _normalize_call(obj):
    """统一成 {"name":..., "args":{...}}（xLAM 用 arguments 字段）。"""
    args = obj.get('args')
    if args is None:
        args = obj.get('arguments')
    if args is None:
        args = {}
    if isinstance(args, str):
        parsed = _try_json(args)
        args = parsed if isinstance(parsed, dict) else {}
    name = obj.get('name') or obj.get('tool') or ''
    return {'name': name, 'args': args}


def execute_tool(name, args=None):
    """执行工具，返回可 JSON 序列化的结果 dict

    5.13：用 audit_context 标记来源为 ai_tool，使下行审计能区分
    "AI 调了开灯" 与 "人在网页点开灯"（这是可追责的关键）。
    """
    tool = TOOLS.get(name)
    if not tool:
        return {'error': f'未知工具: {name}'}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {'error': f'工具 {name} 参数必须是 JSON 对象，收到: {args}'}
    try:
        from core import audit
        with audit.audit_context('ai_tool', 'ai'):
            result = tool['func'](**args)
    except TypeError as e:
        return {'error': f'工具 {name} 参数错误: {e}'}
    except Exception as e:
        return {'error': f'工具 {name} 执行失败: {e}'}
    return result


def format_tool_result(name, result):
    """工具结果 → 注入 prompt 的文本"""
    return f"[工具 {name}] 结果: {json.dumps(result, ensure_ascii=False)}"

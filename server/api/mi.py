# -*- coding: utf-8 -*-
"""
米家设备接入模块（5.6）— Blueprint
==================================
把现成米家设备（首台：欧伦特智能排插 CB7，model `oit.plug.cb7`）纳入 0-Smart，
使网页 / 自动化 / LLM 能像控制自研节点一样控制它。

设备属性（据 MIoT-SPEC 官方定义 + 实机扫描，2026-10-11）
--------------------------------------------------------
    siid=1  Device Information      设备信息（只读）
    siid=2  Switch                  总开关              → 读写
            piid=2 Default Power On State  上电默认状态（读回实测为 0/1）
            piid=3 Device Fault            设备故障标志
    siid=3  Indicator Light         指示灯              → 读写
    siid=4  Physical Controls Locked 童锁（物理按键锁定） → 读写
    siid=6~10  switch ×5            5 个物理孔位         → 读写
            每个孔位 piid=2 倒计时、piid=3 定时

控制通路
--------
主通路 **云端 MIoT**（`https://api.io.mi.com/app`，区域 cn），用 micloud 的 `request()`：
它自带必需请求头 `x-xiaomi-protocal-flag-cli: PROTOCAL-HTTP2`、`generate_enc_params`
签名与 RC4 解密（这三样缺一必然 401 auth error）。优点是**与网络拓扑无关**
（服务器在热点、设备在 2.4G 也能控制）。

降级通路 **本地 miIO UDP**（python-miio 直连 54321）。实测该型号本地返回
`{"code":-9999,"message":"undefined command"}`（本地命令集不兼容），故仅作尝试。

会话与续期
----------
- `data/mi_devices.json`：设备清单（ip / token / model / did）
- `data/mi_session.json`：云端会话（serviceToken / ssecurity / userId / cUserId / passToken / location）
  serviceToken 会过期；过期后先尝试用保存的 `location` 重新换取（`_refresh_session`），
  仍失败则需重新扫码（`tools/local/mi_qr_login.py`）。
"""
import json
import threading
import time

from flask import Blueprint, request, jsonify

from core.auth import admin_required
from core.config import MI_DEVICES, MI_SESSION

mi_bp = Blueprint('mi', __name__)

_UA = ('Android-7.1.1-1.0.0-ONEPLUS A3010-136-8C4B3D6B1F4E '
       'APP/xiaomi.smarthome APPV/62830')

# ---- 属性地址 ----
SIID_POWER = 2
PIID_POWER = 1               # 总开关 Switch Status
PIID_POWER_ON_STATE = 2      # 上电默认状态 Default Power On State
PIID_FAULT = 3               # 设备故障 Device Fault
SIID_INDICATOR = 3           # 指示灯
PIID_INDICATOR = 1
SIID_CHILD_LOCK = 4          # 童锁（物理按键锁定）
PIID_CHILD_LOCK = 1
CHANNEL_SIIDS = [6, 7, 8, 9, 10]   # 5 个物理孔位
PIID_SWITCH = 1

CHANNEL_NAMES = {6: '孔位1', 7: '孔位2', 8: '孔位3', 9: '孔位4', 10: '孔位5'}

_STATUS_TTL = 5.0            # 状态缓存（秒）
MIN_WRITE_INTERVAL = 3.0     # 同一目标两次写入的最小间隔（秒）——防反复触发
_lock = threading.Lock()
_status_cache = {}
_session_cache = {}
_last_write = {}             # key -> {target: (ts, value)}  写入防抖记录


# ==================== 文件读写 ====================

def _load_devices():
    try:
        with open(str(MI_DEVICES), 'r', encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _load_session():
    global _session_cache
    try:
        with open(str(MI_SESSION), 'r', encoding='utf-8') as f:
            data = json.load(f) or {}
    except Exception:
        return {}
    key = (data.get('serviceToken'), data.get('ssecurity'), data.get('userId'))
    if key != _session_cache.get('key'):
        _session_cache = dict(data)
        _session_cache['key'] = key
    return _session_cache


def save_session(service_token, ssecurity, user_id, c_user_id=None,
                 pass_token=None, location=None):
    """由扫码登录脚本调用：写入云端会话（含续期所需的 passToken/location）"""
    payload = {'serviceToken': service_token, 'ssecurity': ssecurity,
               'userId': str(user_id), 'cUserId': c_user_id,
               'passToken': pass_token, 'location': location,
               'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    with open(str(MI_SESSION), 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    global _session_cache
    _session_cache = dict(payload)
    _session_cache['key'] = (service_token, ssecurity, str(user_id))
    return payload


# ==================== 云端通路 ====================

class MiAuthError(Exception):
    """serviceToken 过期/无效，需要重新扫码登录"""


def _new_client(sess):
    from micloud import MiCloud
    mc = MiCloud(username=sess.get('userId') or '', password='')
    mc.user_id = sess.get('userId')
    mc.service_token = sess.get('serviceToken')
    mc.ssecurity = sess.get('ssecurity')
    mc.cuser_id = sess.get('cUserId')
    mc.pass_token = sess.get('passToken')
    mc._init_session(reset=True)
    return mc


def _cloud_call(path, body):
    sess = _load_session()
    if not sess.get('serviceToken') or not sess.get('ssecurity'):
        raise MiAuthError('未登录：缺少 data/mi_session.json，请先扫码登录')
    try:
        from micloud import MiCloud  # noqa: F401
    except ImportError as e:
        raise RuntimeError(f'缺少 micloud 依赖：{e}')

    mc = _new_client(sess)
    raw = mc.request_country(path, 'cn', {'data': json.dumps(body)})
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode('utf-8', 'replace')
    try:
        return json.loads(raw)
    except Exception:
        raise RuntimeError(f'云端返回不是 JSON: {str(raw)[:160]}')


def refresh_session():
    """用 **passToken** 自动续期 serviceToken（无需密码、无需扫码）。

    流程（2026-10-11 实测跑通）：
      1. 带上 `passToken` / `userId` / `cUserId` 作为 cookie，GET
         `https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true`
         → 返回 `result=ok` 且带新的 `location` + `ssecurity`
         （**关键**：这一步已登录就直接成功，无需走 serviceLoginAuth2 / 密码）
      2. GET 该 `location` → 从 cookie 取新的 `serviceToken`
      3. 落盘

    passToken 是长期凭据（数月），故此后无需人工扫码。
    返回 (ok, message)。
    """
    sess = _load_session()
    pt = sess.get('passToken')
    uid = str(sess.get('userId') or '')
    if not pt or not uid:
        return False, '缺少 passToken（需重新扫码一次以启用自动续期）'

    try:
        import requests
        s = requests.Session()
        s.headers.update({'User-Agent': _UA})
        for name, val in (('passToken', pt), ('userId', uid), ('cUserId', sess.get('cUserId'))):
            if val:
                s.cookies.set(name, val, domain='.xiaomi.com')

        r1 = s.get('https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true', timeout=20)
        j1 = json.loads(r1.text.replace('&&&START&&&', ''))
        if j1.get('result') != 'ok' or not j1.get('location'):
            return False, f"passToken 续期失败: code={j1.get('code')} {j1.get('description')}"

        r3 = s.get(j1['location'],
                   headers={'content-type': 'application/x-www-form-urlencoded'}, timeout=20)
        tok = None
        for ck in s.cookies:
            if ck.name == 'serviceToken':
                tok = ck.value
                break
        if not tok:
            return False, f'续期取 serviceToken 失败（HTTP {r3.status_code}）'

        save_session(tok, j1.get('ssecurity') or sess.get('ssecurity'),
                     j1.get('userId') or uid,
                     j1.get('cUserId') or sess.get('cUserId'),
                     j1.get('passToken') or pt,
                     j1.get('location'))
        print('[米家] serviceToken 已用 passToken 自动续期')
        return True, 'refreshed'
    except Exception as e:
        return False, f'{type(e).__name__}: {str(e)[:120]}'


def _prop_get_many(did, items):
    """批量读：items=[(siid,piid),...] → {(siid,piid): value}"""
    params = [{'did': did, 'siid': s, 'piid': p} for s, p in items]
    js = _cloud_call('/miotspec/prop/get', {'params': params})
    if js.get('code') != 0:
        raise MiAuthError(f'云端读取失败: {str(js)[:120]}')
    return {(r.get('siid'), r.get('piid')): r.get('value')
            for r in (js.get('result') or []) if r.get('code') == 0}


def _prop_set(did, siid, piid, value):
    js = _cloud_call('/miotspec/prop/set',
                     {'params': [{'did': did, 'siid': siid, 'piid': piid, 'value': value}]})
    res = (js.get('result') or [{}])[0]
    if js.get('code') != 0 or res.get('code') not in (0, None):
        return False, f'云端写入失败: {str(js)[:160]}'
    return True, 'ok'


def _local_info(cfg):
    try:
        from miio import Device
        return Device(cfg['ip'], cfg['token']).info()
    except Exception:
        return None


# ==================== 对外能力 ====================

def _build_status(cfg, key):
    items = [(SIID_POWER, PIID_POWER), (SIID_POWER, PIID_FAULT),
             (SIID_INDICATOR, PIID_INDICATOR),
             (SIID_CHILD_LOCK, PIID_CHILD_LOCK)] + \
            [(s, PIID_SWITCH) for s in CHANNEL_SIIDS]
    vals = _prop_get_many(cfg.get('did'), items)
    return {
        'power': vals.get((SIID_POWER, PIID_POWER)),
        'fault': vals.get((SIID_POWER, PIID_FAULT)),
        'indicator': vals.get((SIID_INDICATOR, PIID_INDICATOR)),
        'child_lock': vals.get((SIID_CHILD_LOCK, PIID_CHILD_LOCK)),
        'channels': [{'siid': s, 'name': CHANNEL_NAMES.get(s, f'孔位{s}'),
                      'on': vals.get((s, PIID_SWITCH))} for s in CHANNEL_SIIDS],
    }


def get_status(key, ttl=_STATUS_TTL, retry_on_auth=True):
    """读全部状态（带缓存）。"""
    cfg = _load_devices().get(key)
    if not cfg:
        return {'key': key, 'error': f'未知设备 {key}', 'reachable': False}

    now = time.time()
    with _lock:
        hit = _status_cache.get(key)
        if hit and (now - hit[0]) < ttl:
            payload = dict(hit[1])
            payload['cached'] = True
            payload['age_ms'] = int((now - hit[0]) * 1000)
            return payload

    payload = {'key': key, 'name': cfg.get('name', key), 'model': cfg.get('model', ''),
               'ip': cfg.get('ip', ''), 'power': None, 'channels': [],
               'indicator': None, 'child_lock': None, 'fault': None,
               'reachable': False, 'source': None, 'error': None}
    try:
        payload.update(_build_status(cfg, key))
        payload['reachable'] = True
        payload['source'] = 'cloud'
    except Exception as e:
        # 会话过期 → 尝试自动续期一次
        if retry_on_auth:
            ok, msg = refresh_session()
            if ok:
                return get_status(key, ttl=0, retry_on_auth=False)
            payload['error'] = f'云端会话失效（自动续期失败: {msg}），需重新扫码'
        else:
            payload['error'] = f'云端: {str(e)[:110]}'
            info = _local_info(cfg)
            if info is not None:
                payload['reachable'] = True
                payload['source'] = 'local'
                payload['error'] += '（本地可连，但该型号本地命令集不兼容）'

    with _lock:
        _status_cache[key] = (now, dict(payload))
    payload['cached'] = False
    payload['age_ms'] = 0
    return payload


def list_devices():
    out = []
    for key, cfg in _load_devices().items():
        item = {'key': key, 'name': cfg.get('name', key), 'model': cfg.get('model', ''),
                'ip': cfg.get('ip', ''), 'did': cfg.get('did', ''),
                'online': None, 'power': None, 'channels': [],
                'indicator': None, 'child_lock': None, 'fault': None, 'error': None}
        try:
            st = get_status(key, ttl=10)
            for f in ('power', 'channels', 'indicator', 'child_lock', 'fault', 'error'):
                item[f] = st.get(f)
            item['online'] = st.get('reachable')
        except Exception as e:
            item['error'] = str(e)[:120]
        out.append(item)
    return out


# 可控制的目标（供校验与前端复用）
def control_targets():
    t = {'main': {'siid': SIID_POWER, 'piid': PIID_POWER, 'name': '总开关'},
         'indicator': {'siid': SIID_INDICATOR, 'piid': PIID_INDICATOR, 'name': '指示灯'},
         'child_lock': {'siid': SIID_CHILD_LOCK, 'piid': PIID_CHILD_LOCK, 'name': '童锁'}}
    for s in CHANNEL_SIIDS:
        t[str(s)] = {'siid': s, 'piid': PIID_SWITCH, 'name': CHANNEL_NAMES.get(s, f'孔位{s}')}
    return t


def set_mi_target(key, on, target='main', force=False):
    """通用控制：target ∈ {'main','indicator','child_lock','6'..'10'}。
    供路由 / 自动化 / LLM 工具复用，返回 (ok, message)。

    防抖（2026-10-11 按用户要求加入）：
      - **相同值不重复下发**：目标当前已是期望状态时直接返回，不打扰设备；
      - **冷却窗口**：同一目标 MIN_WRITE_INTERVAL 秒内的重复请求被忽略（避免连点/IP 重放）；
      - `force=True` 可绕过（供自动化等确需立即复位的场景）。
    """
    cfg = _load_devices().get(key)
    if not cfg:
        return False, f'未知设备 {key}'
    if not cfg.get('did'):
        return False, '设备缺少 did，无法走云端控制'

    tgt = control_targets().get(str(target))
    if not tgt:
        return False, f'未知控制目标 {target}（可用: {list(control_targets())}）'

    on = bool(on)
    now = time.time()

    if not force:
        # ① 相同值不重复下发
        st = get_status(key, ttl=_STATUS_TTL)
        field = {'main': 'power', 'indicator': 'indicator', 'child_lock': 'child_lock'}.get(str(target))
        if field:
            cur = st.get(field)
        else:
            cur = next((c['on'] for c in st.get('channels', []) if str(c['siid']) == str(target)), None)
        if cur is not None and bool(cur) == on:
            return True, f"{cfg.get('name', key)} {tgt['name']}已是{'开启' if on else '关闭'}状态（未重复下发）"

        # ② 冷却窗口
        with _lock:
            rec = _last_write.get(key, {}).get(str(target))
        if rec and (now - rec[0]) < MIN_WRITE_INTERVAL:
            return False, f"{tgt['name']}操作过于频繁，请 {MIN_WRITE_INTERVAL:.0f} 秒后再试（已忽略）"

    try:
        ok, msg = _prop_set(cfg.get('did'), tgt['siid'], tgt['piid'], on)
        if ok:
            with _lock:
                _status_cache.pop(key, None)
                _last_write.setdefault(key, {})[str(target)] = (now, on)
            state = '开启' if on else '关闭'
            print(f"[米家] {cfg.get('name', key)} {tgt['name']} -> {state}")
            # 5.13：米家走云端 API、不经 downlink，需在此单独记操作审计 + 快照
            try:
                from core import audit
                dev_tag = (key if str(target) == 'main' else f'{key}:{target}')
                # 操作前状态快照（供撤销；取缓存即可，失败跳过）
                try:
                    st0 = get_status(key, ttl=_STATUS_TTL)
                    fld = {'main': 'power', 'indicator': 'indicator',
                           'child_lock': 'child_lock'}.get(str(target))
                    if fld:
                        prev = st0.get(fld)
                    else:
                        prev = next((c['on'] for c in st0.get('channels', [])
                                     if str(c['siid']) == str(target)), None)
                    if prev is not None:
                        audit.save_snapshot(dev_tag, 'mi', {'on': bool(prev)})
                except Exception:
                    pass
                src, act = audit.current_context()
                if act is None:
                    try:
                        from flask import has_request_context, session
                        if has_request_context():
                            act = session.get('username') or ''
                    except Exception:
                        act = ''
                audit.log_operation(src or 'web', act or '', dev_tag,
                                    {'type': 'mi', 'target': str(target), 'on': on},
                                    'ok', 'cloud', 0)
            except Exception as e:
                print(f'[米家] 审计记录失败（不影响控制）: {e}')
            return True, f"{cfg.get('name', key)} {tgt['name']}已{state}"
        return False, msg
    except Exception as e:
        return False, f'控制失败: {str(e)[:120]}'


def set_mi_power(key, on, siid=None):
    """兼容旧签名：siid=None → 总开关；siid 为 6~10 → 对应孔位。"""
    return set_mi_target(key, on, 'main' if siid is None else str(siid))


def first_device_key():
    devs = _load_devices()
    return next(iter(devs), None)


# ==================== 后台自动续期 ====================

_REFRESH_INTERVAL = 6 * 3600      # 每 6 小时主动续期一次，避免用户操作时才发现过期
_refresh_thread_started = False


def _refresh_loop():
    """后台线程：定期用 passToken 刷新 serviceToken。
    失败不重试到打爆接口；下个周期再试（过期时 get_status 也会即时续期一次）。"""
    time.sleep(60)                 # 启动后先等 60 秒，避开服务启动高峰
    while True:
        try:
            sess = _load_session()
            if sess.get('passToken') and _load_devices():
                ok, msg = refresh_session()
                if not ok:
                    print(f'[米家] 定期续期失败（下轮再试）: {msg}')
        except Exception as e:
            print(f'[米家] 续期线程异常: {type(e).__name__}: {e}')
        time.sleep(_REFRESH_INTERVAL)


def start_refresh_thread():
    """由 app.py 在启动时调用（幂等）"""
    global _refresh_thread_started
    if _refresh_thread_started:
        return
    _refresh_thread_started = True
    t = threading.Thread(target=_refresh_loop, daemon=True, name='mi-refresh')
    t.start()
    print(f'[米家] 自动续期线程已启动（每 {_REFRESH_INTERVAL // 3600} 小时一次）')


# ==================== 路由 ====================

@mi_bp.route('/api/esp32/mi/devices', methods=['GET'])
def api_mi_devices():
    return jsonify({'devices': list_devices(), 'ts': int(time.time() * 1000)})


@mi_bp.route('/api/admin/mi/status', methods=['GET'])
@admin_required
def api_mi_status():
    key = request.args.get('key') or first_device_key()
    if not key:
        return jsonify({'ok': False, 'error': '未配置任何米家设备'}), 404
    ttl = float(request.args.get('ttl', _STATUS_TTL))
    return jsonify({'ok': True, 'status': get_status(key, ttl=ttl)})


@mi_bp.route('/api/admin/mi/control', methods=['POST'])
@admin_required
def api_mi_control():
    data = request.get_json(silent=True) or {}
    key = data.get('key') or first_device_key()
    if not key:
        return jsonify({'ok': False, 'error': '未配置任何米家设备'}), 404
    if 'on' not in data:
        return jsonify({'ok': False, 'error': '缺少 on 字段'}), 400
    target = data.get('target')
    if target is None:
        target = 'main' if data.get('siid') is None else str(data.get('siid'))
    ok, msg = set_mi_target(key, bool(data.get('on')), target,
                            force=bool(data.get('force')))
    # 云端写入有数秒传播延迟：真正下发后等 4 秒再回读，避免响应里带旧状态
    # 造成"点了没反应"的错觉（实测写入→读回一致需约 4 秒）
    if ok and '未重复下发' not in msg:
        time.sleep(4)
    return jsonify({'ok': ok, 'message': msg,
                    'cooldown_s': MIN_WRITE_INTERVAL,
                    'status': get_status(key, ttl=0)})


@mi_bp.route('/api/admin/mi/refresh', methods=['POST'])
@admin_required
def api_mi_refresh():
    """手动触发会话续期（serviceToken 过期时用）"""
    ok, msg = refresh_session()
    return jsonify({'ok': ok, 'message': msg})


@mi_bp.route('/api/mi/client_error', methods=['POST'])
def api_mi_client_error():
    """前端错误上报（公开：用于排查浏览器端 JS 异常，只记日志不回显敏感信息）"""
    d = request.get_json(silent=True) or {}
    src = str(d.get('source', ''))[:200]
    line = d.get('line')
    col = d.get('col')
    msg = str(d.get('message', ''))[:400]
    stack = str(d.get('stack', ''))[:400]
    ua = str(request.headers.get('User-Agent', ''))[:120]
    print(f'[前端错误] {src}:{line}:{col} {msg} | stack={stack} | ua={ua}')
    return jsonify({'ok': True})

# -*- coding: utf-8 -*-
"""
离线告警与远控重启自愈（5.7）
==============================
定位：在 §5.5"看得见"的基础上做"**能救回来**"——先远控重启，救不回来才喊人。

判据与流程
----------
    静默超阈值（§5.5 的节点健康判定，默认 60s）
      → ① 远控重启：下行 {"type":"reboot"}，按最近活跃通道走（MQTT 或 ESP-NOW）
      → 等待回连 REBOOT_WAIT（默认 90s）
      → ② 仍静默 → 再重启一次（最多 MAX_ATTEMPTS 次，带退避）
      → ③ 连续 K 次仍不回 → 判"需人工介入"
           记录事件 + 网页告警 + 外部通知（Notifier 抽象）
           同一事件去抖，避免重复轰炸
    设备恢复 → 自动清除告警并补发"已恢复"通知

现实边界（写进论文）
--------------------
节点彻底断网时 MQTT 下行送不到，但 **ESP-NOW 通道不依赖路由器**（节点降级后仍可达），
所以 ①② 仍有机会送达；两通道都不通时才落到 ③（现场处理/等看门狗）。
若该节点接了米家插座，③ 可追加"断电复位"动作（用 §5.6 的 miot 接入）。

不做 TTS 播报（按用户设定：告警不做语音打扰）。
"""
import json
import threading
import time

from core.config import ALERTS_STATE
from link.downlink import send_downlink
from core import notifier

# ---- 可调参数 ----
# 2026-11-14 修复"弱网隔门反复重启/飞书轰炸"事故，参数整体放宽：
#   原先 SILENT_THRESHOLD_S=60 与固件"连续 60s 不可用才降级 ESP-NOW"完全同步，
#   服务器总在节点即将降级时先下发重启 → 节点重启后失败计数归零 → 重新连弱 WiFi
#   → 又 60s → 又重启，**永远走不到 ESP-NOW**，24 小时重启 77 次、飞书 43 条。
#   现在给固件降级(60s) + ESP-NOW 建立与稳定上报留足余量。
SILENT_THRESHOLD_S = 300         # 静默多久算离线（必须 > 固件降级窗口 60s，留 5 分钟）
REBOOT_WAIT_S = 120              # 重启后等待回连时间（原 90s，弱网下回连更慢）
MAX_ATTEMPTS = 2                 # 最多自动重启次数（之后判人工介入）
CHECK_INTERVAL_S = 15            # 巡检周期
RECOVER_GRACE_S = 20             # 出现上报后确认恢复的稳定时间（避免抖动）

# 弱网保护（2026-11-14 新增）
ESP_NOW_TRUST_S = 180            # 最近 180s 内有 ESP-NOW 上行 → 节点已降级成功，绝不重启
REBOOT_MIN_INTERVAL_S = 3600     # 同一设备两次重启最小间隔（1 小时），杜绝重启风暴
NOTIFY_MIN_INTERVAL_S = 21600    # 同一设备"需人工介入"最小间隔（6 小时），杜绝飞书轰炸

# 开机静默期（防"断电重启误判"）
# 学校/NP 场景：夜间断电，早上 Pi 与节点同时上电。此时数据库里的 last_update_ts
# 还停留在断电前（可能数小时前），devices_status() 会算出巨大的 silent_s →
# 若不防护，服务一起来就会误判"离线"并下发重启。
# 对策：服务启动后的 STARTUP_GRACE_S 内，**只对"本次开机后已上报过"的设备做判定**；
# 其余设备视为"等待首次上报"，不下发重启、不告警。
STARTUP_GRACE_S = 180

_service_started_at = time.time()   # 模块加载时刻（≈ 服务启动）
_reported_since_start = set()       # 本次开机后已上报过的设备

_lock = threading.Lock()
_state = None
_thread_started = False

# 重启归来检测：节点重启后上报里的 t0（开机毫秒）会归零/很小，
# 据此可**服务端自证**"它真的重启过"，无需接串口（实机验证用）。
RETURNED_T0_MS = 120000          # 认为"刚开机"的 t0 上限（120 秒）
_pending_reboot = {}             # device_id -> 下发重启的时间戳

# 联调/演示用：临时把某设备伪装为离线（不动真实硬件），到期自动失效
# 用途：完整演示"失联 → 自动重启 → 通知 → 恢复"，且不冒真实断网的风险
_simulated = {}                  # device_id -> 失效时间戳

# 弱网保护用的限流记录（内存态，随服务重启清空，够用）
_reboot_ts = {}                  # device_id -> [重启时刻, ...]
_notify_ts = {}                  # device_id -> 上次实际推送"需人工介入"的时刻


def simulate_offline(device_id, seconds):
    if seconds and seconds > 0:
        _simulated[device_id] = time.time() + float(seconds)
    else:
        _simulated.pop(device_id, None)
    return _simulated.get(device_id)


def simulated_until(device_id):
    ts = _simulated.get(device_id)
    if not ts:
        return None
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


# ==================== 状态持久化 ====================

def _default_state():
    return {
        'devices': {},      # device_id -> {status, silent_s, attempts, last_reboot, alerted, recovered_at}
        'events': [],       # 告警事件列表（最新在前，最多保留 200 条）
        'stats': {'reboots': 0, 'alerts': 0, 'recoveries': 0, 'last_check': None},
    }


def _load():
    global _state
    if _state is None:
        try:
            with open(str(ALERTS_STATE), 'r', encoding='utf-8') as f:
                _state = json.load(f)
        except Exception:
            _state = _default_state()
        for k, v in _default_state().items():
            _state.setdefault(k, v)
    return _state


def _save():
    try:
        with open(str(ALERTS_STATE), 'w', encoding='utf-8') as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'[自愈] 状态保存失败: {e}')


def _add_event(level, device_id, action, detail):
    st = _load()
    st['events'].insert(0, {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'level': level, 'device_id': device_id, 'action': action, 'detail': detail,
    })
    del st['events'][200:]
    if level in ('warning', 'critical'):
        st['stats']['alerts'] = st['stats'].get('alerts', 0) + 1
    _save()


# ==================== 对外查询 ====================

def snapshot():
    """给前端/接口用：告警状态摘要"""
    st = _load()
    active = [{'device_id': d, **v} for d, v in st['devices'].items()
              if v.get('status') in ('offline', 'rebooting', 'failed')]
    return {
        'active_alerts': active,
        'count': len(active),
        'events': st['events'][:30],
        'stats': st['stats'],
        'config': {'silent_threshold_s': SILENT_THRESHOLD_S,
                   'reboot_wait_s': REBOOT_WAIT_S,
                   'max_attempts': MAX_ATTEMPTS,
                   'esp_now_trust_s': ESP_NOW_TRUST_S,
                   'reboot_min_interval_s': REBOOT_MIN_INTERVAL_S,
                   'notify_min_interval_s': NOTIFY_MIN_INTERVAL_S},
    }


def clear_device(device_id):
    st = _load()
    st['devices'].pop(device_id, None)
    _save()


# ==================== 单设备处理 ====================

def _reboot(device_id):
    """下发重启命令，返回使用的通道名（5.13：审计来源标记为 system）"""
    try:
        from core import audit as _audit
        with _audit.audit_context('system', 'self_heal'):
            ch = send_downlink(device_id, {'type': 'reboot'})
    except Exception as e:
        print(f'[自愈] {device_id[:8]} 重启下发异常: {type(e).__name__}: {e}')
        return ''
    if ch:
        _pending_reboot[device_id] = time.time()
        print(f'[自愈] {device_id[:8]} 已下发重启（通道 {ch}）')
    else:
        print(f'[自愈] {device_id[:8]} 重启下发失败（两通道均不可用）')
    return ch or ''


# ==================== 弱网保护（2026-11-14 事故修复）====================

def _recent_espnow_s(device_id):
    """最近一次 ESP-NOW 上行距今秒数；None=从未上报过。"""
    try:
        from link import link_state
        return link_state.last_up_age(device_id, 'espnow')
    except Exception:
        return None


def _reboot_allowed(device_id):
    """同设备重启频率限制：1 小时内最多 1 次，杜绝"重启风暴"。

    事故复盘：弱网隔门时节点每 90s 被重启一次，24h 累计 77 次，
    既救不回来又打断固件的 ESP-NOW 降级，必须限流。
    """
    now = time.time()
    ts_list = [t for t in _reboot_ts.get(device_id, []) if now - t < REBOOT_MIN_INTERVAL_S]
    _reboot_ts[device_id] = ts_list
    return len(ts_list) == 0


def _notify_allowed(device_id):
    """同设备"需人工介入"通知频率限制：6 小时内最多 1 条。"""
    return (time.time() - _notify_ts.get(device_id, 0)) >= NOTIFY_MIN_INTERVAL_S


def note_uplink(device_id, t0_ms):
    """节点每次上报时调用。

    1) **记入本次开机已上报集合**：用于开机静默期防止断电重启误判；
    2) 若曾下发重启且 t0 很小（刚开机），判定"重启归来"——这是**服务端自证重启成功**
       的依据（节点开机毫秒归零），比只看 ACK 更硬：ACK 只说明命令送达，
       t0 归零说明它确实重启过。
    """
    _reported_since_start.add(device_id)

    if device_id not in _pending_reboot:
        return False
    if t0_ms is None or t0_ms > RETURNED_T0_MS:
        return False
    sent_at = _pending_reboot.pop(device_id, 0)
    took = time.time() - sent_at
    _add_event('info', device_id, 'reboot_confirmed',
               f'重启归来已确认（t0={int(t0_ms)}ms，下发后 {took:.0f}s 恢复上报）')
    rec = _load()['devices'].get(device_id)
    if rec is not None:
        rec['reboot_confirmed_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        rec['reboot_confirmed_s'] = round(took, 1)
    _save()
    print(f'[自愈] {device_id[:8]} 重启归来确认：t0={int(t0_ms)}ms，耗时 {took:.0f}s')
    return True


def _handle_offline(device_id, rec, silent_s):
    """离线处理：重启 → 等待 → 重试 → 判人工介入（含弱网三层保护）"""
    st = _load()
    attempts = rec.get('attempts', 0)

    # 已判定"需人工介入"：不再重启、不再计数，只更新静默时长，
    # 既避免无限重试轰炸设备，也避免 reboots 计数持续膨胀（实机测试发现）
    if rec.get('status') == 'failed' and rec.get('alerted'):
        return

    # ---- 保护 1（最关键）：ESP-NOW 通道仍活跃 → 节点已降级成功，只是丢了几个包 ----
    # 弱网隔门场景下，节点降级到 ESP-NOW 后仍能上报；此时"静默"是抖动而非死亡。
    # 若此时重启，会打断降级、把节点拉回弱 WiFi，形成死循环（2026-11-14 事故根因）。
    esp_age = _recent_espnow_s(device_id)
    if esp_age is not None and esp_age <= ESP_NOW_TRUST_S:
        rec.update({'status': 'online', 'attempts': 0, 'alerted': False,
                    'silent_s': round(silent_s, 1)})
        _save()
        print(f'[自愈] {device_id[:8]} 静默 {silent_s:.0f}s 但 ESP-NOW 通道 '
              f'{esp_age:.0f}s 前仍活跃 → 判为弱网抖动，不重启')
        return

    if attempts < MAX_ATTEMPTS:
        # ---- 保护 2：重启频率限制（1 小时最多 1 次）----
        if not _reboot_allowed(device_id):
            print(f'[自愈] {device_id[:8]} 近 {REBOOT_MIN_INTERVAL_S // 60} 分钟内已重启过，'
                  f'跳过本次（防重启风暴）')
            return
        ch = _reboot(device_id)
        attempts += 1
        _reboot_ts.setdefault(device_id, []).append(time.time())
        rec.update({'status': 'rebooting', 'attempts': attempts,
                    'last_reboot': time.time(), 'last_channel': ch,
                    'rebooted_at': time.strftime('%Y-%m-%d %H:%M:%S')})
        st['stats']['reboots'] = st['stats'].get('reboots', 0) + 1
        detail = (f'静默 {silent_s:.0f}s，第 {attempts}/{MAX_ATTEMPTS} 次远控重启'
                  f'（通道 {ch or "不可用"}）')
        _add_event('warning', device_id, 'reboot', detail)
        # 通知策略（按用户要求）：**断链重启不发外部通知**，只在后台记录。
        # 只有"重连后仍未恢复、需人工介入"才推送飞书，避免无意义打扰。
        _save()
        return

    if not rec.get('alerted'):
        # ---- 保护 3：通知去抖（6 小时最多 1 条）----
        if not _notify_allowed(device_id):
            rec.update({'status': 'failed', 'alerted': True,
                        'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')})
            _add_event('critical', device_id, 'need_human',
                       f'连续 {MAX_ATTEMPTS} 次远控重启后仍无上报（6 小时内已通知过，本次仅记录）')
            _save()
            print(f'[自愈] {device_id[:8]} 判定离线，但 {NOTIFY_MIN_INTERVAL_S // 3600}h 内'
                  f'已通知过 → 只记录不推送')
            return
        rec.update({'status': 'failed', 'alerted': True,
                    'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')})
        detail = f'连续 {MAX_ATTEMPTS} 次远控重启后仍无上报，需人工介入（检查供电/网络/现场）'
        _add_event('critical', device_id, 'need_human', detail)
        _notify_ts[device_id] = time.time()
        notifier.notify('设备离线需人工介入',
                        f'设备 {device_id}\n{detail}\n静默已超 {silent_s:.0f}s。\n'
                        f'（此前已自动重启 {MAX_ATTEMPTS} 次仍无效，请现场检查）',
                        level='critical', event_key=f'failed:{device_id}')
        _save()


def _handle_recovered(device_id, rec, silent_s):
    attempts = rec.get('attempts', 0)
    was_problem = rec.get('status') in ('offline', 'rebooting', 'failed')
    # 只有"确实推送过需人工介入"才补发恢复通知；被去抖跳过的不要发
    had_alerted = bool(rec.get('alerted')) and device_id in _notify_ts
    rec.update({'status': 'online', 'attempts': 0, 'alerted': False,
                'silent_s': 0, 'recovered_at': time.strftime('%Y-%m-%d %H:%M:%S')})
    if was_problem:
        _load()['stats']['recoveries'] = _load()['stats'].get('recoveries', 0) + 1
        detail = (f'已恢复上报（此前静默 {silent_s:.0f}s，尝试重启 {attempts} 次）'
                  if attempts else f'已恢复上报（此前静默 {silent_s:.0f}s）')
        _add_event('info', device_id, 'recovered', detail)
        # 仅当此前发过"需人工介入"才补发恢复通知（否则断链重启**不发外部通知**，
        # 避免"设备已恢复"变成无意义打扰）
        if had_alerted:
            notifier.reset_cooldown(f'failed:{device_id}')
            notifier.notify('设备已恢复', f'设备 {device_id}\n{detail}',
                            level='info', event_key=f'recovered:{device_id}', force=True)
            _notify_ts.pop(device_id, None)     # 恢复后清掉，下次故障可正常通知
    _save()


# ==================== 巡检主循环 ====================

def check_once():
    """执行一轮巡检（也供测试直接调用）"""
    from core import metrics  # 延迟导入，避免循环依赖

    st = _load()
    devices = st['devices']
    try:
        ds = metrics.devices_status(offline_after_s=SILENT_THRESHOLD_S)
    except Exception as e:
        print(f'[自愈] 读取设备状态失败: {type(e).__name__}: {e}')
        return

    now = time.time()
    # 查询前先清理过期的"模拟离线"标记
    for k in [k for k, v in _simulated.items() if v <= now]:
        _simulated.pop(k, None)

    for dev in ds.get('devices', []):
        did = dev.get('device_id')
        if not did:
            continue
        silent = dev.get('silent_s')
        rec = devices.setdefault(did, {'status': 'unknown', 'attempts': 0, 'silent_s': 0})

        # 联调模式：把该设备伪装为长时间静默（不影响真实设备的正常判定）
        simulated = did in _simulated
        if simulated:
            silent = max(silent or 0, SILENT_THRESHOLD_S + 1)
            dev = dict(dev)
            dev['online'] = False

        if silent is None:
            continue
        rec['silent_s'] = silent

        # ---- 开机静默期：防"断电重启误判" ----
        # 服务刚启动时，数据库里的 last_update_ts 可能还是断电前的时间，
        # 会算出巨大 silent_s。这里只对"本次开机后已上报过"的设备做判定；
        # 尚未上报的设备视为"等待首次上报"，不下发重启、不告警。
        # （联调模拟的设备不受此限制，否则刚部署完无法演示）
        if (not simulated and (now - _service_started_at) < STARTUP_GRACE_S
                and did not in _reported_since_start):
            if rec.get('status') not in ('unknown', None):
                rec['status'] = 'unknown'
                rec['attempts'] = 0
                rec['alerted'] = False
            continue

        if dev.get('online'):
            # 恢复确认：需要连续稳定一段时间，避免网络抖动误报
            if rec['status'] in ('offline', 'rebooting', 'failed'):
                rec['online_since'] = rec.get('online_since') or now
                if now - rec['online_since'] >= RECOVER_GRACE_S:
                    _handle_recovered(did, rec, silent)
                continue
            rec.update({'status': 'online', 'attempts': 0, 'online_since': None})
            continue

        rec['online_since'] = None
        if silent < SILENT_THRESHOLD_S:
            rec['status'] = rec.get('status', 'unknown')
            continue

        # 已判定需人工介入：不再重启、不再计数，仅保留告警
        if rec.get('status') == 'failed' and rec.get('alerted'):
            continue

        # 重启后需等 REBOOT_WAIT_S 确认是否回连，期间不重复重启
        if rec.get('status') == 'rebooting':
            if now - (rec.get('last_reboot') or 0) < REBOOT_WAIT_S:
                continue
            # 等待期结束仍静默 → 进入下一轮判定（可能再次重启或判人工介入）

        rec['status'] = 'offline'
        _handle_offline(did, rec, silent)

    st['stats']['last_check'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _save()


def _loop():
    time.sleep(20)      # 启动后先等 20 秒（避开服务启动高峰）
    while True:
        try:
            check_once()
        except Exception as e:
            print(f'[自愈] 巡检异常: {type(e).__name__}: {e}')
        time.sleep(CHECK_INTERVAL_S)


def start():
    """由 app.py 启动时调用（幂等）"""
    global _thread_started, _service_started_at
    if _thread_started:
        return
    _thread_started = True
    _service_started_at = time.time()
    _reported_since_start.clear()

    # 断电场景状态复位：alerts.json 会持久化，但其中的 attempts/alerted 是
    # 上一次运行（可能断电前）的相对状态，新一次开机后不应继承，否则会出现
    # "刚启动就判需人工介入"或"恢复通知逻辑错乱"。
    st = _load()
    for did, rec in st.get('devices', {}).items():
        if rec.get('status') in ('offline', 'rebooting', 'failed'):
            rec.update({'status': 'unknown', 'attempts': 0, 'alerted': False,
                        'silent_s': 0, 'online_since': None})
    _save()

    t = threading.Thread(target=_loop, daemon=True, name='self-heal')
    t.start()
    print(f'[自愈] 离线自愈线程已启动（静默>{SILENT_THRESHOLD_S}s → 重启，最多 {MAX_ATTEMPTS} 次，'
          f'每次等 {REBOOT_WAIT_S}s；开机 {STARTUP_GRACE_S}s 内只判定已上报设备，防断电误判）')


def manual_reboot(device_id):
    """管理员手动重启（不计入自动尝试次数）"""
    ch = _reboot(device_id)
    _add_event('warning' if not ch else 'info', device_id, 'manual_reboot',
               f'管理员手动远控重启（通道 {ch or "不可用"}）')
    _load()['stats']['reboots'] = _load()['stats'].get('reboots', 0) + (1 if ch else 0)
    _save()
    return ch

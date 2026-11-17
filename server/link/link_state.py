# -*- coding: utf-8 -*-
"""
链路状态与下行可靠性（5.4 增强）
================================
两件事：

1) **动态通道选择**（配合节点的网络自适应）
   - 正常时设备走 MQTT（主）；节点检测到网络不通一段时间后会自动降级到 ESP-NOW，
     并继续用 ESP-NOW 上报。
   - 本模块记录每个设备最近一次上报来自哪条通道，`effective_transport()` 据此推断
     "当前真正能用的下行通道"，服务器无需人工干预即可把下行改走 ESP-NOW；
     网络恢复、节点切回 MQTT 后自动回到 MQTT。

2) **下行确认与自动重发**
   - 下发命令带回执序号（seq）；`ACK_TIMEOUT_MS` 内未收到节点 ACK 则重发，
     最多 `RETRY_MAX` 次（总投递 3 次）。重发前重新选路（节点可能刚好切换了通道）。
   - 节点侧对 light/ir/tts 按 seq 去重，重复到达不会重复执行；config 类幂等。

本模块不 import 其它业务模块（避免循环依赖）：发送器由 `register()` 注入。
"""
import threading
import time

ACTIVE_WINDOW_S = 20.0      # 该窗口内有上报 → 认为该通道"当前活跃"
STALE_WINDOW_S = 300.0      # 5 分钟内活跃过也算候选通道
ACK_TIMEOUT_MS = 1500       # 未收到回执的判定超时
RETRY_MAX = 2               # 最多额外重发 2 次（合计 3 次投递）
SCAN_INTERVAL_S = 0.3

_lock = threading.Lock()
_last = {}          # dev -> {'mqtt': ts, 'espnow': ts}
_pending = {}       # (dev, seq) -> {'payload','attempts','sent_ms','transport'}
_senders = {}       # {'mqtt': fn(dev, payload)->bool, 'espnow': fn}
_transport_getter = None
_started = False
_stats = {'sent': 0, 'retry': 0, 'acked': 0, 'timeout': 0,
          'route_mqtt': 0, 'route_espnow': 0, 'fallback': 0}


def _now_ms():
    return int(time.time() * 1000)


def mark_up(dev, transport):
    """记录一次上行（供通道推断）"""
    if not dev:
        return
    t = 'espnow' if transport == 'espnow' else 'mqtt'
    with _lock:
        _last.setdefault(dev.upper(), {})[t] = time.time()


def effective_transport(dev, configured='mqtt'):
    """推断设备当前实际可用的下行通道（优先最近有上报的那条）"""
    d = (dev or '').upper()
    now = time.time()
    with _lock:
        rec = dict(_last.get(d) or {})
    ages = {}
    for t in ('mqtt', 'espnow'):
        ts = rec.get(t)
        ages[t] = (now - ts) if ts else None
    mq, en = ages['mqtt'], ages['espnow']
    if mq is not None and mq <= ACTIVE_WINDOW_S:
        return 'mqtt'
    if en is not None and en <= ACTIVE_WINDOW_S:
        return 'espnow'
    cands = [(a, t) for a, t in ((mq, 'mqtt'), (en, 'espnow'))
             if a is not None and a <= STALE_WINDOW_S]
    if cands:
        return min(cands)[1]
    return configured if configured in ('mqtt', 'espnow') else 'mqtt'


def last_up_age(dev, transport=None):
    """最近一次上行距今多少秒；从未上报返回 None。

    5.7 修复（2026-11-14）新增：自愈模块用它判断"设备是不是已经在走 ESP-NOW
    降级通道"。弱网隔门场景下，节点降级到 ESP-NOW 后静默只是丢包抖动，
    此时重启会打断降级、形成死循环——所以必须能问出"某通道最近还活着吗"。
    """
    d = (dev or '').upper()
    now = time.time()
    with _lock:
        rec = dict(_last.get(d) or {})
    if transport:
        t = 'espnow' if transport == 'espnow' else 'mqtt'
        ts = rec.get(t)
        return (now - ts) if ts else None
    cands = [now - ts for ts in rec.values() if ts]
    return min(cands) if cands else None


def register(senders, transport_getter):
    """注入发送器与选路函数，并启动重发扫描线程"""
    global _senders, _transport_getter, _started
    _senders = dict(senders)
    _transport_getter = transport_getter
    if not _started:
        _started = True
        threading.Thread(target=_retry_loop, daemon=True).start()
        print('[链路] 下行可靠性已启动：ACK 超时 %dms，最多重发 %d 次' %
              (ACK_TIMEOUT_MS, RETRY_MAX))


def _dispatch(dev, transport, payload):
    fn = _senders.get(transport)
    if fn is None:
        return False
    try:
        return bool(fn(dev, payload))
    except Exception as e:
        print(f'[链路] {transport} 下发异常: {e}')
        return False


def send(dev, payload):
    """下发命令：自动选路 + 登记待回执（返回实际使用的通道名）"""
    d = (dev or '').upper()
    seq = payload.get('seq')
    transport = _transport_getter(d) if _transport_getter else 'mqtt'
    ok = _dispatch(d, transport, payload)
    used = transport
    if not ok:
        alt = 'espnow' if transport == 'mqtt' else 'mqtt'
        if _dispatch(d, alt, payload):
            used = alt
            with _lock:
                _stats['fallback'] += 1
            print(f'[链路] {d[:8]} {transport} 下发失败，已改走 {alt}')
    with _lock:
        _stats['sent'] += 1
        _stats['route_' + used] += 1
        if seq is not None:
            _pending[(d, seq)] = {'payload': payload, 'attempts': 0,
                                  'sent_ms': _now_ms(), 'transport': used}
    return used


def notify_ack(dev, seq):
    """节点回执到达（MQTT ack / ESP-NOW ACK 均调用）"""
    key = ((dev or '').upper(), seq)
    with _lock:
        p = _pending.pop(key, None)
        if p is not None:
            _stats['acked'] += 1
    return p is not None


def pending_count():
    with _lock:
        return len(_pending)


def stats():
    with _lock:
        return dict(_stats)


def _retry_loop():
    while True:
        time.sleep(SCAN_INTERVAL_S)
        now = _now_ms()
        with _lock:
            items = list(_pending.items())
        for key, info in items:
            if now - info['sent_ms'] < ACK_TIMEOUT_MS:
                continue
            if info['attempts'] >= RETRY_MAX:
                with _lock:
                    _pending.pop(key, None)
                    _stats['timeout'] += 1
                print(f"[链路] {key[0][:8]} seq={key[1]} 重发 {info['attempts']} 次仍无回执，放弃")
                continue
            info['attempts'] += 1
            info['sent_ms'] = now
            transport = info['transport']
            if _transport_getter is not None:
                transport = _transport_getter(key[0])       # 重发前重新选路
            info['transport'] = transport
            ok = _dispatch(key[0], transport, info['payload'])
            with _lock:
                _stats['retry'] += 1
            print(f"[链路] 重发 {key[0][:8]} seq={key[1]} 第 {info['attempts']} 次 via {transport} ok={ok}")

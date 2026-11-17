# -*- coding: utf-8 -*-
"""
FunASR Paraformer ONNX 识别集成（2026-11-16）
=============================================
为什么换：实测同一段音频，Vosk small 输出
   "打开 行 在 几点 我 是 了 是 温度 客厅 的 开 客厅"（大量噪声词、丢字）
而 FunASR Paraformer ONNX 输出
   "打开现在几点卧室卧室温度客厅的开客厅"（连贯、关键内容完整）

架构差异：
- Vosk = 流式（逐包喂，边收边出）
- FunASR = 离线（必须整段音频一次识别）

因此本模块做：
1. 按设备累积 PCM
2. 后台线程检测"静音超时"（默认 1.8s 无新数据）→ 认为一句话结束
3. 整段喂 FunASR 识别，返回文本

对外接口与 speech.recognize_audio 兼容（返回"本轮新识别文本"，无则空串）。
"""
import os
import threading
import time
import wave

# 延迟导入（模型加载很慢，只在第一次用时加载）
_model = None
_model_lock = threading.Lock()
_model_error = None

MODEL_DIR = '/home/ubuntu/server/models/funasr-onnx'
SILENCE_TIMEOUT_S = 10.0    # 2026-11-16: 主路径改为「结束标记」触发识别，后台线程只做兜底（原 1.8s 会与结束标记抢触发导致重复识别）
MAX_BUFFER_BYTES = 16000 * 2 * 15   # 最多累积 15 秒（防内存无限增长）

_buffers = {}       # device_key -> bytearray
_last_rx = {}       # device_key -> timestamp
_buf_lock = threading.Lock()
_bg_started = False
_results = {}       # device_key -> 待取走的识别文本


def _load_model():
    """惰性加载 FunASR 模型（首次调用约 3~5 秒）"""
    global _model, _model_error
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from funasr_onnx import Paraformer
            t0 = time.time()
            _model = Paraformer(MODEL_DIR, batch_size=1, quantize=True,
                                intra_op_num_threads=4)
            print(f"[FunASR] 模型加载完成，耗时 {time.time()-t0:.1f}s")
        except Exception as e:
            _model_error = f'{type(e).__name__}: {e}'
            print(f"[FunASR] 模型加载失败: {_model_error}")
        return _model


def _recognize_pcm(pcm_bytes):
    """整段 PCM(16kHz/16bit/单声道) → 文本"""
    model = _load_model()
    if model is None:
        return ''
    if len(pcm_bytes) < 16000 * 2 * 0.3:   # 少于 0.3 秒不识别
        return ''
    tmp = f'/tmp/funasr_{threading.get_ident()}_{int(time.time()*1000)}.wav'
    try:
        with wave.open(tmp, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm_bytes)
        result = model(tmp)
        # funasr-onnx 返回 [{'preds': (text, tokens)}]
        if isinstance(result, list) and result:
            preds = result[0].get('preds')
            if isinstance(preds, (tuple, list)) and preds:
                return str(preds[0]).strip()
            if isinstance(preds, str):
                return preds.strip()
        return ''
    except Exception as e:
        print(f"[FunASR] 识别失败: {type(e).__name__}: {e}")
        return ''
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _flush_device(device_key):
    """把某设备累积的 PCM 识别掉"""
    with _buf_lock:
        buf = _buffers.pop(device_key, None)
        _last_rx.pop(device_key, None)
    if not buf:
        return
    if len(buf) < 16000 * 2 * 0.6:   # 2026-11-16：<0.6s 不识别
        return
    text = _recognize_pcm(bytes(buf))
    if text:
        with _buf_lock:
            _results[device_key] = _results.get(device_key, '') + text
        print(f"[FunASR] {device_key[:12]} 识别: {text}")


def _bg_loop():
    """后台线程：检测静音超时 → 触发识别"""
    while True:
        time.sleep(0.3)
        now = time.time()
        expired = []
        with _buf_lock:
            for k, ts in list(_last_rx.items()):
                if now - ts >= SILENCE_TIMEOUT_S:
                    expired.append(k)
        for k in expired:
            _flush_device(k)


def _ensure_bg():
    global _bg_started
    if _bg_started:
        return
    with _buf_lock:
        if _bg_started:
            return
        _bg_started = True
    t = threading.Thread(target=_bg_loop, daemon=True, name='funasr-bg')
    t.start()
    print('[FunASR] 后台识别线程已启动')


def feed(pcm_bytes, device_key='default'):
    """喂入 PCM；返回本轮已完成的识别文本（可能为空串）"""
    _ensure_bg()
    with _buf_lock:
        buf = _buffers.setdefault(device_key, bytearray())
        buf.extend(pcm_bytes)
        # 防爆：超过上限强制识别
        overflow = len(buf) >= MAX_BUFFER_BYTES
        _last_rx[device_key] = time.time()
    if overflow:
        _flush_device(device_key)
    with _buf_lock:
        text = _results.pop(device_key, '')
    return text


def flush(device_key='default'):
    """强制识别某设备当前累积的 PCM（收到"句子结束"标记时调用）。

    与 _flush_device 的区别：这个同步等识别完，并直接返回文本。
    """
    _ensure_bg()
    with _buf_lock:
        buf = _buffers.pop(device_key, None)
        _last_rx.pop(device_key, None)
    if not buf:
        # 可能后台线程已识别并放入 _results
        with _buf_lock:
            return _results.pop(device_key, '')
    # 2026-11-16：过滤太短的缓冲（<0.6s）——实测会误识别出"对对对"之类
    if len(buf) < 16000 * 2 * 0.6:
        print(f"[FunASR] 缓冲过短丢弃: {len(buf)} 字节 (<0.6s)")
        return ''
    text = _recognize_pcm(bytes(buf))
    # 2026-11-16：不在这里打印，避免与 speech.py 的 [Stream] 日志重复
    return text


def is_available():
    """模型是否可用（未加载时尝试加载）"""
    return _load_model() is not None

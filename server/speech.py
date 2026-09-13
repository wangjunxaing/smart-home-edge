# -*- coding: utf-8 -*-
"""
语音识别模块 - Vosk 离线识别 + 唤醒词 + 麦克风控制 Blueprint
==========================================================
持续监听模式: ESP32 每2秒POST PCM → Vosk 识别 → 唤醒词检测 → LLM
识别文字带时间戳写入 voice_log.txt
"""
import os
import json
import time
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify

speech_bp = Blueprint('speech', __name__)

try:
    import vosk
    VOSK_AVAILABLE = True
except ImportError:
    VOSK_AVAILABLE = False
    print("[Vosk] vosk 模块未安装，语音识别将返回占位文本")

VOSK_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'vosk-model', 'vosk-model-small-cn-0.22')
vosk_model = None
mic_record_requested = False
last_recognition_result = None

WAKE_WORD = "\u5c0f\u667a"  # 小智

# 语音日志文件路径（与 speech.py 同目录）
VOICE_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'voice_log.txt')


def init_vosk():
    global vosk_model
    if not VOSK_AVAILABLE:
        return
    try:
        if os.path.isdir(VOSK_MODEL_PATH):
            vosk_model = vosk.Model(VOSK_MODEL_PATH)
            print(f"[Vosk] 模型加载成功: {VOSK_MODEL_PATH}")
        else:
            print(f"[Vosk] 模型路径不存在: {VOSK_MODEL_PATH}")
    except Exception as e:
        print(f"[Vosk] 模型加载失败: {e}")


# ---- 持久识别会话（按来源设备维护）----
# ESP32 语音突发期间每1秒POST一个PCM包；若每包新建识别器，
# 一句话被切成多段且 Vosk 内部状态丢失，长句几乎必然识别为空。
# 持久识别器连续喂入，Vosk 在语句收尾（尾部静音）时返回完整文本。
_sessions = {}
_sessions_lock = threading.Lock()
SESSION_GAP_FLUSH = 3.0    # 超过3秒无新包：旧语句未收尾，先冲刷再开新会话
SESSION_MAX_AGE = 300.0    # 闲置会话回收


class _RecogSession:
    __slots__ = ('rec', 'last_ts')

    def __init__(self, model, rate):
        self.rec = vosk.KaldiRecognizer(model, rate)
        self.last_ts = time.time()


def recognize_audio(pcm_bytes, sample_rate=16000, device_key='default'):
    """PCM 喂入持久识别器，返回本轮新识别出的完整文本（无则空串）"""
    if vosk_model is None:
        return ""
    now = time.time()
    texts = []
    with _sessions_lock:
        sess = _sessions.get(device_key)
        if sess is not None and now - sess.last_ts > SESSION_GAP_FLUSH:
            try:
                tail = json.loads(sess.rec.FinalResult()).get('text', '').strip()
            except Exception:
                tail = ''
            if tail:
                texts.append(tail)
            sess = None
        if sess is None:
            sess = _RecogSession(vosk_model, sample_rate)
            _sessions[device_key] = sess
        sess.last_ts = now
        try:
            if sess.rec.AcceptWaveform(pcm_bytes):
                seg = json.loads(sess.rec.Result()).get('text', '').strip()
                if seg:
                    texts.append(seg)
        except Exception as e:
            print(f"[Vosk] 识别出错: {e}")
            _sessions.pop(device_key, None)
            return ""
        if len(_sessions) > 8:
            for k in [k for k, s in _sessions.items()
                      if now - s.last_ts > SESSION_MAX_AGE]:
                _sessions.pop(k, None)
    return ' '.join(texts).strip()


def save_voice_log(text):
    """将识别文本带时间戳写入 voice_log.txt"""
    if not text:
        return
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"{timestamp} | {text}\n"
    try:
        with open(VOICE_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line)
        print(f"[VoiceLog] {line.strip()}")
    except Exception as e:
        print(f"[VoiceLog] 写入失败: {e}")


# Vosk 中文小模型常把"小智"误识别为同音字，一并视为唤醒
WAKE_VARIANTS = ["小智", "小志", "小制", "小治", "小知"]


def normalize_text(text):
    """去除识别文本空格（Vosk 中文按词输出带空格，中文语义不需要）"""
    return ''.join(text.split())


def check_wake_word(text):
    """检测唤醒词（含同音变体），返回 (是否唤醒, 唤醒词后的内容)"""
    if not text:
        return False, text
    words = [WAKE_WORD] + [w for w in WAKE_VARIANTS if w != WAKE_WORD]
    for w in words:
        if w and w in text:
            idx = text.find(w) + len(w)
            after = text[idx:].strip()
            return True, after if after else text
    return False, text


@speech_bp.route('/api/mic/wakeword', methods=['GET'])
def api_get_wakeword():
    global WAKE_WORD
    return jsonify({'wake_word': WAKE_WORD})


@speech_bp.route('/api/mic/wakeword', methods=['POST'])
def api_set_wakeword():
    global WAKE_WORD
    data = request.get_json()
    word = data.get('wake_word', '').strip()
    if word:
        WAKE_WORD = word
        print(f"[唤醒词] 修改为: {WAKE_WORD}")
        return jsonify({'status': 'ok', 'wake_word': WAKE_WORD})
    return jsonify({'status': 'error', 'message': '唤醒词不能为空'}), 400


@speech_bp.route('/api/mic/start', methods=['POST'])
def api_mic_start():
    global mic_record_requested, last_recognition_result
    mic_record_requested = True
    last_recognition_result = None
    print("[麦克风] 录音任务已设置")
    return jsonify({'status': 'ok', 'message': '录音指令已发送'})


@speech_bp.route('/api/mic/task', methods=['GET'])
def api_mic_task():
    global mic_record_requested
    if mic_record_requested:
        mic_record_requested = False
        return jsonify({'record': True})
    return jsonify({'record': False}), 404


@speech_bp.route('/api/mic/stream', methods=['POST'])
def api_mic_stream():
    """ESP32 每2秒POST一段PCM原始音频，Vosk识别后写入日志"""
    global last_recognition_result
    audio_data = request.get_data()
    if not audio_data or len(audio_data) < 500:
        return jsonify({"status": "error", "message": "too short"}), 400

    # 按来源设备维护持久识别器，一句话的多个PCM包连续喂入
    device_key = request.remote_addr or 'default'
    text = recognize_audio(audio_data, device_key=device_key)

    if not text:
        # 本轮无新文本：不覆盖上次结果，避免前端轮询拿到空结果
        return jsonify({"status": "ok", "recognized": "",
                        "wake": False, "question": "",
                        "bytes": len(audio_data)})

    text = normalize_text(text)
    print(f"[Stream] 识别: {text}")
    is_wake, question = check_wake_word(text)
    save_voice_log(text)

    if is_wake:
        print(f"[唤醒] 检测到唤醒词! 问题: {question}")

    last_recognition_result = json.dumps({
        "recognized": text,
        "wake": is_wake,
        "question": question
    }, ensure_ascii=False)

    return jsonify({
        "status": "ok",
        "recognized": text,
        "wake": is_wake,
        "question": question,
        "bytes": len(audio_data)
    })


@speech_bp.route('/api/mic/upload', methods=['POST'])
def api_mic_upload():
    global last_recognition_result
    audio_data = request.get_data()
    if not audio_data or len(audio_data) < 100:
        return jsonify({'status': 'error', 'message': '音频数据无效'}), 400

    pcm_data = audio_data[44:] if len(audio_data) > 44 else audio_data
    print(f"[麦克风] 收到音频: {len(audio_data)} 字节 (PCM: {len(pcm_data)} 字节)")

    debug_audio_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_debug')
    os.makedirs(debug_audio_dir, exist_ok=True)
    audio_filename = f"mic_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
    audio_path = os.path.join(debug_audio_dir, audio_filename)
    with open(audio_path, 'wb') as f:
        f.write(audio_data)

    recognized_text = recognize_audio(pcm_data)
    print(f"[麦克风] 识别结果: {recognized_text}")

    if recognized_text:
        save_voice_log(recognized_text)

    is_wake, question = check_wake_word(recognized_text)
    if is_wake:
        print(f"[唤醒] 检测到唤醒词, 问题: {question}")

    result_data = json.dumps({
        'recognized': recognized_text,
        'wake': is_wake,
        'question': question
    }, ensure_ascii=False)
    last_recognition_result = result_data

    return jsonify({
        'status': 'ok',
        'recognized': recognized_text,
        'wake': is_wake,
        'question': question,
        'audio_file': audio_filename
    })


@speech_bp.route('/api/mic/upload/result', methods=['GET'])
def api_mic_upload_result():
    global last_recognition_result
    if last_recognition_result is not None:
        try:
            result = json.loads(last_recognition_result)
        except Exception:
            result = {'recognized': last_recognition_result, 'wake': False, 'question': ''}
        last_recognition_result = None
        return jsonify(result)
    return jsonify({'recognized': None, 'wake': False}), 404
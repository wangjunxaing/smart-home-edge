# -*- coding: utf-8 -*-
"""
固件 OTA 管理模块 — Blueprint（5.2 签名 + 审计）
===============================================
ESP32 固件的托管、检查更新与下载。

目录结构 (server/firmware/):
  manifest.json   当前最新固件清单 {version, filename, size, md5, sha256, sig, uploaded_at, note}
  *.bin           固件文件（仅保留最新一个）
  devices.json    各设备当前固件版本 {device_id: {fw, last_seen}}
  audit.log       审计日志（上传事件 + 设备版本迁移）

签名链（5.2）:
  上传时对固件 SHA-256（小写 hex 字符串）做 RSA-2048 签名（私钥 keys/ota_rsa_priv.pem，
  仅存服务器）；下载响应携带 x-SHA256 + x-Signature，v1.7.0+ 固件验签后才写入。
  旧固件（≤1.6.x）走 HTTPUpdate + x-MD5 不受影响（两套响应头并存，灰度迁移）。

升级链路:
  ESP32 每 30 秒 GET /api/esp32/firmware/check?device_id=..&current=..
  → 有新版本返回 {"update": true, ...}
  → ESP32 从 /firmware/file 下载并校验签名
"""
import os
import re
import json
import time
import base64
import hashlib
import subprocess
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify, send_file

from auth import login_required, admin_required

firmware_bp = Blueprint('firmware', __name__)

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_DIR = os.path.join(SERVER_DIR, 'firmware')
MANIFEST_PATH = os.path.join(FIRMWARE_DIR, 'manifest.json')
DEVICES_PATH = os.path.join(FIRMWARE_DIR, 'devices.json')
AUDIT_PATH = os.path.join(FIRMWARE_DIR, 'audit.log')
KEYS_DIR = os.path.join(SERVER_DIR, 'keys')
OTA_PRIV = os.path.join(KEYS_DIR, 'ota_rsa_priv.pem')
MAX_FIRMWARE_SIZE = 2 * 1024 * 1024   # OTA 分区约 1.3MB，2MB 上限留有余量

_lock = threading.Lock()
_devices_cache = None   # 设备版本缓存（懒加载，避免每次请求读盘）


def _load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_manifest():
    return _load_json(MANIFEST_PATH) or {}


def _audit(msg):
    """追加审计日志（上传/设备版本迁移）"""
    try:
        os.makedirs(FIRMWARE_DIR, exist_ok=True)
        with open(AUDIT_PATH, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception as e:
        print(f"[固件] 审计写入失败: {e}")


def _sign_sha256(sha256_hex):
    """对固件 SHA-256 hex 串做 RSA-2048 签名，返回 base64；密钥缺失/失败返回 ''"""
    if not os.path.isfile(OTA_PRIV):
        return ''
    try:
        p = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', OTA_PRIV],
                           input=sha256_hex.encode(), capture_output=True, timeout=15)
        if p.returncode == 0 and p.stdout:
            return base64.b64encode(p.stdout).decode()
        print(f"[固件] 签名失败 rc={p.returncode}")
    except Exception as e:
        print(f"[固件] 签名异常: {e}")
    return ''


def _record_device(device_id, fw):
    """记录设备当前固件版本与在线时间（版本变化立即写盘，否则 5 分钟节流）"""
    global _devices_cache
    if not device_id:
        return
    with _lock:
        if _devices_cache is None:
            _devices_cache = _load_json(DEVICES_PATH) or {}
        dev = _devices_cache.get(device_id) or {}
        fw_changed = dev.get('fw') != fw
        stale = True
        old_seen = dev.get('last_seen', '')
        if old_seen:
            try:
                dt = datetime.now() - datetime.strptime(old_seen, '%Y-%m-%d %H:%M:%S')
                stale = dt.total_seconds() > 300
            except ValueError:
                stale = True
        if fw_changed or stale:
            old_fw = dev.get('fw')
            _devices_cache[device_id] = {
                'fw': fw,
                'last_seen': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            _save_json(DEVICES_PATH, _devices_cache)
            # 5.2 审计：设备版本迁移（首次见到的不记）
            if fw_changed and old_fw:
                _audit(f"device {device_id[:8]}: fw {old_fw} -> {fw}")


# ==================== ESP32 设备端接口 ====================

@firmware_bp.route('/api/esp32/firmware/check', methods=['GET'])
def api_firmware_check():
    device_id = request.args.get('device_id', '')
    current = request.args.get('current', '')
    _record_device(device_id, current)

    manifest = _get_manifest()
    version = manifest.get('version')
    bin_path = os.path.join(FIRMWARE_DIR, manifest.get('filename', ''))
    if version and version != current and os.path.isfile(bin_path):
        return jsonify({
            'update': True,
            'version': version,
            'path': '/firmware/file',
            'size': manifest.get('size', 0)
        })
    return jsonify({'update': False})


@firmware_bp.route('/firmware/file', methods=['GET'])
def firmware_file():
    manifest = _get_manifest()
    filename = manifest.get('filename', '')
    path = os.path.join(FIRMWARE_DIR, filename)
    if not filename or not os.path.isfile(path):
        return "no firmware", 404
    resp = send_file(path, mimetype='application/octet-stream',
                     as_attachment=True, download_name='firmware.bin')
    # 旧固件(≤1.6.x)：HTTPUpdate 靠 x-MD5 做完整性校验（保留）
    if manifest.get('md5'):
        resp.headers['x-MD5'] = manifest['md5']
    # 新固件(v1.7.0+)：x-SHA256 + x-Signature，设备验摘要与 RSA 签名后才写入
    if manifest.get('sha256'):
        resp.headers['x-SHA256'] = manifest['sha256']
    if manifest.get('sig'):
        resp.headers['x-Signature'] = manifest['sig']
    return resp


# ==================== 管理端接口（需登录） ====================

@firmware_bp.route('/api/admin/firmware/upload', methods=['POST'])
@admin_required
def api_firmware_upload():
    file = request.files.get('file')
    version = (request.form.get('version') or '').strip()
    note = (request.form.get('note') or '').strip()[:100]

    if not file or not file.filename:
        return jsonify({'status': 'error', 'message': '未选择文件'})
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        return jsonify({'status': 'error', 'message': '版本号格式应为 x.y.z（如 1.1.1）'})

    data = file.read()
    if len(data) == 0:
        return jsonify({'status': 'error', 'message': '文件为空'})
    if len(data) > MAX_FIRMWARE_SIZE:
        return jsonify({'status': 'error', 'message': '固件超过 2MB 上限'})

    os.makedirs(FIRMWARE_DIR, exist_ok=True)

    # 只保留最新固件，清理旧 bin
    filename = f'firmware-{version}.bin'
    for old in os.listdir(FIRMWARE_DIR):
        if old.endswith('.bin') and old != filename:
            os.remove(os.path.join(FIRMWARE_DIR, old))

    with open(os.path.join(FIRMWARE_DIR, filename), 'wb') as fp:
        fp.write(data)

    md5 = hashlib.md5(data).hexdigest()
    sha256 = hashlib.sha256(data).hexdigest()
    sig = _sign_sha256(sha256)
    manifest = {
        'version': version,
        'filename': filename,
        'size': len(data),
        'md5': md5,
        'sha256': sha256,
        'sig': sig,
        'uploaded_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'note': note
    }
    _save_json(MANIFEST_PATH, manifest)
    signed = '已签名' if sig else '!! 未签名（缺私钥）'
    _audit(f"upload v{version} size={len(data)} sha256={sha256[:12]} {signed} note={note}")
    print(f"[固件] 上传 v{version}（{len(data)} 字节，{signed}），设备将在 30 秒内检测到更新")
    return jsonify({'status': 'ok', 'version': version, 'signed': bool(sig)})


@firmware_bp.route('/api/admin/firmware/manifest', methods=['GET'])
@admin_required
def api_firmware_manifest():
    global _devices_cache
    with _lock:
        if _devices_cache is None:
            _devices_cache = _load_json(DEVICES_PATH) or {}
        devices = dict(_devices_cache)
    return jsonify({'manifest': _get_manifest(), 'devices': devices})


@firmware_bp.route('/api/admin/firmware/audit', methods=['GET'])
@admin_required
def api_firmware_audit():
    """固件审计（5.2）：上传/签名状态 + 设备版本迁移记录"""
    tail = 100
    try:
        lines = open(AUDIT_PATH, encoding='utf-8').read().splitlines()[-tail:]
    except Exception:
        lines = []
    return jsonify({'audit': lines, 'manifest': _get_manifest(),
                    'key_ok': os.path.isfile(OTA_PRIV)})
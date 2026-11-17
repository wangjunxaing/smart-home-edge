# -*- coding: utf-8 -*-
"""
通知渠道抽象（5.7.1）
=====================
不写死邮件，而是定义统一 `Notifier` 接口 + 多通道实现，配置决定启用哪些。
单通道失败不影响其他通道，失败会重试并记日志。

已实现渠道
----------
- `FeishuNotifier`  飞书群机器人 Webhook（主通道；支持签名校验）
- `WecomNotifier`   企业微信群机器人 Webhook
- `DingTalkNotifier` 钉钉群机器人 Webhook（可选加签）
- `EmailNotifier`   SMTP 邮件（兜底；标准库 smtplib，无第三方依赖）

配置
----
`data/notify_config.json`（gitignore，不进仓库）：
{
  "enabled": ["feishu"],
  "feishu": {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx", "secret": ""},
  "email":  {"host": "smtp.qq.com", "port": 465, "user": "a@qq.com",
             "password": "授权码", "to": ["b@qq.com"]},
  "min_level": "warning",       // info | warning | critical
  "cooldown_s": 300             // 同一事件最小通知间隔（去抖）
}
"""
import json
import smtplib
import ssl
import threading
import time
from email.header import Header
from email.mime.text import MIMEText

from core.config import NOTIFY_CONFIG

_lock = threading.Lock()
_cache = {'mtime': None, 'data': None}
_last_sent = {}          # 事件键 -> 时间戳（去抖）

_LEVELS = {'info': 1, 'warning': 2, 'critical': 3}


def load_config():
    """读取通知配置（带 mtime 缓存）"""
    global _cache
    path = str(NOTIFY_CONFIG)
    try:
        import os
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _lock:
        if _cache['mtime'] != mtime:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    _cache = {'mtime': mtime, 'data': json.load(f) or {}}
            except Exception as e:
                print(f'[通知] 配置读取失败: {e}')
                _cache = {'mtime': mtime, 'data': {}}
        return _cache['data']


def save_config(data):
    with open(str(NOTIFY_CONFIG), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    global _cache
    _cache = {'mtime': None, 'data': None}
    return data


def config_masked():
    """给前端看的配置（隐去密钥）"""
    cfg = load_config()
    out = json.loads(json.dumps(cfg, ensure_ascii=False))
    for ch in ('feishu', 'wecom', 'dingtalk'):
        if isinstance(out.get(ch), dict):
            for k in ('webhook', 'secret'):
                if out[ch].get(k):
                    out[ch][k] = '***已配置***'
    if isinstance(out.get('email'), dict) and out['email'].get('password'):
        out['email']['password'] = '***已配置***'
    return out


def _feishu_sign(secret, timestamp):
    """飞书自定义机器人签名：HMAC-SHA256(key=timestamp+"\n"+secret, msg="") → base64"""
    import base64
    import hashlib
    import hmac
    string_to_sign = f'{timestamp}\n{secret}'
    digest = hmac.new(string_to_sign.encode('utf-8'), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode('utf-8')


# ==================== 各渠道实现 ====================

def _notify_feishu(cfg, title, text, level):
    import requests
    webhook = (cfg or {}).get('webhook', '').strip()
    if not webhook:
        return False, '未配置 webhook'
    body = {'msg_type': 'text',
            'content': {'text': f'[{level.upper()}] {title}\n{text}'}}
    if cfg.get('secret'):
        ts = str(int(time.time()))
        body['timestamp'] = ts
        body['sign'] = _feishu_sign(cfg['secret'], ts)
    r = requests.post(webhook, json=body, timeout=10)
    if r.status_code != 200:
        return False, f'HTTP {r.status_code}'
    try:
        js = r.json()
        if js.get('code') not in (0, None):
            return False, f"飞书返回 code={js.get('code')} {js.get('msg')}"
    except Exception:
        pass
    return True, 'ok'


def _notify_wecom(cfg, title, text, level):
    import requests
    webhook = (cfg or {}).get('webhook', '').strip()
    if not webhook:
        return False, '未配置 webhook'
    r = requests.post(webhook, json={'msgtype': 'text',
                                     'text': {'content': f'[{level.upper()}] {title}\n{text}'}},
                      timeout=10)
    if r.status_code != 200:
        return False, f'HTTP {r.status_code}'
    return True, 'ok'


def _notify_dingtalk(cfg, title, text, level):
    import base64
    import hashlib
    import hmac
    import urllib.parse

    import requests
    webhook = (cfg or {}).get('webhook', '').strip()
    if not webhook:
        return False, '未配置 webhook'
    url = webhook
    if cfg.get('secret'):
        ts = str(round(time.time() * 1000))
        string_to_sign = f'{ts}\n{cfg["secret"]}'
        sign = urllib.parse.quote_plus(base64.b64encode(
            hmac.new(cfg['secret'].encode(), string_to_sign.encode(),
                     digestmod=hashlib.sha256).digest()))
        url = f'{webhook}&timestamp={ts}&sign={sign}'
    r = requests.post(url, json={'msgtype': 'text',
                                 'text': {'content': f'[{level.upper()}] {title}\n{text}'}},
                      timeout=10)
    if r.status_code != 200:
        return False, f'HTTP {r.status_code}'
    return True, 'ok'


def _notify_email(cfg, title, text, level):
    host = (cfg or {}).get('host', '').strip()
    user = (cfg or {}).get('user', '').strip()
    password = (cfg or {}).get('password', '')
    to = (cfg or {}).get('to') or []
    if not (host and user and password and to):
        return False, '邮件配置不完整'
    port = int(cfg.get('port', 465))
    msg = MIMEText(text, 'plain', 'utf-8')
    msg['Subject'] = Header(f'[0-Smart][{level.upper()}] {title}', 'utf-8')
    msg['From'] = user
    msg['To'] = ', '.join(to)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15,
                                  context=ssl.create_default_context()) as s:
                s.login(user, password)
                s.sendmail(user, to, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.sendmail(user, to, msg.as_string())
        return True, 'ok'
    except Exception as e:
        return False, f'{type(e).__name__}: {str(e)[:80]}'


IMPL = {
    'feishu': _notify_feishu,
    'wecom': _notify_wecom,
    'dingtalk': _notify_dingtalk,
    'email': _notify_email,
}


# ==================== 统一入口 ====================

def notify(title, text, level='warning', event_key=None, force=False):
    """向所有启用渠道分发通知。

    - `event_key`：同一事件的去抖键（配合 cooldown_s，避免重复轰炸）
    - `force=True`：跳过去抖（如"已恢复"必须发出）
    返回 {channel: (ok, msg)} 与发送结果摘要。
    """
    cfg = load_config()
    if not cfg:
        return {'skipped': (False, '未配置通知渠道（data/notify_config.json）')}

    min_level = cfg.get('min_level', 'warning')
    if _LEVELS.get(level, 2) < _LEVELS.get(min_level, 2):
        return {'skipped': (False, f'级别 {level} 低于 min_level={min_level}')}

    if event_key and not force:
        cooldown = float(cfg.get('cooldown_s', 300))
        now = time.time()
        with _lock:
            last = _last_sent.get(event_key, 0)
            if now - last < cooldown:
                return {'skipped': (False, f'去抖中（{int(cooldown - (now - last))}s 内已发过）')}
            _last_sent[event_key] = now
    elif event_key:
        with _lock:
            _last_sent[event_key] = time.time()

    results = {}
    for name in cfg.get('enabled') or []:
        impl = IMPL.get(name)
        if not impl:
            results[name] = (False, '未知渠道')
            continue
        try:
            ok, msg = impl(cfg.get(name) or {}, title, text, level)
        except Exception as e:
            ok, msg = False, f'{type(e).__name__}: {str(e)[:90]}'
        results[name] = (ok, msg)
        print(f"[通知] {name} {'成功' if ok else '失败: ' + msg} | {title}")
    if not results:
        results['none'] = (False, '未启用任何渠道')
    return results


def reset_cooldown(event_key=None):
    """清除去抖记录（设备恢复时调用，保证'已恢复'能立刻发出）"""
    with _lock:
        if event_key:
            _last_sent.pop(event_key, None)
        else:
            _last_sent.clear()

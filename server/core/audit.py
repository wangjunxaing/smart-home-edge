# -*- coding: utf-8 -*-
"""
操作审计子系统（5.13）
=====================
动机：4.3 已有 admin/user **权限体系**，但**没有审计**——灯带/红外/TTS/语音/
AI 工具/自动化触发的所有操作都不留痕，出问题无法追责、无法复现、无法回滚。

设计要点
--------
1. **统一收口**：所有下行都经 `link/downlink.send_downlink()`，
   在那里记录最不容易漏（比在每个 API 里各埋各的可靠）。
2. **来源区分**（source）：
   - `web`        网页管理操作
   - `voice`      语音指令
   - `ai_tool`    LLM 工具调用
   - `automation` 阈值规则触发
   - `system`     自愈重启等系统动作
3. **可撤销**：可逆动作（灯带、米家插座）执行前把"上一次状态"存进
   `action_snapshot`；撤销时恢复，并**把撤销本身也记一条审计**。
4. **不阻塞主流程**：审计任何异常都吞掉（打日志），绝不能让"记录失败"
   导致"控制失败"。

独立数据库 `data/audit.db`，避免污染传感器库。
"""
import json
import sqlite3
import threading
import contextvars
from datetime import datetime

from core.config import DATA_DIR

AUDIT_DB = DATA_DIR / 'audit.db'
SNAPSHOT_KEEP = 50          # 每个 (device_id, action) 只保留最近 N 条快照

_lock = threading.Lock()
_inited = False

# ---- 操作来源上下文（5.13）----
# 用 contextvars 把"当前是谁触发的"透传到 link/downlink.send_downlink()，
# 避免为了审计去改一堆函数签名（set_light_state / mi_control / ir_send ...）。
# 用法：
#   with audit.audit_context('ai_tool', 'ai'):
#       execute_tool(...)
_src_ctx = contextvars.ContextVar('audit_source', default=None)
_actor_ctx = contextvars.ContextVar('audit_actor', default=None)


class audit_context:
    """临时设置当前操作来源；退出时恢复。"""
    def __init__(self, source, actor=''):
        self.source = source
        self.actor = actor
        self._ts = None
        self._ta = None

    def __enter__(self):
        self._ts = _src_ctx.set(self.source)
        self._ta = _actor_ctx.set(self.actor)
        return self

    def __exit__(self, *exc):
        if self._ts is not None:
            _src_ctx.reset(self._ts)
        if self._ta is not None:
            _actor_ctx.reset(self._ta)
        return False


def current_context():
    """返回 (source, actor)；未设置时为 (None, None)。"""
    return _src_ctx.get(), _actor_ctx.get()


def _conn():
    conn = sqlite3.connect(str(AUDIT_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建表（幂等）"""
    global _inited
    with _lock:
        if _inited:
            return
        conn = _conn()
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS operation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT,
                actor TEXT,
                device_id TEXT,
                action TEXT,
                params TEXT,
                result TEXT,
                channel TEXT,
                elapsed_ms INTEGER DEFAULT 0,
                undone INTEGER DEFAULT 0,
                undo_of INTEGER
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_op_ts ON operation_log(ts)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_op_dev ON operation_log(device_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_op_source ON operation_log(source)')
        c.execute('''
            CREATE TABLE IF NOT EXISTS action_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                action TEXT,
                state TEXT,
                ts TEXT,
                used INTEGER DEFAULT 0
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_snap_dev '
                  'ON action_snapshot(device_id, action)')
        conn.commit()
        conn.close()
        _inited = True
        print("[审计] 数据库初始化完成（operation_log / action_snapshot）")


def _payload_to_action(payload):
    """把下行 payload 的 type 映射成可读的 action 名。"""
    t = (payload or {}).get('type', '')
    return {
        'light': 'light_set',
        'ir': 'ir_send',
        'tts': 'tts_speak',
        'reboot': 'device_reboot',
        'config': 'device_config',
        'mi': 'mi_control',
        'mic': 'mic_control',
    }.get(t, t or 'unknown')


def log_operation(source, actor, device_id, payload, result,
                  channel='', elapsed_ms=0, undo_of=None):
    """记录一条操作；返回日志 id（失败返回 None，绝不抛异常）。"""
    try:
        init_db()
        action = _payload_to_action(payload)
        # 参数里去掉体积大的字段（如 TTS 的 hex 可保留，但截断超长的）
        params = json.dumps(payload, ensure_ascii=False)
        if len(params) > 1000:
            params = params[:1000] + '...'
        conn = _conn()
        c = conn.cursor()
        c.execute('''
            INSERT INTO operation_log
            (ts, source, actor, device_id, action, params, result, channel, elapsed_ms, undo_of)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
              source or 'unknown', actor or '', device_id or '',
              action, params, result or 'unknown', channel or '',
              int(elapsed_ms or 0), undo_of))
        conn.commit()
        rid = c.lastrowid
        conn.close()
        return rid
    except Exception as e:
        print(f'[审计] 写入失败（已忽略，不影响主流程）: {type(e).__name__}: {e}')
        return None


def save_snapshot(device_id, action, state):
    """保存"动作执行前"的状态，供撤销使用。"""
    try:
        init_db()
        conn = _conn()
        c = conn.cursor()
        c.execute('INSERT INTO action_snapshot (device_id, action, state, ts) '
                  'VALUES (?, ?, ?, ?)',
                  (device_id or '', action or '',
                   json.dumps(state, ensure_ascii=False),
                   datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        # 清理：每个 (device_id, action) 只留最近 SNAPSHOT_KEEP 条未用快照
        c.execute('''
            DELETE FROM action_snapshot WHERE id IN (
                SELECT id FROM action_snapshot
                WHERE device_id = ? AND action = ? AND used = 0
                ORDER BY id DESC LIMIT -1 OFFSET ?
            )
        ''', (device_id or '', action or '', SNAPSHOT_KEEP))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[审计] 快照保存失败: {type(e).__name__}: {e}')
        return False


def take_snapshot(device_id, action):
    """取出最近一条未使用的快照（取后标记 used=1，防止重复撤销）。"""
    try:
        init_db()
        conn = _conn()
        c = conn.cursor()
        row = c.execute(
            'SELECT id, state, ts FROM action_snapshot '
            'WHERE device_id = ? AND action = ? AND used = 0 '
            'ORDER BY id DESC LIMIT 1',
            (device_id or '', action or '')).fetchone()
        if not row:
            conn.close()
            return None
        c.execute('UPDATE action_snapshot SET used = 1 WHERE id = ?', (row['id'],))
        conn.commit()
        conn.close()
        return {'id': row['id'], 'state': json.loads(row['state']), 'ts': row['ts']}
    except Exception as e:
        print(f'[审计] 取快照失败: {type(e).__name__}: {e}')
        return None


def get_operation(op_id):
    try:
        init_db()
        conn = _conn()
        row = conn.execute('SELECT * FROM operation_log WHERE id = ?',
                           (op_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def mark_undone(op_id, undo_op_id):
    try:
        conn = _conn()
        conn.execute('UPDATE operation_log SET undone = 1 WHERE id = ?', (op_id,))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def query(source=None, device_id=None, action=None, result=None,
          limit=50, offset=0, since=None):
    """分页查询（新的在前）。"""
    try:
        init_db()
        where = []
        args = []
        if source:
            where.append('source = ?')
            args.append(source)
        if device_id:
            where.append('device_id LIKE ?')
            args.append(device_id + '%')
        if action:
            where.append('action LIKE ?')
            args.append('%' + action + '%')
        if result:
            where.append('result = ?')
            args.append(result)
        if since:
            where.append('ts >= ?')
            args.append(since)
        sql = 'SELECT * FROM operation_log'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY id DESC LIMIT ? OFFSET ?'
        args.extend([max(1, min(int(limit), 500)), max(0, int(offset))])

        conn = _conn()
        rows = [dict(r) for r in conn.execute(sql, args)]
        total = conn.execute('SELECT count(*) FROM operation_log').fetchone()[0]
        conn.close()
        return {'items': rows, 'total': total}
    except Exception as e:
        print(f'[审计] 查询失败: {type(e).__name__}: {e}')
        return {'items': [], 'total': 0}


def stats():
    """概览：各来源计数、今日操作数、失败数。"""
    try:
        init_db()
        conn = _conn()
        today = datetime.now().strftime('%Y-%m-%d')
        by_source = {}
        for r in conn.execute('SELECT source, count(*) n FROM operation_log GROUP BY source'):
            by_source[r['source'] or 'unknown'] = r['n']
        total = conn.execute('SELECT count(*) FROM operation_log').fetchone()[0]
        today_n = conn.execute(
            "SELECT count(*) FROM operation_log WHERE ts LIKE ?",
            (today + '%',)).fetchone()[0]
        fail_n = conn.execute(
            "SELECT count(*) FROM operation_log WHERE result = 'fail'").fetchone()[0]
        undo_n = conn.execute(
            'SELECT count(*) FROM operation_log WHERE undone = 1').fetchone()[0]
        conn.close()
        return {'total': total, 'today': today_n, 'failed': fail_n,
                'undone': undo_n, 'by_source': by_source}
    except Exception:
        return {'total': 0, 'today': 0, 'failed': 0, 'undone': 0, 'by_source': {}}

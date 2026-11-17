# -*- coding: utf-8 -*-
"""
数据库模块 — 连接工厂与表初始化
==============================
用户系统 (users.db) + 传感器系统 (sensors_history.db) 完全分离。
"""
import sqlite3

from core.config import USERS_DB, SENSORS_DB

DATABASE = str(USERS_DB)
SENSORS_DB = str(SENSORS_DB)


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def get_sensors_db():
    conn = sqlite3.connect(SENSORS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_sensors_db():
    """传感器数据库三表结构：history(全量) / latest(快照) / device_config(配置)"""
    conn = get_sensors_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sensor_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            esp32_id TEXT NOT NULL,
            location TEXT DEFAULT '未分类',
            temperature REAL,
            humidity REAL,
            lux REAL,
            timestamp DATETIME
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sensor_latest (
            esp32_id TEXT PRIMARY KEY,
            location TEXT DEFAULT '未分类',
            temperature REAL DEFAULT -1.0,
            humidity REAL DEFAULT -1.0,
            lux REAL DEFAULT -1.0,
            rssi REAL DEFAULT -100.0,
            last_update TEXT,
            last_update_ts TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS device_config (
            device_id TEXT PRIMARY KEY,
            send_interval INTEGER DEFAULT 5000,
            location TEXT DEFAULT '未分类',
            alias TEXT DEFAULT '',
            payload_pad INTEGER DEFAULT 0
        )
    ''')
    # 迁移：device_config 增加 transport 列（5.1：http/mqtt 传输模式，默认 http）
    cols = [r[1] for r in cursor.execute('PRAGMA table_info(device_config)')]
    if 'transport' not in cols:
        cursor.execute("ALTER TABLE device_config ADD COLUMN transport TEXT DEFAULT 'http'")
        print("[Database] 迁移：device_config 表新增 transport 列")
    # 迁移：device_config 增加 payload_pad 列（5.4：T5 上行载荷扫描，填充字节数 0~80）
    if 'payload_pad' not in cols:
        cursor.execute("ALTER TABLE device_config ADD COLUMN payload_pad INTEGER DEFAULT 0")
        print("[Database] 迁移：device_config 表新增 payload_pad 列")

    # 迁移：5.8 毫米波（LD2410C）字段 —— latest 与 history 都要存，
    # history 用于论文"毫米波 vs 视觉"对比实验与能耗画像分析。
    # 默认 -1 表示"该节点未安装毫米波 / 未收到有效帧"，与固件的哨兵值一致。
    MM_COLS = [
        ('person', 'INTEGER DEFAULT -1'),          # 有人：1有 0无 -1未知
        ('mm_state', 'INTEGER DEFAULT -1'),        # 0无人 1运动 2静止 3运动+静止
        ('mm_move_dist', 'INTEGER DEFAULT -1'),    # 运动目标距离 cm
        ('mm_still_dist', 'INTEGER DEFAULT -1'),   # 静止目标距离 cm
        ('mm_detect_dist', 'INTEGER DEFAULT -1'),  # 探测距离 cm
        ('mm_move_energy', 'INTEGER DEFAULT -1'),  # 运动能量 0~100
        ('mm_still_energy', 'INTEGER DEFAULT -1'), # 静止能量 0~100
        # 5.8.2（2026-11-14）：升级 LD2450 后的**平面坐标**（2D，非 3D）
        ('mm_proto', 'INTEGER DEFAULT -1'),        # 协议：1=LD2410C 2=LD2450
        ('mm_x', 'INTEGER DEFAULT -99999'),        # x 坐标 mm（正=右）
        ('mm_y', 'INTEGER DEFAULT -99999'),        # y 坐标 mm（正=前方探测方向）
        ('mm_speed', 'INTEGER DEFAULT -99999'),    # 速度 cm/s（正=靠近）
    ]
    for table in ('sensor_latest', 'sensor_history'):
        tcols = [r[1] for r in cursor.execute(f'PRAGMA table_info({table})')]
        for cname, cdef in MM_COLS:
            if cname not in tcols:
                cursor.execute(f'ALTER TABLE {table} ADD COLUMN {cname} {cdef}')
                print(f"[Database] 迁移：{table} 表新增 {cname} 列（5.8 毫米波）")

    # 迁移：5.14 能耗统计 —— INA219 电流此前**只上报不落库**，导致无法做历史用电画像。
    # 单位 mA，-1 表示该节点未接 INA219（如 68FE）。采样周期 5s，用于积分算电量。
    ENERGY_COLS = [
        ('current_ma', 'REAL DEFAULT -1'),
    ]
    for table in ('sensor_latest', 'sensor_history'):
        tcols = [r[1] for r in cursor.execute(f'PRAGMA table_info({table})')]
        for cname, cdef in ENERGY_COLS:
            if cname not in tcols:
                cursor.execute(f'ALTER TABLE {table} ADD COLUMN {cname} {cdef}')
                print(f"[Database] 迁移：{table} 表新增 {cname} 列（5.14 能耗）")
    # 能耗查询按 (esp32_id, timestamp) 走索引
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_energy '
                   'ON sensor_history(esp32_id, timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_esp32 ON sensor_history(esp32_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_ts ON sensor_history(timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_loc ON sensor_history(location)')
    conn.commit()
    conn.close()
    print("[Database] 传感器数据库初始化完成")


def init_db():
    """幂等初始化用户数据库 + 链式初始化传感器数据库"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            reg_time DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 迁移：旧库（无 role 列）补充 role 字段
    cols = [r[1] for r in cursor.execute('PRAGMA table_info(users)')]
    if 'role' not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
        print("[Database] 迁移：users 表新增 role 列")
    cursor.execute('INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)',
                   ('test', '123456', 'user'))
    cursor.execute('INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)',
                   ('admin', 'admin123', 'admin'))
    # 保证内置账号角色正确（兼容旧库）
    cursor.execute("UPDATE users SET role='admin' WHERE username='admin'")
    cursor.execute("UPDATE users SET role='user' WHERE username='test'")
    conn.commit()
    conn.close()
    print("[Database] 用户数据库初始化完成")
    print("[Database] 管理员账号: admin / admin123")
    print("[Database] 用户账号: test / 123456")

    init_sensors_db()

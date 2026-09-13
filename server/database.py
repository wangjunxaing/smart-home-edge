# -*- coding: utf-8 -*-
"""
数据库模块 — 连接工厂与表初始化
==============================
用户系统 (users.db) + 传感器系统 (sensors_history.db) 完全分离。
"""
import sqlite3
import os

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'users.db')
SENSORS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sensors_history.db')


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

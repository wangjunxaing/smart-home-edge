# -*- coding: utf-8 -*-
"""
用户认证模块 — Blueprint
========================
提供注册/登录/登出/状态检查 API + login_required 装饰器。
"""
import sqlite3
from functools import wraps
from flask import Blueprint, request, session, jsonify
from core.database import get_db

auth_bp = Blueprint('auth', __name__)


def login_required(f):
    """登录验证装饰器：未登录返回 401，API 请求返回 JSON"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'unauthorized', 'message': '请先登录'}), 401
            from flask import redirect, url_for
            return redirect(url_for('register_page'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """管理员权限装饰器：需登录且 role=admin，否则 403"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'unauthorized', 'message': '请先登录'}), 401
            from flask import redirect, url_for
            return redirect(url_for('register_page'))
        if session.get('role') != 'admin':
            return jsonify({'error': 'forbidden', 'message': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated_function


@auth_bp.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json() or request.form
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    print(f"[Register] 注册请求: username={username}")

    if not username or not password:
        return jsonify({'success': False, 'message': '用户名和密码不能为空'})
    if len(password) < 6:
        return jsonify({'success': False, 'message': '密码长度至少6位'})

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO users (username, password, role) VALUES (?, ?, ?)',
                      (username, password, 'user'))
        conn.commit()
        conn.close()
        print(f"[Register] 注册成功: {username}")
        return jsonify({'success': True, 'message': '注册成功'})
    except sqlite3.IntegrityError:
        print(f"[Register] 注册失败 - 用户名已存在: {username}")
        return jsonify({'success': False, 'message': '用户名已存在'})
    except Exception as e:
        print(f"[Register] 注册失败 - 数据库错误: {e}")
        return jsonify({'success': False, 'message': f'数据库错误: {str(e)}'})


@auth_bp.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or request.form
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    print(f"[Login] 登录请求: {username}")

    if not username or not password:
        return jsonify({'success': False, 'message': '用户名和密码不能为空'})

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username=? AND password=?',
                      (username, password))
        user = cursor.fetchone()
        conn.close()

        if user:
            session['logged_in'] = True
            session['username'] = username
            session['role'] = user['role'] if 'role' in user.keys() else 'user'
            print(f"[Login] 登录成功: {username} (role={session['role']})")
            return jsonify({'success': True, 'message': '登录成功',
                            'username': username, 'role': session['role']})
        else:
            print(f"[Login] 登录失败: {username}")
            return jsonify({'success': False, 'message': '用户名或密码错误'})
    except Exception as e:
        print(f"[Login] 登录失败 - 数据库错误: {e}")
        return jsonify({'success': False, 'message': f'数据库错误: {str(e)}'})


@auth_bp.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'success': True, 'message': '已退出登录'})


@auth_bp.route('/api/check_login')
def check_login():
    if session.get('logged_in'):
        return jsonify({'logged_in': True, 'username': session.get('username')})
    return jsonify({'logged_in': False})

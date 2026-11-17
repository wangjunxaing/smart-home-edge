# -*- coding: utf-8 -*-
"""
系统可观测性采集（5.5）
======================
给"健康监控"提供 Pi 主机 / 节点 / 链路三类指标，**只读、不写业务数据**。

设计要点：
  - **psutil 优先，/proc 回退**：Pi 上若没装 psutil，CPU/内存/磁盘/网络仍可用；
    装机建议 `pip install psutil`（内存细分与进程视图更完整）。
  - **网络速率由服务端算差值**：`/proc/net/dev` 只有累计字节，速率需相邻两次采样求差；
    本模块保存上一采样点并返回 KB/s（首次调用没有差值 → 返回 None）。
  - **不抛异常**：任何一项采集失败都置 None/空，健康接口不能因为一个指标挂掉。
  - **敏感信息隔离**：`health_summary()` 只给布尔与计数（可公开）；
    明细（磁盘、网络字节、进程列表）只出现在管理员接口。
"""
import ctypes
import os
import shutil
import subprocess
import time
from datetime import datetime

try:
    import psutil
    PSUTIL = True
except ImportError:          # Pi 上未安装时走 /proc 回退
    psutil = None
    PSUTIL = False

if PSUTIL:
    # 预热 psutil 的 CPU 基线：否则首次 cpu_percent(interval=None) 恒为 0.0
    # （它是"距上次调用"的增量，没有基线就没有意义）。100ms 只花在首次导入。
    try:
        psutil.cpu_percent(interval=0.1)
    except Exception:
        pass

from core.config import SERVER_DIR, DATA_DIR
from core.database import get_sensors_db
from core.config_manager import load_device_config

# 被监控的服务（systemd 单元名）
SERVICES = ['smart-server', 'smart-usb-gateway', 'mosquitto']

_prev_net = {}       # {'ts': float, 'rx': int, 'tx': int}
_prev_disk = {}      # 供未来扩展磁盘 IO 速率


# ==================== 工具 ====================

def _read_first(path_candidates):
    """按顺序读第一个能打开的文件，返回字符串或 None"""
    for p in path_candidates:
        try:
            with open(p, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except OSError:
            continue
    return None


def _service_active(name):
    """systemd 单元是否 active；非 systemd 环境（如本机 Windows）返回 None"""
    try:
        r = subprocess.run(['systemctl', 'is-active', name],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() == 'active'
    except Exception:
        return None


# ==================== 主机指标 ====================

def _cpu_stat_proc():
    """从 /proc/stat 读累计 jiffies，返回 (idle, total)"""
    out = _read_first(['/proc/stat'])
    if not out:
        return None
    for line in out.splitlines():
        if line.startswith('cpu '):
            parts = [int(x) for x in line.split()[1:]]
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
            return idle, sum(parts)
    return None


_prev_cpu = {}
_MIN_CPU_WINDOW_S = 0.8      # 两次 CPU 采样至少要隔这么久，否则沿用上次结果
_CPU_STALE_S = 6.0           # 基准点比这还旧（例如长时间没人看仪表盘）才重新对齐基线


def _cpu_proc():
    """CPU 使用率（用 /proc/stat 两次采样求差）——非阻塞、窗口可控。

    说明：不用 psutil.cpu_percent(interval=None)，因为它测的是"距上一次调用"的增量，
    调用间隔不可控时会出现假 0；这里自己维护采样点，并保证窗口不小于 _MIN_CPU_WINDOW_S。
    """
    cur = _cpu_stat_proc()
    if not cur:
        return None
    idle, total = cur
    now = time.time()
    prev = _prev_cpu.get('v')
    prev_t = _prev_cpu.get('t', 0)
    if not prev:
        # 首次调用：无基准，只记录采样点
        _prev_cpu['v'] = cur
        _prev_cpu['t'] = now
        return None
    if (now - prev_t) < _MIN_CPU_WINDOW_S:
        # 窗口太短（例如缓存失效但连着请求）：沿用上次百分比，避免假 0
        return _prev_cpu.get('pct')
    d_idle, d_total = idle - prev[0], total - prev[1]
    # 窗口过长（例如长时间没人访问、基准点已过期）：用 psutil 重新对齐基线再测，
    # 否则读数是"过去很久的平均值"，与"当前"不符。
    if (now - prev_t) > _CPU_STALE_S and PSUTIL:
        try:
            pct = round(psutil.cpu_percent(interval=0.1), 1)
            _prev_cpu['v'] = _cpu_stat_proc() or cur
            _prev_cpu['t'] = time.time()
            _prev_cpu['pct'] = pct
            return pct
        except Exception:
            pass
    _prev_cpu['v'] = cur
    _prev_cpu['t'] = now
    if d_total <= 0:
        return _prev_cpu.get('pct')
    pct = round(100.0 * (1 - d_idle / d_total), 1)
    _prev_cpu['pct'] = pct
    return pct


def _mem_proc():
    """/proc/meminfo → 内存字典；Windows 上退回 GlobalMemoryStatusEx（便于本地开发验证）"""
    out = _read_first(['/proc/meminfo'])
    if out:
        kv = {}
        for line in out.splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                kv[k.strip()] = int(v.strip().split()[0])      # kB
        total = kv.get('MemTotal', 0) / 1024.0
        avail = kv.get('MemAvailable', kv.get('MemFree', 0)) / 1024.0
        used = total - avail
        return {'total_mb': round(total), 'used_mb': round(used), 'available_mb': round(avail),
                'percent': round(100.0 * used / total, 1) if total else None,
                'swap_total_mb': round(kv.get('SwapTotal', 0) / 1024.0),
                'swap_used_mb': round((kv.get('SwapTotal', 0) - kv.get('SwapFree', 0)) / 1024.0)}
    return _mem_windows()


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


def _mem_windows():
    """Windows 内存（仅本地开发用；Pi 上走 psutil 或 /proc）"""
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        total = st.ullTotalPhys / 1048576.0
        avail = st.ullAvailPhys / 1048576.0
        return {'total_mb': round(total), 'used_mb': round(total - avail),
                'available_mb': round(avail), 'percent': float(st.dwMemoryLoad),
                'swap_total_mb': None, 'swap_used_mb': None}
    except Exception:
        return None


def cpu_temperature():
    """SoC 温度：thermal_zone（Pi 优先）→ psutil.sensors_temperatures()"""
    t = _read_first(['/sys/class/thermal/thermal_zone0/temp'])
    if t:
        try:
            return round(int(t) / 1000.0, 1)
        except ValueError:
            pass
    if PSUTIL:
        try:
            temps = psutil.sensors_temperatures() or {}
            for entries in temps.values():
                for e in entries:
                    if e.current:
                        return round(float(e.current), 1)
        except Exception:
            pass
    return None


def host_metrics():
    """Pi 主机指标（CPU/内存/磁盘/网络/温度/负载）"""
    out = {'psutil': PSUTIL}

    # ---- CPU ----
    # 优先 /proc/stat 差分（非阻塞、采样窗口可控），避免 psutil.cpu_percent(interval=None)
    # 因调用间隔不可控而报假 0；没有 /proc 的平台（Windows 本地）再退回 psutil。
    pct = _cpu_proc()
    if pct is not None:
        out['cpu_source'] = 'proc'
    elif PSUTIL:
        try:
            pct = round(psutil.cpu_percent(interval=None), 1)
            out['cpu_source'] = 'psutil'
        except Exception:
            pct = None
            out['cpu_source'] = None
    else:
        out['cpu_source'] = None
    out['cpu_percent'] = pct
    out['cpu_count'] = (psutil.cpu_count() if PSUTIL else None) or os.cpu_count()
    try:
        out['load_avg'] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        out['load_avg'] = None
    if PSUTIL:
        try:
            out['cpu_per_core'] = psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            out['cpu_per_core'] = None

    # ---- 内存 ----
    if PSUTIL:
        try:
            vm = psutil.virtual_memory()
            sm = psutil.swap_memory()
            out['memory'] = {'total_mb': round(vm.total / 1048576), 'used_mb': round(vm.used / 1048576),
                             'available_mb': round(vm.available / 1048576), 'percent': vm.percent,
                             'swap_total_mb': round(sm.total / 1048576), 'swap_used_mb': round(sm.used / 1048576)}
        except Exception:
            out['memory'] = _mem_proc()
    else:
        out['memory'] = _mem_proc()

    # ---- 磁盘（关注数据盘 = DB 所在盘；shutil 兜底保证跨平台）----
    try:
        if PSUTIL:
            du = psutil.disk_usage(str(DATA_DIR))
            out['disk'] = {'total_gb': round(du.total / 1073741824, 1),
                           'used_gb': round(du.used / 1073741824, 1),
                           'free_gb': round(du.free / 1073741824, 1),
                           'percent': du.percent, 'mount': str(DATA_DIR)}
        else:
            du = shutil.disk_usage(str(DATA_DIR))
            out['disk'] = {'total_gb': round(du.total / 1073741824, 1),
                           'used_gb': round(du.used / 1073741824, 1),
                           'free_gb': round(du.free / 1073741824, 1),
                           'percent': round(100.0 * du.used / du.total, 1) if du.total else None,
                           'mount': str(DATA_DIR)}
    except Exception:
        out['disk'] = None

    # ---- 网络（速率需两次采样求差）----
    rx = tx = None
    if PSUTIL:
        try:
            io = psutil.net_io_counters()
            rx, tx = io.bytes_recv, io.bytes_sent
        except Exception:
            pass
    if rx is None:
        net = _read_first(['/proc/net/dev'])
        if net:
            rx = tx = 0
            for line in net.splitlines()[2:]:
                if ':' in line:
                    fields = line.split(':', 1)[1].split()
                    rx += int(fields[0])
                    tx += int(fields[8])
    now = time.time()
    if rx is not None:
        prev = _prev_net
        rate = None
        if prev.get('ts'):
            dt = now - prev['ts']
            if dt > 0:
                rate = {'rx_kbps': round((rx - prev['rx']) / dt / 1024, 1),
                        'tx_kbps': round((tx - prev['tx']) / dt / 1024, 1)}
        _prev_net.update({'ts': now, 'rx': rx, 'tx': tx})
        out['network'] = {'rx_bytes': rx, 'tx_bytes': tx, 'rate': rate}
    else:
        out['network'] = None

    # ---- 温度 / 运行时长 ----
    out['temperature_c'] = cpu_temperature()
    try:
        if PSUTIL:
            out['uptime_s'] = int(time.time() - psutil.boot_time())
        else:
            up = _read_first(['/proc/uptime'])
            out['uptime_s'] = int(float(up.split()[0])) if up else None
    except Exception:
        out['uptime_s'] = None
    return out


# ==================== 服务 / 节点 / 链路 ====================

def services_status():
    """三个 systemd 服务的 active 状态（非 systemd 环境为 None）"""
    return {name: _service_active(name) for name in SERVICES}


def devices_status(offline_after_s=60):
    """每个节点的健康：静默时长 / 位置 / RSSI / 固件版本 / 传输通道。

    offline_after_s：超过该秒数未上报 → online=False（5.5 的判定阈值；
    5.7 的远控重启自愈会复用这个判定）。
    """
    now = datetime.now()
    devices = []
    try:
        conn = get_sensors_db()
        rows = conn.execute(
            'SELECT esp32_id, location, temperature, humidity, lux, rssi, '
            'last_update, last_update_ts FROM sensor_latest').fetchall()
        conn.close()
    except Exception as e:
        return {'error': str(e), 'devices': []}

    for row in rows:
        silent = None
        online = False
        ts = row['last_update_ts']
        if ts:
            try:
                silent = round((now - datetime.fromisoformat(ts)).total_seconds(), 1)
                online = silent < offline_after_s
            except Exception:
                pass
        cfg = load_device_config(row['esp32_id']) or {}
        devices.append({
            'device_id': row['esp32_id'],
            'location': row['location'],
            'alias': cfg.get('alias', ''),
            'online': online,
            'silent_s': silent,
            'rssi': row['rssi'],
            'last_update': row['last_update'],
            'transport': cfg.get('transport', 'mqtt'),
            'send_interval_ms': cfg.get('send_interval'),
            'temperature': row['temperature'],
            'humidity': row['humidity'],
            'lux': row['lux'],
        })
    devices.sort(key=lambda d: d['device_id'])
    return {'offline_after_s': offline_after_s, 'devices': devices}


def link_status():
    """链路层：网关 STATUS/统计（ESP-NOW）与下行可靠性计数"""
    out = {}
    try:
        import link.espnow_link as espnow_link
        out['gateway'] = espnow_link.get_gateway_status() or {}
        out['espnow_stats'] = espnow_link.get_stats()
    except Exception as e:
        out['gateway'] = {'error': str(e)}
    try:
        import link.link_state as link_state
        out['downlink_stats'] = link_state.stats()
        out['downlink_pending'] = link_state.pending_count()
    except Exception as e:
        out['downlink_stats'] = {'error': str(e)}
    return out


# ==================== 汇总（分层缓存）====================
# 一次快照里开销分布很不均：host 只读 /proc（<5ms，廉价），而 services 要起 3 个
# systemctl 子进程、devices 要查库（合计约 30ms）。所以**只缓存贵的部分**：
#   - host：每次实采 → 前端看到的就是"此刻"的 CPU/内存/网络（没有 TTL 延迟）
#   - services / devices：缓存几秒，避免高频轮询反复起子进程
# 代价：CPU 采样窗口 = 前端轮询间隔（3s），比"缓存整份快照"更准。

SERVICES_TTL_S = 5.0        # systemctl 子进程结果
DEVICES_TTL_S = 2.0         # 节点静默判定（阈值本身是 60s，2s 缓存无感）
HEALTH_TTL_S = 2.0          # 公开概要（它同时用到 services + devices）
_cache = {}                 # key -> (ts, payload), ts=0 表示"只记录采样点、不算命中"


def _cached(key, ttl, producer):
    """极简 TTL 缓存；producer 抛异常时不写入缓存。

    返回 (payload, 是否本次新建)。注意：不能靠 payload 自带的标志位判断命中，
    因为 host_metrics 会就地更新 cpu 采样值，会让同一份缓存对象看起来"刚生成"。
    """
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < ttl:
        return hit[1], False
    val = producer()
    _cache[key] = (now, val)
    return val, True


def health_summary():
    """公开健康概要：**只给状态与计数，不含资源明细**"""
    def build():
        svc = services_status()
        devs = devices_status().get('devices', [])
        online = sum(1 for d in devs if d['online'])
        # 服务状态：None（非 systemd）不算失败
        failed = [k for k, v in svc.items() if v is False]
        status = 'ok'
        if failed:
            status = 'degraded'
        if any(d for d in devs if not d['online']):
            status = 'degraded' if status == 'ok' else status
        return {
            'status': status,
            'ts': datetime.now().isoformat(timespec='seconds'),
            'uptime_s': host_metrics().get('uptime_s'),
            'devices_total': len(devs),
            'devices_online': online,
            'services_total': len(svc),
            'services_failed': failed,
        }

    payload, _built = _cached('health', HEALTH_TTL_S, build)
    return dict(payload)


def system_snapshot():
    """管理员明细：主机（每次实采）+ 服务/节点（短缓存）+ 链路"""
    svc, svc_built = _cached('services', SERVICES_TTL_S, services_status)
    devs, dev_built = _cached('devices', DEVICES_TTL_S, devices_status)
    now = time.time()
    return {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'server_dir': str(SERVER_DIR),
        'host': host_metrics(),                 # 每次实采：CPU/内存/网络都是当前值
        'services': svc,
        'devices': devs,
        'link': link_status(),
        'age_ms': 0,                            # host 为实时，兼容旧前端字段
        'cached': not (svc_built or dev_built),
        'parts_age_ms': {
            'host': 0,
            'services': max(0, int((now - _cache['services'][0]) * 1000)) if not svc_built else 0,
            'devices': max(0, int((now - _cache['devices'][0]) * 1000)) if not dev_built else 0,
        },
    }

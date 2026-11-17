# 0-Smart 系统架构文档

> 对应论文第 2 章"系统总体设计"底稿。更新时间：2026-09

## 1. 系统总体架构

```
┌────────────────────────────────────────────────────────────────────┐
│  应用层（Web 前端 main.html）                                       │
│   管理员：仪表盘 / 设备控制 / 自动化规则 / 固件管理                  │
│   用户：  环境传感器 / 实时监控 / AI 对话（纯信息）                  │
├────────────────────────────────────────────────────────────────────┤
│  服务层（Orange Pi 5 Pro · RK3588 · Flask :8080）
│  ┌────────────┬─────────────┬──────────────┬───────────────────┐
│  │ sensors    │ automation  │ llm_manager  │ speech(vosk)      │
│  │ (数据/历史)│ (规则引擎)  │ + tool_use   │ 语音识别          │
│  ├────────────┴─────────────┴──────────────┴───────────────────┤
│  │ auth(role) · light · ir · firmware(OTA) · camera            │
│  │ SQLite: data/users.db · sensors_history.db · automation.db  │
│  └───────────────────────────────┬─────────────────────────────┘
│                                  │ UDS (/tmp/llm_proxy.sock)
│                        ┌─────────▼─────────┐
│                        │ native/llm_proxy  │  PTY 通道（会话管理）
│                        │ (forkpty)         │
│                        └─────────┬─────────┘
│                        ┌─────────▼─────────┐
│                        │ llm_demo(RKLLM)   │  Qwen2.5-1.5B @ NPU
│                        │ 工具模式 system   │  [TOOL] 协议
│                        │ prompt 内置       │
│                        └───────────────────┘
├────────────────────────────────────────────────────────────────────┤
│  感知层（ESP32 节点 · FreeRTOS 四任务双核）                         │
│  sensor_task → sensorQueue → net_task(HTTP上报/OTA/配置)           │
│  net_task → controlQueue → control_task(灯带WS2812/红外RMT)        │
│  audio_task(INMP441 I2S)                                           │
│  BH1750 光照 · AHT20 温湿度 · HLK-LD2410B 毫米波(阶段二)            │
└────────────────────────────────────────────────────────────────────┘
```

## 2. 数据流

### 2.1 传感器上报链路
```
ESP32(每5s) --POST /api/esp32/sensors--> sensors.py
  → sensor_latest(快照表, upsert) + sensor_history(全量表)
  → automation 引擎每15s 读 latest 判规则 → 命中执行动作 → 写 automation_logs
```

### 2.2 LLM 工具调用链路（双层意图解析）
```
用户输入 → chat_with_tools()
  ├─ 规则引擎 parse_intent（开灯/关灯/问亮度/问时间…）→ 直接执行 → 模板回复
  ├─ LLM 决策轮（system prompt 内置 [TOOL] 协议）→ 解析执行 → 模板回复
  └─ 纯闲聊 → LLM 直接回答（SSE 流式）
```

### 2.3 控制链路（双模：MQTT 主 / ESP-NOW 副）
```
Web/AI → light.py | ir.py | tts.py → downlink.send_downlink()
        ├─ link_state 选路：MQTT（默认）或 ESP-NOW（节点降级后自动切换）
        ├─ MQTT：publish /esp32/{id}/cmd → 节点订阅执行
        └─ ESP-NOW：espnow_link → USB 网关 → 空口 CMD → 节点执行
节点回执（/esp32/{id}/ack 或空口 ACK）→ 清除待回执；1.5s 未回执自动重发（最多 2 次，重发前重新选路）
```
- 控制面已**不再使用 HTTP 轮询**；HTTP 仅用于配置拉取与 OTA（MQTT 模式周期执行，ESP-NOW 走维护窗口）

## 3. 数据库（SQLite）

| 库 | 表 | 说明 |
|---|---|---|
| users.db | users | id/username/password/**role**(admin/user)/reg_time |
| sensors_history.db | sensor_history | 全量历史（esp32_id/location/temperature/humidity/lux/timestamp） |
| | sensor_latest | 最新快照（esp32_id 主键 + rssi + last_update_ts） |
| | device_config | 设备动态配置（send_interval/location/alias） |
| automation.db | automation_rules | 规则（location/metric/operator/threshold/action/cooldown） |
| | automation_logs | 规则触发记录 |

## 4. 权限模型（4.3 引入）

```
users.role = 'admin' | 'user'
├─ login_required   → 所有登录用户（信息类 API：sensors/llm/camera）
└─ admin_required   → 仅管理员（控制/配置类：light/ir/firmware/automation/设备配置）
前端：Jinja 服务端按 role 渲染（非管理员拿不到控制区 HTML）+ API 403 双重拦截
```

## 5. API 清单

### 认证
| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | /api/login | 公开 | 登录（返回 role） |
| POST | /api/register | 公开 | 注册（默认 user） |
| POST | /api/logout | 登录 | 登出 |

### 传感器信息
| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | /api/esp32/sensors | 设备 | ESP32 数据上报 |
| GET | /api/esp32/sensors/latest?location= | 公开 | 最新数据+统计+告警 |
| GET | /api/esp32/sensors/history?id=&hours=&location= | 公开 | 历史（C 加速/Python 回退） |
| GET | /api/esp32/sensors/export | 公开 | CSV 导出 |
| POST | /api/esp32/config/update | admin | 采样间隔/位置 |
| POST | /api/esp32/config/alias | admin | 设备别名 |

### AI
| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | /api/llm/chat | 登录 | SSE 流式对话（含工具调用） |
| GET | /api/llm/status | 登录 | 模型状态 |
| POST | /api/llm/start /restart | 登录 | 代理控制 |

### 控制（全部 admin）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/admin/light/set | 灯带（on/rgb/brightness） |
| GET | /api/admin/light/state | 灯带状态 |
| POST | /api/admin/ir/learn · /send · /codes/* | 红外学习/发射/码库 |
| POST | /api/admin/firmware/upload | OTA 固件上传 |
| GET | /api/admin/automation + CRUD + /logs | 自动化规则 |

## 6. 部署与运维（4.4）

### systemd（开机自启 + 崩溃自愈）
```bash
# /etc/systemd/system/smart-server.service
#   Restart=always, RestartSec=5
sudo systemctl enable --now smart-server
journalctl -u smart-server -f        # 日志
systemctl restart smart-server        # 重启
```

### 定时关机（默认禁用，按需启用）
```bash
# /etc/systemd/system/smart-shutdown.{service,timer}
# 启用：取消 service 中 ExecStart 注释 + systemctl enable --now smart-shutdown.timer
```

### 日志轮转
```bash
# /etc/logrotate.d/smart-server（daily × 7 份压缩，su ubuntu）
sudo logrotate -f /etc/logrotate.d/smart-server   # 手动触发
```

## 7. ESP32 固件（FreeRTOS）

```
setup: WDT(15s) → 外设(LED/I2C/灯带/红外/麦克风) → 队列/互斥 → 网络 → 4任务
├─ sensor_task  (core1, prio2): I2C 采集(部分降级) → sensorQueue
├─ net_task     (core0, prio2): 双模上报 / 配置拉取 / OTA / 维护窗口
├─ control_task (core1, prio3): 控制命令执行 + LED
└─ audio_task   (core1, prio2): INMP441 I2S → 服务器语音识别
```

- **双模传输**：`transport = mqtt | espnow`；MQTT 经路由器（30 s 控制面），ESP-NOW 不依赖任何网络基础设施（每 10 分钟维护窗口拉配置 / 查 OTA）
- **故障转移**：MQTT 连续 60 s 不可用 → 自动降级 ESP-NOW；降级期间每 2 分钟探测网络，连续 3 次成功才切回 MQTT
- **上行**：ESP-NOW 帧带递增序号（网关据此统计丢包缺口）；单帧载荷上限实测边界约 233 B（250 B 帧长）
- **OTA**：签名升级（RSA-2048 + SHA-256 验签）→ 双 OTA 分区切换 → 应用级健康回滚（连续异常启动自动回退旧固件）
- 断网缓冲：传感器队列暂存，网络恢复后补报

## 8. 实测数据与结论

各阶段实测的关键数字与结论集中收录于 `docs/TEST_RESULTS.md`：传输方式（HTTP/MQTT）对比、
ESP-NOW 测试矩阵 T1–T8（协议 / 周期 / 载荷 / 干扰 / 长稳 / 多节点 / 距离）、双模故障转移与下行重发、
测试环境与复现命令。

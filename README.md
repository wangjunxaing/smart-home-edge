# 基于边缘计算的全本地智能家居系统

> 嵌入式方向本科毕业设计 · 独立完成

基于 **ESP32 边缘节点 + Orange Pi 5 Pro (RK3588)** 的端侧智能家居系统。
语音识别、设备控制与 AI 推理**全部在本地边缘设备完成**，家庭数据不出户；
WiFi 链路质量下降时，经 ESP-NOW 双通道自动兜底，保障通信不中断。

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│ 应用层   Web 控制面板（双角色） · 语音交互 · AI 工具调用      │
├─────────────────────────────────────────────────────────┤
│ 边缘层   Orange Pi 5 Pro (RK3588, 8GB)                   │
│          Flask + SQLite · MQTT Broker                    │
│          端侧 AI: xLAM-2-3B (NPU/RKLLM) · FunASR 语音识别   │
├─────────────────────────────────────────────────────────┤
│ 通信层   MQTT（主） + ESP-NOW（兜底）· 双通道自适应          │
├─────────────────────────────────────────────────────────┤
│ 感知层   ESP32 节点: 光照/温湿度/电流/毫米波/麦克风          │
│          ESP32-S3 网关: ESP-NOW ↔ USB 桥接                │
└─────────────────────────────────────────────────────────┘
```

## 功能特性

- **全本地 AI**：端侧 3B 工具调用模型（RK3588 NPU）+ FunASR 中文语音识别，无云端依赖
- **双通道通信**：MQTT 主通道 + ESP-NOW 兜底，链路劣化自动降级、恢复自动回切
- **语音交互**：唤醒词 + 自然语言指令，规则优先 + LLM 兜底的混合意图解析
- **自动化规则**：传感器阈值联动，支持灯带/插座/红外空调
- **毫米波人体感知**：LD2450 2D 坐标定位
- **可运维**：离线自愈（三级保护）、操作审计（可追溯/可撤销）、能耗画像
- **OTA 升级**：固件签名 + 分设备灰度 + 审计日志

## 技术栈

| 层 | 技术 |
|---|---|
| 固件 | ESP32 / FreeRTOS / C / I2S / UART / I2C / SPI |
| 通信 | MQTT / ESP-NOW / WiFi / 双通道冗余 |
| 边缘 AI | RKLLM (RK3588 NPU) / 模型量化 W8A8 / ONNX Runtime / FunASR Paraformer |
| 服务端 | Python / Flask / SQLite / SSE / systemd |
| 前端 | 原生 JS / HTML / CSS |
| 运维 | Tailscale / 自愈 / 审计 |

## 目录结构

```
├── esp32-environment/     # ESP32 节点固件（PlatformIO, FreeRTOS 四任务）
│   └── src/               #   wifi_manager / sensors_mod / net_client / control / ota_update
├── esp32-gateway/         # ESP-NOW ↔ USB 网关固件（ESP32-S3）
├── esp32-node-espnow/     # 纯 ESP-NOW 最小节点（实验基线 / 低功耗对照）
├── esp32-deepsleep-test/  # 深睡功耗测量固件
├── shared/                # espnow_proto.h（三端共用 ESP-NOW 帧协议）
├── server/                # 边缘服务器（Flask）
│   ├── app.py             #   入口（:8080）
│   ├── core/              #   config / database / auth / llm_manager / tool_use / audit / asr_funasr
│   ├── api/               #   sensors / light / ir / tts / automation / firmware / speech / ...
│   ├── link/              #   mqtt_bridge / espnow_link / usb_gateway / downlink / link_state
│   ├── native/            #   llm_proxy.cpp / rkllm-demo（C++ 推理代理）
│   ├── deploy/            #   systemd 单元 / net_watchdog / 部署说明
│   ├── scripts/           #   测量与运维脚本
│   ├── templates/ static/ #   前端
│   └── models/ data/      #   模型与运行时数据（.gitignore，需自行准备）
└── docs/                  # 架构文档与实测报告
```

## 快速开始

### 1. 服务器（Orange Pi / Ubuntu）

```bash
# 依赖
sudo apt install python3 python3-venv tmux
python3 -m venv ~/ai_env
~/ai_env/bin/pip install flask vosk funasr-onnx onnxruntime

# 编译 C++ 推理代理（需 g++ / gcc / sqlite3 开发库）
cd server/native && make
cd rkllm-demo && make    # 生成 llm_demo（RKLLM 推理）

# 启动（开发模式）
cd server && python3 app.py
# 访问 http://<服务器IP>:8080
```

### 2. 模型准备（不入仓库，需自行下载）

| 用途 | 模型 | 大小 | 获取方式 |
|---|---|---|---|
| 工具调用 | **xLAM-2-3B-fc-r**（转 `.rkllm`） | 3.5 GB | [ModelScope: Salesforce/xLAM-2-3b-fc-r](https://modelscope.cn/models/Salesforce/xLAM-2-3b-fc-r) → 用 rkllm-toolkit 转换 |
| 语音识别 | **FunASR Paraformer ONNX** | 227 MB | [ModelScope: iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx](https://modelscope.cn/models/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx) → 放 `server/models/funasr-onnx/` |
| 语音识别（回退） | Vosk small cn | 66 MB | [Vosk Models](https://alphacephei.com/vosk/models) → 放 `server/models/vosk-model/` |

> **转换 xLAM 为 `.rkllm`**：参考 [rknn-llm](https://github.com/airockchip/rknn-llm)。
> 注意 WSL 内存需 ≥12GB（3B fp32 加载约 12GB），并固定 `numpy==1.26.4`。

### 3. ESP32 固件

```bash
cd esp32-environment
pio run                # 编译
pio run -t upload      # 烧录（需指定串口）
pio device monitor     # 串口日志
```

修改 `src/app_config.h` 中的 WiFi/MQTT/服务器地址后编译。

### 4. 系统服务（可选）

```bash
sudo cp server/deploy/smart-server.service /etc/systemd/system/
sudo systemctl enable --now smart-server
journalctl -u smart-server -f    # 查看日志
```

## 默认账号

| 角色 | 账号 | 密码 | 权限 |
|---|---|---|---|
| 管理员 | admin | admin123 | 全部功能 |
| 用户 | test | 123456 | 只读 |

> **部署到公网前请务必修改默认密码。**

## 文档

- [PLAN.md](PLAN.md) — 完整项目计划与实测记录
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 架构设计
- [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md) — 测试数据汇总

## License

个人毕业设计项目，仅供学习参考。

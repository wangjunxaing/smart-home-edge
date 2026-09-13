# 0-Smart：家庭服务器与自研 ESP32 节点

基于 Orange Pi 5 Pro（RK3588）与自研 ESP32 节点的本地智能家居系统：服务器负责设备接入、
控制、语音交互与数据存储；节点负责环境感知与执行器控制。
支持 **MQTT / ESP-NOW 双模传输**、**RSA 签名 OTA 与健康回滚**、**断网自动降级与恢复**。

## 技术栈

- **服务器**：Python / Flask / SQLite / Mosquitto；本地语音识别（Vosk）、本地小模型（RKLLM）
- **节点**：ESP32（PlatformIO / Arduino / FreeRTOS）；ESP32-S3 作为 ESP-NOW ↔ USB 网关
- **前端**：原生 HTML / CSS / JavaScript

## 目录

| 路径 | 说明 |
|---|---|
| `server/` | 家庭服务器：Flask 应用、MQTT/ESP-NOW 链路、统一下行路由、签名 OTA、网页前端、实测脚本 |
| `esp32-environment/` | 自研节点固件（双模传输 + OTA + 传感器/执行器） |
| `esp32-gateway/` | ESP-NOW ↔ USB 网关固件（ESP32-S3） |
| `esp32-node-espnow/` | 纯 ESP-NOW 轻量节点（不依赖 WiFi） |
| `shared/espnow_proto.h` | 空口帧 / USB 帧协议（三端共用，header-only） |

## 构建与运行

```bash
# 服务器
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 server/app.py            # 默认 http://0.0.0.0:8080

# ESP32 节点（先填 WiFi 配置）
cp esp32-environment/include/wifi_config.example.h esp32-environment/include/wifi_config.h
pio run -d esp32-environment -t upload

# ESP-NOW 网关（ESP32-S3）
pio run -d esp32-gateway -t upload

# 链路协议自检
python3 server/deploy_scripts/espnow_proto.py
```

> 本仓库只包含源代码，不含数据库、固件二进制、模型文件与密钥。

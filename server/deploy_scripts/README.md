# 测试脚本（deploy_scripts）

这些脚本在**家庭服务器（Orange Pi 5 Pro）上运行**，用于 ESP-NOW / MQTT 链路的协议自检、
控制时延与上行质量实测，以及多节点公平性验证。

## 前置条件

- 服务器已运行 `smart-server`（Flask）、`mosquitto`（MQTT broker）、USB 网关守护（ESP-NOW）
- ESP-NOW 网关接在 `/dev/ttyACM0`，控制口监听 `127.0.0.1:8765`
- 脚本中的**设备 ID 与 IP 是实测环境示例**（如 `A1B2C3D4E5F6`），请按自己的部署替换（多数脚本提供
  `--device` / `--dev` / `--devs` / `--device-ip` 等参数）
- 需要登录的脚本默认用内置演示账号 `admin`；请用环境变量 `SMART_WEB_USER` / `SMART_WEB_PASS`
  传入实际账号口令，不要写进脚本
- 结果默认写入 `/home/ubuntu/ab_results/`（可在脚本内调整）

## 脚本一览

| 脚本 | 用途 | 典型用法 |
|---|---|---|
| `espnow_proto.py` | 协议自检：空口帧 / USB 帧往返、CRC 向量、抗篡改（共 7 项） | `python3 espnow_proto.py` |
| `espnow_test.py` | 链路测试驱动：下行 RTT 与丢包、上行稳定性、长稳 soak | `python3 espnow_test.py downlink --dev <12位ID> --count 50 --interval 1`<br>`python3 espnow_test.py soak --hours 2 --dev <12位ID> --send-every 30` |
| `measure_mqtt_espnow.py` | MQTT vs ESP-NOW 对比：自动切换传输模式，采集控制时延、静默功耗、流量、上行到达率 | `python3 measure_mqtt_espnow.py --device <12位ID> --minutes 15 --phases espnow,mqtt --pokes 20` |
| `measure_espnow_periods.py` | 上报周期扫描（1 s / 5 s / 30 s） | `python3 measure_espnow_periods.py --dev <12位ID> --minutes 10` |
| `measure_espnow_payloads.py` | 上行载荷扫描（固件 `payload_pad` 0/20/40/80，含单帧上限边界） | `python3 measure_espnow_payloads.py --dev <12位ID> --minutes 8` |
| `measure_espnow_multinode.py` | 多节点公平性（到达率极差、丢包、静默） | `python3 measure_espnow_multinode.py --devs <ID1>,<ID2> --minutes 30` |

## 说明

- 脚本通过**网关控制口 8765**（`{"op":"send" | "subscribe" | "stats"}`）或服务器 **HTTP 接口**与设备交互，
  与生产代码走同一套通道，没有为测试额外开后门。
- `espnow_proto.py` 必须与固件侧 `shared/espnow_proto.h` 保持一致；修改协议后先跑该自检再上机。
- 主要测量口径在各脚本注释中给出，例如：
  - 到达率 = 实际收到帧数 ÷ 理论帧数（理论值 = 窗口时长 ÷ 上报周期）
  - ESP-NOW 空口帧长 = 网关 USB 字节增量 ÷ 帧数 − 6（USB 封装开销）
  - 控制时延 = 服务器下发时刻 → 收到节点 ACK 时刻（同一服务器时钟）

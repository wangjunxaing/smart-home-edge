# 部署目录说明（server/deploy/）

本目录放**部署与加固配置**，不含业务代码。目录重排（2026-09 结构整理）后，
原来 `server/deploy_scripts/` 下的运维类文件被拆开：

| 原位置 | 现位置 | 说明 |
|---|---|---|
| `deploy_scripts/net_watchdog/` | `deploy/net_watchdog/` | 断网自愈定时器（systemd + NetworkManager 加固） |
| `deploy_scripts/orangepi_deploy_key(.pub)` | `deploy/orangepi_deploy_key(.pub)` | 香橙派 SSH 部署密钥（无口令 ed25519，`.pub` 已进 Pi 的 authorized_keys） |
| `deploy_scripts/{measure_*,espnow_*,failover_test.sh,run_t*.sh,restart.sh}` | `scripts/measure/`、`scripts/ops/` | 测量与运维脚本，已独立成 `server/scripts/` |
| `deploy_scripts/cleanup.sql` | `tools/local/cleanup.sql` | 本机一次性 SQL |
| `deploy_scripts/deploy_key(.pub)` | **已删除** | 早期 RSA 死键：Pi 的 `authorized_keys` 里只有 `orangepi_deploy_key` 的 ed25519 公钥，这对密钥早已无法登录 |

## systemd 单元（已在 Pi 上核对原值）

`systemd/` 下是本仓库托管的单元文件，路径已按重排后的目录更新：

| 单元 | 重排前的关键路径 | 重排后的关键路径 |
|---|---|---|
| `smart-server.service` | `ExecStart=…/server/app.py`；`LD_LIBRARY_PATH=…/server/demo_Linux_aarch64/lib` | `ExecStart=…/server/app.py`（不变）；`LD_LIBRARY_PATH=…/server/native/rkllm-demo/lib` |
| `smart-usb-gateway.service` | `ExecStart=…/server/usb_gateway.py` | `ExecStart=…/server/link/usb_gateway.py` |

两个单元的 `WorkingDirectory` 都保持 `/home/ubuntu/server`，`PYTHONUNBUFFERED=1`
已并入单元本体（原先 `smart-server` 靠 `override.conf` 提供）。

## 迁移记录：已于 2026-09-23 22:10 在香橙派上执行完成 ✅

实际执行结果（`server/deploy/` 下的单元文件与线上一致）：

| 检查项 | 结果 |
|---|---|
| 目录 | `/home/ubuntu/server/` 下只剩 `app.py` + `core/ api/ link/ native/ deploy/ scripts/ tools/ data/ models/ static/ templates/ firmware/ keys/ archive/` |
| 服务 | `smart-server` / `smart-usb-gateway` / `mosquitto` 全部 active，`NRestarts=0`，无 traceback |
| Vosk | 从 `models/vosk-model/...` 正常加载 |
| 本地 LLM | `native/llm_proxy` 拉起 `native/rkllm-demo/llm_demo`，UDS `{"type":"status","ready":true}` |
| 上行 | `[MQTT-UP]` 每分钟约 24 条（两节点 ×5s），DB 5 分钟落库 1.1 万行 |
| OTA 签名链 | `/api/esp32/firmware/check` 返回 1.9.10；`/firmware/file` 正常返回 `x-SHA256` + `x-Signature` |
| ESP-NOW 网关 | 控制口 8765 `{"op":"stats"}` 正常应答，`crc_bad=0` |

停机窗口约 5 秒（22:10:36 停 → 22:10:41 起）；mosquitto 全程未停，所以两个节点没有察觉。

**踩到的坑（已修）**：`native/rkllm-demo/lib/librkllmrt.so` 被 `.gitignore` 排除，从 git 部署时该目录是空的，
`make` 报 `cannot find -lrkllmrt`。已在 `rkllm-demo/Makefile` 加 `$(LIB_RKLLM)` 前置依赖，缺库时给出明确提示；
库本体需从 RKLLM 官方 SDK 取回放入 `lib/`（6.7 MB，不进 git）。

**回滚手段**（三份，都在 Pi 上，均为 `cp -a` 原样快照）：
- `/home/ubuntu/server_backup_20260923_221019/`：迁移前的整目录快照（2.2 GB，含旧平铺代码 + 数据库 + 模型）；
- `/home/ubuntu/server/.legacy_flat/`：24 个旧平铺 `.py` 与 `llm_demo.cpp` / `Makefile` 等；
- `/home/ubuntu/server/.legacy_flat_demo_Linux_aarch64/`：旧的 `demo_Linux_aarch64/`（含原 `llm_demo` 二进制）。

回滚＝`sudo systemctl stop smart-server smart-usb-gateway` → 用快照覆盖 `/home/ubuntu/server`
→ `git checkout` 旧的 systemd 单元 → `daemon-reload` → 重启。

## 增量部署记录（2026-10-10：5.5 系统可观测性）

新增/修改文件：`core/metrics.py`（新增）、`api/system.py`（新增）、`app.py`（注册 Blueprint）、
`templates/main.html`（管理员"服务器状态"卡片）、`static/js/main.js`（卡片渲染 + 10s 轮询）、
`tools/local/selfcheck_imports.py`（改为跨平台自检）。

```bash
# 上传上述文件 → Pi 上依赖已就绪（psutil 7.2.2 已在 /home/ubuntu/ai_env）→ 重启
sudo systemctl restart smart-server
curl -s http://127.0.0.1:8080/api/health
```

**上线后实测**：CPU 9.4% / 内存 29.0% / 磁盘 22.2% / SoC 46.2°C；网络 44/49 KB/s；
负载 0.25/0.11/0.02；两节点在线（客厅 -60 dBm、卧室 -70 dBm）；三服务 active；无 traceback；
上行 22 条/分钟；未登录访问 `/api/admin/system` 返回 **401**（权限生效）。

> 注意：`/api/health` 是**公开**接口，只返回状态与计数（不含磁盘/网络/进程明细）；
> 明细只在需 admin 的 `/api/admin/system`。

## 部署步骤（复现用留档）

以下即 2026-09-23 实际执行的流程（脚本在 `.tmp/deploy_to_pi.py`，未入库）：

```bash
# 1) 建目录骨架
ssh -i deploy/orangepi_deploy_key ubuntu@<TAILSCALE_IP>
cd /home/ubuntu/server
mkdir -p core api link native/rkllm-demo native/rkllm-demo/lib deploy/systemd \
         deploy/net_watchdog scripts/measure scripts/ops tools/local data models

# 2) 上传新布局（本机 paramiko sftp，逐文件 put；此时服务仍在跑，零停机）
#    app.py / core/* / api/* / link/* / native/* / deploy/* / scripts/* / tools/*

# 3) 先在服务运行状态下编译（避免拖长停机窗口）
cd /home/ubuntu/server/native && make
cd rkllm-demo && make            # 需要 lib/librkllmrt.so 在位，见上面"踩到的坑"

# 4) 停服务 → 搬数据/模型（同盘 mv，秒级；mosquitto 不用停）
sudo systemctl stop smart-server smart-usb-gateway
cd /home/ubuntu/server
mv users.db sensors_history.db automation.db light_state.json ir_state.json \
   llm_chat.log sensor_errors.log data/
mv Qwen2.5-1.5B-Instruct_W8A8_RK3588.rkllm vosk-model models/
cp -f demo_Linux_aarch64/lib/*.so native/rkllm-demo/lib/    # 补齐 .so
mkdir -p .legacy_flat .legacy_flat_demo_Linux_aarch64
mv <旧平铺 .py/.cpp/Makefile> .legacy_flat/
mv demo_Linux_aarch64 .legacy_flat_demo_Linux_aarch64/
rm -rf deploy_scripts __pycache__ */__pycache__

# 5) 换 systemd 单元并重启
sudo cp deploy/systemd/smart-server.service deploy/systemd/smart-usb-gateway.service /etc/systemd/system/
sudo rm -f /etc/systemd/system/smart-server.service.d/override.conf   # PYTHONUNBUFFERED 已并入单元
sudo systemctl daemon-reload
sudo systemctl restart smart-server smart-usb-gateway
journalctl -u smart-server -n 50 --no-pager
```

**根目录残留**：迁移后还能看到旧二进制（`llm_proxy` / `sensor_history`）、`*.py.pre*` / `*.pre*` / `*.bak_prev`、
散落的 `*.log.gz` 等共 38 个文件，已统一收进 `server/archive/`（可随时删除，不影响运行）。

## net_watchdog 部署（未变）

见 `net_watchdog/README.md`，安装命令与重排无关（装到 `/usr/local/bin` 与 `/etc/systemd/system`）。

#!/bin/bash
# 断网自动降级 / 恢复验证（5.4 failover）
# 流程：节点切 MQTT → 停 broker 模拟网络不通 → 观察节点自动降级 ESP-NOW
#       → 验证下行自动改走 ESP-NOW → 恢复 broker → 观察探测后自动切回 MQTT
# 停止/启动服务需要 sudo：默认用免密 sudo（sudo -n）；
#   若未配置 NOPASSWD，可临时提供口令： SUDO_PW=你的口令 bash failover_test.sh
# 用法: setsid bash scripts/ops/failover_test.sh > /dev/null 2>&1 < /dev/null &
# 日志: /home/ubuntu/ab_results/failover_test.log
cd /home/ubuntu/server || exit 1
LOG=/home/ubuntu/ab_results/failover_test.log
DEV=${1:-04B247F9EA08}

run_sudo() {
  if [ -n "${SUDO_PW:-}" ]; then
    echo "$SUDO_PW" | sudo -S "$@"
  else
    sudo -n "$@"
  fi
}

if ! run_sudo true 2>/dev/null; then
  echo "需要 sudo 权限：配置 NOPASSWD，或设置 SUDO_PW 环境变量后重试" >&2
  exit 1
fi

{
  echo "=== 1) 把 $DEV 切到 MQTT（写库 + 空口下发立即生效） $(date '+%T') ==="
  python3 /tmp/cfg.py set "$DEV" mqtt
  python3 /tmp/cfg_push.py "$DEV" transport mqtt
  sleep 40

  echo "=== 2) 停 mosquitto（模拟 broker/网络不通） $(date '+%T') ==="
  run_sudo systemctl stop mosquitto
  echo "节点需连续 60s 判定不可用，等待 150s..."
  sleep 150

  echo "=== 3) 断网期间验证下行是否自动改走 ESP-NOW $(date '+%T') ==="
  python3 /tmp/light.py "$DEV" 1
  sleep 8

  echo "=== 4) 恢复 mosquitto $(date '+%T') ==="
  run_sudo systemctl start mosquitto
  echo "节点每 120s 探测一次、连续 3 次成功才切回，等待 480s..."
  sleep 480

  echo "=== 5) 验证下行是否回到 MQTT $(date '+%T') ==="
  python3 /tmp/light.py "$DEV" 0
  sleep 8
  echo "=== 验证结束 $(date '+%T') ==="
} > "$LOG" 2>&1

#!/bin/bash
# T7（ESP-NOW 2 小时长稳，含 T6 WiFi 并发干扰）+ T8（双节点公平性）串联执行
# 用法：setsid bash scripts/ops/run_t7_t8.sh > /dev/null 2>&1 < /dev/null &
# 日志：/home/ubuntu/ab_results/t7_t8_run.log
cd /home/ubuntu/server || exit 1
LOG=/home/ubuntu/ab_results/t7_t8_run.log
{
  echo "=== T7/T6 开始 $(date '+%F %T') ==="
  # T6 干扰前 WiFi 字节基线
  cat /sys/class/net/wlan0/statistics/rx_bytes > /tmp/wlan_rx0
  cat /sys/class/net/wlan0/statistics/tx_bytes > /tmp/wlan_tx0
  # T6 干扰源：同频段 WiFi 持续大流量（限速 ~3Mbps 循环下载阿里云镜像）
  setsid nohup bash -c 'while true; do curl -s -o /dev/null --limit-rate 375k https://mirrors.aliyun.com/ubuntu/ls-lR.gz || sleep 2; done' > /dev/null 2>&1 &
  echo "T6 干扰流已启动 pid=$!"

  # T7：ESP-NOW 长稳 2 小时，每 30s 一条下行命令
  python3 -u scripts/measure/espnow_test.py soak --hours 2 --dev 04B247F9EA08 --send-every 30

  pkill -f "limit-rate 375k" 2>/dev/null
  sleep 2
  echo "T6 干扰流已停止"
  RX0=$(cat /tmp/wlan_rx0); TX0=$(cat /tmp/wlan_tx0)
  RX1=$(cat /sys/class/net/wlan0/statistics/rx_bytes); TX1=$(cat /sys/class/net/wlan0/statistics/tx_bytes)
  echo "T6 干扰期间 WiFi 累计: rx=$((RX1-RX0))B tx=$((TX1-TX0))B（含 SSH/Tailscale 等全部流量）"

  # T8：第二个节点切 ESP-NOW，双节点并发公平性
  echo "=== T8 双节点开始 $(date '+%F %T') ==="
  python3 /tmp/cfg.py set 68FE718B2A54 espnow
  echo "已请求 68FE 切 ESP-NOW（约 30s 生效），等待 90s 稳定..."
  sleep 90
  python3 -u scripts/measure/measure_espnow_multinode.py --devs 04B247F9EA08,68FE718B2A54 --minutes 30

  # 收尾：68FE 切回 MQTT（写库即可，它会在下个维护窗口生效）
  python3 /tmp/cfg.py set 68FE718B2A54 mqtt
  echo "=== 全部完成 $(date '+%F %T') ==="
} > "$LOG" 2>&1

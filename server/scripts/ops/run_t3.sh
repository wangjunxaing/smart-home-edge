#!/bin/bash
# T3（距离/穿墙）单档采集：先等 04B 升级到目标版本，再跑 N 分钟 ESP-NOW 链路质量采集
# 用法: bash run_t3.sh <档位名> <分钟> [等待的版本]
cd /home/ubuntu/server || exit 1
NAME=${1:-D3}
MIN=${2:-10}
WANT=${3:-1.9.9}
LOG=/home/ubuntu/ab_results/t3_run.log
{
  echo "=== T3 $NAME 准备 $(date '+%F %T') ==="
  for i in $(seq 1 120); do
    if grep -q "device 04B247F9: fw 1.9.8 -> $WANT" server/firmware/audit.log; then
      echo "04B 已升级到 $WANT（$(date '+%T')）"
      break
    fi
    sleep 10
  done
  sleep 20                                   # 等重启后 ESP-NOW 栈稳定
  echo "=== $NAME 开始采集 $(date '+%F %T')（${MIN} 分钟，档位信息由操作者提供） ==="
  HOURS=$(python3 -c "print($MIN/60.0)")
  python3 -u scripts/measure/espnow_test.py soak --hours "$HOURS" --dev 04B247F9EA08 --send-every 30
  echo "=== $NAME 采集结束 $(date '+%F %T') ==="
} > "$LOG" 2>&1

#!/bin/bash
# 断网自愈（0-Smart 服务器加固，2026-09-12）
# 部署：install -m755 net-watchdog.sh /usr/local/bin/ ; install -m644 net-watchdog.{service,timer} /etc/systemd/system/
#       systemctl daemon-reload && systemctl enable --now net-watchdog.timer
# 背景：2026-09-12 21:44 路由器 BSS 变更后 wlan0 被 AP 拒绝关联（ASSOC-REJECT status_code=16），
#       NetworkManager 判定 supplicant-timeout 后放弃，Tailscale 离线 15 分钟无人自愈。
# 逻辑：每 2 分钟探测默认网关 / 公网 ICMP / DNS，全不可达计一次失败；连续 3 次先重连 wlan0，
#       仍不行再重启 NetworkManager（不会无脑重启，避免正常网络下误动作）。
# 日志：journalctl -t net-watchdog
set -u

STATE=/run/net-watchdog.fail
THRESHOLD=3
GW=$(ip route 2>/dev/null | awk '/^default/{print $3; exit}')
TARGETS="223.5.5.5 114.114.114.114 1.1.1.1"

reachable() {
  if [ -n "${GW:-}" ] && ping -c1 -W2 "$GW" >/dev/null 2>&1; then
    return 0
  fi
  for t in $TARGETS; do
    if ping -c1 -W2 "$t" >/dev/null 2>&1; then
      return 0
    fi
  done
  # ICMP 可能被禁：再用 DNS 解析兜底
  if getent hosts tailscale.com >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

if reachable; then
  [ -f "$STATE" ] && rm -f "$STATE"
  exit 0
fi

n=$(cat "$STATE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$STATE"
logger -t net-watchdog "connectivity check failed ($n/$THRESHOLD, gw=${GW:-none})"

if [ "$n" -ge "$THRESHOLD" ]; then
  logger -t net-watchdog "recovering: reconnect wlan0 after $n consecutive failures"
  rm -f "$STATE"
  if ! nmcli device connect wlan0 >/dev/null 2>&1; then
    logger -t net-watchdog "wlan0 reconnect failed, restarting NetworkManager"
    systemctl restart NetworkManager
  fi
  sleep 5
  if reachable; then
    logger -t net-watchdog "recovery OK"
  else
    logger -t net-watchdog "recovery attempt finished, still unreachable"
  fi
fi
exit 0

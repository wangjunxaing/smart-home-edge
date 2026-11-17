# 断网自愈加固（0-Smart 服务器，2026-09-12）

## 背景

2026-09-12 21:44，路由器（`YOUR_WIFI_SSID`，BSSID `<BSSID>`）发生 BSS 变更，
服务器 wlan0 主动断开后**被 AP 拒绝关联**：

```
21:44:54 wpa_supplicant: wlan0: CTRL-EVENT-DISCONNECTED bssid=<BSSID> reason=0 locally_generated=1
21:44:54 systemd-networkd: wlan0: Lost carrier
21:45:03 wpa_supplicant: wlan0: CTRL-EVENT-ASSOC-REJECT bssid=00:00:00:00:00:00 status_code=16
21:45:09 NetworkManager: device (wlan0): link timed out → activated -> failed (reason 'supplicant-timeout')
21:45:10 tailscaled: LinkChange: all links down; pausing → magicsock: SetNetworkUp(false)
```

系统本身一直存活（ESP-NOW 采集照常上报到 22:00:38 人为断电为止），
但 Tailscale 离线约 15 分钟无人自愈，直到重启系统才恢复。

## 已做的加固

```bash
# 1) 无限自动重连（默认 4 次就放弃）
sudo nmcli connection modify home connection.autoconnect-retries 0
# 2) 关闭 WiFi 省电（减少掉线），需重连生效
sudo nmcli connection modify home 802-11-wireless.powersave 2
sudo nmcli connection up home
# 3) 关闭扫描 MAC 随机化（部分 AP 会因此拒绝关联）
sudo install -m644 99-wifi-no-rand.conf /etc/NetworkManager/conf.d/99-wifi-no-rand.conf
sudo systemctl reload NetworkManager
# 4) 断网自愈定时器（每 2 分钟探测，连续 3 次失败自动恢复）
sudo install -m755 net-watchdog.sh /usr/local/bin/net-watchdog.sh
sudo install -m644 net-watchdog.service net-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now net-watchdog.timer
```

## 验证

```bash
nmcli -f connection.autoconnect-retries,802-11-wireless.powersave connection show home
#   connection.autoconnect-retries: 0 (forever)
#   802-11-wireless.powersave: 2 (disable)
systemctl list-timers net-watchdog.timer --no-pager
journalctl -t net-watchdog -n 20        # 正常运行时不产生日志（只有异常才记录）
```

## 其他经验

- **Tailscale 离线 ≠ 服务器掉电**：先看业务日志是否还在写（本例 `[ESPNOW-UP]` 一直在写）；
  整机掉电时 journal 会直接截断且没有 `Shutting down` / `Journal stopped`。
- 遇到类似情况优先 `sudo systemctl restart NetworkManager`，比反复拔电有效。
- 有线网口 `enP4p65s0` 目前是 DOWN，若条件允许接有线最稳（不依赖 AP）。
- 相关脚本：`net-watchdog.sh` / `net-watchdog.service` / `net-watchdog.timer` / `99-wifi-no-rand.conf`

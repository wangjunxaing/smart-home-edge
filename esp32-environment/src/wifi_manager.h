/*
 * WiFi 管理模块 — 头文件
 * ======================
 * 负责多网络连接、断线重连、mDNS 服务器地址解析。
 * 仅供 net_task 调用（网络操作集中在网络任务中，单一所有者）。
 */

#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include <Arduino.h>

// 多网络遍历连接（启动时调用一次，可能阻塞最多 15 秒 × 网络数）
bool wifiConnect();

// 断线检测与自动重连（net_task 每 10 秒调用一次）
void wifiCheck();

// 当前是否已连接 WiFi
bool wifiConnected();

// mDNS 解析服务器地址（60 秒缓存），成功返回 true
bool resolveServerHost();

// 构造完整服务器 URL（懒解析：未解析时先触发 mDNS 查询）
String makeURL(const char *path);

// 使 DNS 缓存失效（POST 失败时调用，触发下次重新解析）
void wifiInvalidateServerIP();

// 当前已解析的服务器 IP（未解析时先触发 mDNS 查询）。
// MQTT broker 与 HTTP 同机，传输层共用该地址。
IPAddress wifiServerIP();

#endif // WIFI_MANAGER_H
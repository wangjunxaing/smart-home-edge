/*
 * MQTT 传输层 — 头文件（阶段二 5.1）
 * ================================
 * 与 HTTP 传输共存、按服务器动态配置可切换（transportMode）。
 * 只被 net_task 调用（网络单一所有者原则，broker 与 HTTP 同机:1883）。
 *
 * 话题约定（与服务器 mqtt_bridge.py 对应）:
 *   /esp32/{id}/sensors  设备 → 服务器  传感器 JSON（与 HTTP POST body 同构 + t0_ms）
 *   /esp32/{id}/cmd      服务器 → 设备  下行控制命令 JSON
 *   /esp32/{id}/ack      设备 → 服务器  控制执行回执（含 seq + t_ms）
 *   /esp32/{id}/lwt      设备遗嘱       "offline"（异常断线由 broker 代发）
 */

#ifndef MQTT_CLIENT_H
#define MQTT_CLIENT_H

#include <Arduino.h>
#include "app_config.h"   // SensorReading / ESP32_ID 等

// 初始化 MQTT 客户端（首次进入 MQTT 模式时调用；可重复调用，幂等）
void mqttInit();

// 每轮维护：未连接则尝试连接（3s 退避），已连接则 loop() 收包
void mqttLoop();

// 当前是否已连接 broker
bool mqttConnected();

// 主动断开（切回 HTTP 模式时调用；正常 DISCONNECT 不触发遗嘱）
void mqttDisconnect();

// 发布一条传感器读数（同构 HTTP JSON，附 t0_ms 采集打点）
bool mqttPublishSensor(const SensorReading &r);

// 发布控制回执（cmd 处理入队成功后由 net_task 调用）
bool mqttPublishAck(uint32_t seq);

#endif // MQTT_CLIENT_H

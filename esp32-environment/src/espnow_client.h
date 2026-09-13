/*
 * ESP-NOW 节点客户端（5.4，节点侧）
 * =================================
 * 作用：把节点从"WiFi + HTTP/MQTT"里解耦出来——不连路由器，周期把传感器 JSON
 *       经 ESP-NOW 发给 USB 网关；接收网关下发的 light/ir/tts/config 命令并回 ACK。
 *
 *   节点 --DATA(seq, JSON)--> 网关 --USB--> Pi usb_gateway.py --> /api/esp32/sensors
 *   网关 --CMD--> 节点（applyDownlinkCmd 处理）--ACK(seq,RSSI)--> 网关（服务器测时延/丢包）
 *
 * 协议与 shared/espnow_proto.h 一致；信道必须与网关一致（默认 6）。
 * 默认发广播（网关会回 ACK）；调用 espnowSetGatewayId() 可改单播（更省电）。
 */
#ifndef ESPNOW_CLIENT_H
#define ESPNOW_CLIENT_H

#include <Arduino.h>
#include "app_config.h"

// 初始化：WiFi STA 但不连接路由、设置信道、注册回调、添加广播 peer（幂等）
void espnowInit();

// 关闭 ESP-NOW 并断开射频（切回 MQTT 模式时调用）
void espnowDeinit();

// 周期维护（当前实现为空操作：收发均在回调完成，便于后续加重传队列）
void espnowLoop();

// 是否已初始化
bool espnowReady();

// 上报一条传感器读数（内部做 ACK 等待与重传）；成功返回 true
bool espnowPublishSensor(const SensorReading &r);

// 可选：设置网关 12 位 ID（非 "FFFFFFFFFFFF" 时改单播）
void espnowSetGatewayId(const String &id12);

// 统计：tx=发送次数(含重传) ack=收到ACK lost=整周期失败 retry=重传次数
void espnowGetStats(uint32_t *tx, uint32_t *ack, uint32_t *lost, uint32_t *retry);

#endif // ESPNOW_CLIENT_H

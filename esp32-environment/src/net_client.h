/*
 * 网络通信模块 — 头文件（v1.9.0：MQTT / ESP-NOW 双传输）
 * =====================================================
 * 数据通道：MQTT 或 ESP-NOW；HTTP 仅作配置/OTA 控制面（维护窗口）。
 */

#ifndef NET_CLIENT_H
#define NET_CLIENT_H

#include <Arduino.h>    // uint32_t / size_t 等基础类型

// 网络任务主体（FreeRTOS 任务函数，不返回）
void netTask(void *param);

// 下行命令统一解析与入队（MQTT 回调 / ESP-NOW 回调共用）：
// 支持 light / ir / tts / ir_learn / config；返回需回执的 seq（0=无需回执）。
uint32_t applyDownlinkCmd(const char *json, size_t len);

#endif // NET_CLIENT_H
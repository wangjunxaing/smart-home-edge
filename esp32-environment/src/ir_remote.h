/*
 * 红外遥控模块 — 头文件
 * =====================
 * 基于 RMT 外设的 NEC 协议收发：
 *   发射 — GPIO27，RMT 通道 1（带 38kHz 载波调制）
 *   接收 — GPIO34，RMT 通道 2（接 1838 一体化接收头，已解调）
 * 发射由 control_task 调用；接收在 control_task 主循环中非阻塞轮询。
 */

#ifndef IR_REMOTE_H
#define IR_REMOTE_H

#include <Arduino.h>

// 初始化红外收发（setup 中调用一次）
void irRemoteInit();

// 发射一帧 NEC 32 位编码（阻塞约 70ms，一帧时长）
void irSendNEC(uint32_t data);

// 非阻塞接收：收到并解出一帧 NEC 码返回 true，编码写入 outData
bool irReceiveNEC(uint32_t *outData);

#endif // IR_REMOTE_H
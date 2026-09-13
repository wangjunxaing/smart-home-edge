/*
 * 控制任务模块 — 头文件
 * =====================
 * 系统"执行层"：接收 controlQueue 中的控制命令并执行。
 * 当前命令：OTA 固件升级；后续扩展：WS2812 灯带、红外遥控等执行器。
 * 同时负责 LED 状态指示（WiFi 连接常亮 / 断开熄灭 / OTA 期间闪烁）。
 */

#ifndef CONTROL_H
#define CONTROL_H

// LED 引脚初始化（setup 中调用一次）
void controlInit();

// 控制任务主体（FreeRTOS 任务函数，不返回）
void controlTask(void *param);

#endif // CONTROL_H
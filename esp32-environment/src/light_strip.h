/*
 * WS2812 灯带驱动模块 — 头文件
 * ============================
 * 由 control_task 独占调用（执行器单一所有者）。
 */

#ifndef LIGHT_STRIP_H
#define LIGHT_STRIP_H

#include <Arduino.h>

// 初始化灯带（setup 中调用一次），初始全灭
void lightStripInit();

// 应用灯带状态：开关 + RGB 颜色 + 亮度（0~255）+ 点亮数量（1~LED_STRIP_COUNT）
// 只点亮前 ledCount 颗灯珠（同色），其余保持熄灭。
void lightStripApply(bool on, uint8_t r, uint8_t g, uint8_t b,
                     uint8_t brightness, uint16_t ledCount);

#endif // LIGHT_STRIP_H
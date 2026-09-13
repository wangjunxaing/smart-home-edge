/*
 * 传感器采集模块 — 头文件
 * =======================
 * 负责 I2C 总线、BH1750 光照、AHT20 温湿度的初始化与周期采集。
 * 采集结果通过 sensorQueue 发送给 net_task，本模块不接触网络。
 */

#ifndef SENSORS_MOD_H
#define SENSORS_MOD_H

// 初始化 I2C 总线与两个传感器（setup 中调用一次）。
// 单个传感器失败只置降级标志，不阻塞其他外设。
void sensorsInit();

// 采集任务主体（FreeRTOS 任务函数，不返回）
void sensorTask(void *param);

#endif // SENSORS_MOD_H
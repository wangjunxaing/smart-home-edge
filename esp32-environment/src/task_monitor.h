/*
 * FreeRTOS 任务监控（5.10 深度调优）
 * ==================================
 * 目的：给论文第 4 章补"多任务实时性**实测**数据"（目前只有设计说明）。
 *
 * 采集三类指标：
 *   ① **栈余量**（uxTaskGetStackHighWaterMark）：任务启动以来栈的历史最小剩余
 *      → 判断栈是否给多了/给少了
 *   ② **周期抖动**（任务每轮打点）：实际间隔 vs 预期周期的偏差
 *      → 反映任务实际调度稳定性，比"设计周期"更可信
 *   ③ **超时次数**（间隔 > 预期 × 1.5）：被高优先级抢占或阻塞的次数
 *
 * ⚠ 单位坑：ESP-IDF 的 `uxTaskGetStackHighWaterMark()` 返回 **字节数**
 *   （FreeRTOS 原生返回 StackType_t 个字，但 ESP-IDF 里 StackType_t = uint8_t）。
 *   2026-11-14 首次实测时误按"字"×4 换算，得到"剩余 7680B > 分配 4096B"的矛盾值，
 *   据此修正为直接用返回值（字节）。
 *
 * 用法：
 *   1) main.cpp 创建任务后 taskMonitorRegister(name, handle, expected_ms, stack_bytes)
 *   2) 各任务 while 循环开头 taskMonitorTick(name)
 *   3) loop() 里每 30s taskMonitorReport()
 */
#ifndef TASK_MONITOR_H
#define TASK_MONITOR_H

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#define TM_MAX_TASKS  6
#define TM_MAX_NAME   12

struct TmStats {
  char name[TM_MAX_NAME];
  TaskHandle_t handle;
  uint32_t expected_ms;      // 预期周期（0 = 无固定周期，只测栈）
  uint32_t stack_bytes;      // 创建时分配的栈大小（字节），用于算余量百分比
  uint32_t last_ms;          // 上次打点时刻
  uint32_t first_ms;         // 首次打点时刻
  uint32_t min_gap;          // 实测最小间隔
  uint32_t max_gap;          // 实测最大间隔
  uint64_t sum_gap;          // 间隔累加（算平均）
  uint32_t count;            // 打点次数
  uint32_t overruns;         // 超时次数（gap > expected*1.5 + 20ms 容差）
  bool registered;
};

// 注册任务（创建任务后调用）。handle=NULL 时只做打点，不报栈余量。
void taskMonitorRegister(const char *name, TaskHandle_t handle,
                         uint32_t expected_ms, uint32_t stack_bytes);

// 任务每轮循环开始调用（按任务名匹配）
void taskMonitorTick(const char *name);

// 打印一行汇总（由 loop() 定期调用）
void taskMonitorReport();

#endif  // TASK_MONITOR_H

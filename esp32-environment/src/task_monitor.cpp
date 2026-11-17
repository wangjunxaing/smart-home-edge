/*
 * FreeRTOS 任务监控实现（5.10）
 */
#include "task_monitor.h"

#include <string.h>

static TmStats s_tasks[TM_MAX_TASKS];
static uint8_t s_count = 0;

static TmStats *findSlot(const char *name) {
  for (uint8_t i = 0; i < s_count; i++) {
    if (strncmp(s_tasks[i].name, name, TM_MAX_NAME - 1) == 0) return &s_tasks[i];
  }
  return NULL;
}

void taskMonitorRegister(const char *name, TaskHandle_t handle,
                         uint32_t expected_ms, uint32_t stack_bytes) {
  if (s_count >= TM_MAX_TASKS) return;
  TmStats *s = findSlot(name);
  if (s == NULL) {
    s = &s_tasks[s_count++];
    memset(s, 0, sizeof(*s));
    strncpy(s->name, name, TM_MAX_NAME - 1);
    s->min_gap = 0xFFFFFFFF;
  }
  s->handle = handle;
  s->expected_ms = expected_ms;
  s->stack_bytes = stack_bytes;
  s->registered = true;
}

void taskMonitorTick(const char *name) {
  TmStats *s = findSlot(name);
  if (s == NULL) return;
  uint32_t now = millis();
  if (s->count == 0) {
    s->first_ms = now;
  } else {
    uint32_t gap = now - s->last_ms;
    if (gap < s->min_gap) s->min_gap = gap;
    if (gap > s->max_gap) s->max_gap = gap;
    s->sum_gap += gap;
    if (s->expected_ms > 0) {
      uint32_t limit = (uint32_t)(s->expected_ms * 1.5f) + 20;
      if (gap > limit) s->overruns++;
    }
  }
  s->last_ms = now;
  s->count++;
}

void taskMonitorReport() {
  Serial.println("[FreeRTOS] ---- 任务监控（栈余量 / 周期抖动）----");
  Serial.printf("[FreeRTOS] %-8s %5s %10s %8s %8s %8s %8s\n",
                "task", "used", "stack(free)", "min_gap", "avg_gap", "max_gap", "overrun");
  for (uint8_t i = 0; i < s_count; i++) {
    TmStats *s = &s_tasks[i];
    // 栈余量：ESP-IDF 的 uxTaskGetStackHighWaterMark 返回**字节**
    uint32_t free_b = 0;
    if (s->handle != NULL) {
      free_b = (uint32_t)uxTaskGetStackHighWaterMark(s->handle);
    }
    // 平均间隔
    uint32_t avg = 0;
    if (s->count > 1) {
      avg = (uint32_t)(s->sum_gap / (s->count - 1));
    }
    // 使用率：(分配 - 剩余) / 分配
    char used_pct[8] = "-";
    char stack_str[24] = "n/a";
    if (s->handle != NULL && s->stack_bytes > 0) {
      uint32_t used = (s->stack_bytes > free_b) ? (s->stack_bytes - free_b) : 0;
      snprintf(used_pct, sizeof(used_pct), "%lu%%",
               (unsigned long)(used * 100 / s->stack_bytes));
      snprintf(stack_str, sizeof(stack_str), "%lu/%luB",
               (unsigned long)free_b, (unsigned long)s->stack_bytes);
    }
    Serial.printf("[FreeRTOS] %-8s %5s %10s %7lums %7lums %7lums %6lu\n",
                  s->name, used_pct, stack_str,
                  (unsigned long)(s->min_gap == 0xFFFFFFFF ? 0 : s->min_gap),
                  (unsigned long)avg,
                  (unsigned long)s->max_gap,
                  (unsigned long)s->overruns);
  }
  Serial.printf("[FreeRTOS] 预期周期: sensor=%lums net=%lums control=%lums\n",
                (unsigned long)(findSlot("sensor") ? findSlot("sensor")->expected_ms : 0),
                (unsigned long)(findSlot("net") ? findSlot("net")->expected_ms : 0),
                (unsigned long)(findSlot("control") ? findSlot("control")->expected_ms : 0));
}

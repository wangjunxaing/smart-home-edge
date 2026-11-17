/*
 * 24GHz 毫米波人体感知（5.8）— 双协议：LD2410C + LD2450
 * ======================================================
 * 接线（UART1 重映射，见 PLAN §7.3.2）：
 *   ESP32 GPIO18 (TX) → 模块 RX
 *   ESP32 GPIO19 (RX) ← 模块 TX
 *   3V3 / GND 共地（3.3V TTL，直连无需电平转换）
 *   OUT（仅 LD2410C 有，可选）→ ESP32 GPIO14，兼作 5.9 Deep Sleep ext0 唤醒源
 *
 * 为什么必须重映射：
 *   UART1 默认脚 GPIO9/10 属 Flash 引脚，接模块会起不来；
 *   GPIO16/17 已被节点②的 TTS（UART2）独占 → 用 GPIO 矩阵映射到 18/19。
 *
 * 两种模块（固件自动识别，无需改代码）：
 *   LD2410C：只有"距离"（无方位），OUT 脚可用，帧头 F4 F3 F2 F1
 *   LD2450：**只有 x/y 平面坐标**（2D，无高度），**没有 OUT**，帧头 AA FF 03 00
 *
 * 设计要点：
 *   - **非阻塞**：poll() 只消费串口已有字节，不 await；controlTask/sensorTask 周期调用。
 *   - **不抛异常**：解析失败只丢帧并计数，绝不影响温湿度等主传感器。
 *   - `raw_hex`：前 32 字节的十六进制，供远程确认模块实际输出（诊断神器）。
 */

#ifndef MMWAVE_H
#define MMWAVE_H

#include <Arduino.h>

// 一次解析结果（sensorTask 采集时取快照）
struct MmwaveReading {
  int8_t person;          // 有人判定：1=有, 0=无, -1=未知（未收到有效帧）
  int16_t moving_dist;    // 运动目标距离 (cm)，-1 = 无效
  int16_t still_dist;     // 静止目标距离 (cm)，-1 = 无效
  int16_t detect_dist;    // 探测距离 (cm)，-1 = 无效
  uint8_t moving_energy;  // 运动能量 0~100（LD2450 无此概念，用速度代替）
  uint8_t still_energy;   // 静止能量 0~100
  uint8_t target_state;   // 原始目标状态 0~3（LD2450 用 1=运动 2=静止）
  // ---- LD2450 专属：平面坐标（2D，非 3D）----
  int16_t x_mm;           // x 坐标 (mm)，正=右；-1 = 无效
  int16_t y_mm;           // y 坐标 (mm)，正=前方探测方向；-1 = 无效
  int16_t speed_cms;      // 速度 (cm/s)，正=靠近 负=远离
  uint8_t protocol;       // 1=LD2410C 2=LD2450 0=未知
  bool out_pin;           // OUT 脚电平（仅 LD2410C 有效）
  bool valid;             // 是否已收到过有效帧
  uint32_t last_update_ms;// 最后一次有效帧的 millis()
};

// 初始化 UART1（setup 中调用一次）。失败只告警，不阻塞启动。
void mmwaveInit();

// 非阻塞消费串口数据并更新内部状态
void mmwavePoll();

// 取当前读数快照
MmwaveReading mmwaveGet();

// 是否已收到过有效帧（模块接线/供电/协议正常性判断）
bool mmwaveReady();

// 链路统计（远程诊断用，不依赖串口）
struct MmwaveStats {
  uint32_t rx_bytes;
  uint32_t frames_ok;
  uint32_t frames_bad;
  uint32_t cmd_frames;    // 命令响应帧数（>0 = LD2410 双向通信正常）
  uint32_t baud;          // 当前使用的波特率（自动探测结果）
  uint8_t baud_tries;     // 已尝试过几次切换
  uint8_t protocol;       // 1=LD2410C 2=LD2450 0=未知
  bool inited;
};
MmwaveStats mmwaveStats();

// 取前 32 字节的十六进制串（最多 64 字符 + '\0'），写入 out，返回长度。
// 用于远程诊断："模块到底吐的是 LD2410C 还是 LD2450 帧？"
size_t mmwaveGetRawHex(char *out, size_t cap);

#endif // MMWAVE_H

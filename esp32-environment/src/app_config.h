/*
 * 全局共享配置与数据结构定义
 * ==========================
 * 多任务架构下各模块共享的常量、数据类型和全局对象声明。
 * 定义（实体）在 app_state.cpp 中，其他模块通过本头文件引用。
 *
 * 任务间通信模型:
 *   sensor_task  --SensorReading-->  sensorQueue  -->  net_task
 *   net_task     --ControlCommand--> controlQueue --> control_task
 *   共享配置 (SEND_INTERVAL/deviceLocation) 由 configMutex 保护
 */

#ifndef APP_CONFIG_H
#define APP_CONFIG_H

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>

// ===================== 固件版本 =====================
// OTA 升级时递增。服务器按版本号判断是否需要下发新固件。
#define FW_VERSION "1.9.10"

// ===================== 硬件引脚 =====================
#define LED_PIN 2               // 板载 LED（GPIO2）
#define LED_STRIP_PIN 4         // WS2812 灯带数据线（GPIO4）
#define LED_STRIP_COUNT 60      // 灯带总灯珠数量（两串 30 串联 = 60）
#define IR_TX_PIN 27            // 红外发射模块 DAT（GPIO27，RMT 通道1）
#define IR_RX_PIN 34            // 红外接收模块 DAT（GPIO34 仅输入，RMT 通道2）
#define MIC_STREAM_ENABLED 1    // INMP441 麦克风语音流（1 启用 / 0 禁用）
#define I2C_SDA_PIN 21          // I2C 数据线（BH1750/AHT20 共用）
#define I2C_SCL_PIN 22          // I2C 时钟线
#define LIGHT_DEFAULT_COUNT 10  // 灯带默认点亮数量（服务器可动态修改 1~LED_STRIP_COUNT）

// ===================== 服务器连接 =====================
extern const char *SERVER_HOST; // mDNS 主机名（wang-orange.local）
extern const int SERVER_PORT;   // HTTP 端口（8080）

// ===================== 设备标识 =====================
// 使用 WiFi STA 模式 MAC 地址作为全局唯一 ID（12 位大写十六进制）。
extern const String ESP32_ID;

// ===================== 任务间数据类型 =====================

// 传感器读数：sensor_task 生产，net_task 消费。
// 无效数据用 -1.0 哨兵值表示（不写入正常上报 JSON，走异常分流）。
struct SensorReading {
  float lux;                    // 光照 (lx)，-1.0 = 无效
  float temperature;            // 温度 (°C)，-1.0 = 无效
  float humidity;               // 湿度 (%RH)，-1.0 = 无效
  int rssi;                     // WiFi 信号强度 (dBm)
  float current_ma;             // 节点电流 (mA，INA219 采样)，-1.0 = 未接/无效
  uint64_t t0_ms;               // 采集完成时刻（esp_timer 上电毫秒），论文时延测量（t0_ms 随包上报）
};

// 控制命令类型：net_task 生产，control_task 消费。
enum ControlType {
  CTRL_OTA_UPDATE,              // 执行 OTA 固件升级
  CTRL_LIGHT_SET,               // 设置 WS2812 灯带状态
  CTRL_IR_SEND,                 // 发射红外 NEC 编码
  CTRL_TTS_SPEAK                // TTS 播报（GB2312 文本，十六进制串传输）
};

struct ControlCommand {
  ControlType type;
  uint32_t seq;                 // 服务端下发序号（MQTT ack 回执用；HTTP 轮询路径也可带）
  char path[96];                // 固件下载路径（服务器相对路径）
  char version[16];             // 目标版本号
  // CTRL_LIGHT_SET 负载
  uint8_t on;                   // 开关
  uint8_t r, g, b;              // 颜色
  uint8_t brightness;           // 亮度（0~255）
  uint8_t led_count;            // 点亮灯珠数量（1~LED_STRIP_COUNT）
  // CTRL_IR_SEND 负载
  uint32_t ir_data;             // NEC 32 位编码
  // CTRL_TTS_SPEAK 负载：GB2312 文本的十六进制串（服务器转码，设备直接转发给 TTS 模块）
  char tts_hex[400];
};

// ===================== 任务间通信对象 =====================
extern QueueHandle_t sensorQueue;    // SensorReading 队列（长度 8，断网缓冲）
extern QueueHandle_t controlQueue;   // ControlCommand 队列（长度 2）

// 红外学习上报：control_task 生产，net_task 消费（上报服务器）
struct IrReport {
  uint32_t data;                // 收到的 NEC 32 位编码
};
extern QueueHandle_t irReportQueue;    // IrReport 队列（长度 4）
extern volatile bool irLearnMode;      // 学习模式开关（net_task 写，control_task 读）

// ===================== 共享运行配置 =====================
// net_task 通过远程配置接口修改，sensor_task 读取，由 configMutex 保护。
extern SemaphoreHandle_t configMutex;
extern unsigned long SEND_INTERVAL;  // 上报周期（1000~60000ms，服务器可动态修改）
extern String deviceLocation;        // 位置分类（服务器可动态修改）
extern int payloadPad;               // 上行 JSON 填充字节数（0~80，T5 载荷扫描；0=不填充）

// ===================== 传输模式（v1.9.0：MQTT / ESP-NOW 双模式） =====================
// HTTP 数据通道已移除；HTTP 仅用于配置/OTA 控制面（MQTT 模式 30s，ESP-NOW 维护窗口）。
enum TransportMode {
  TRANSPORT_MQTT = 0,     // MQTT 长连接（连路由器）
  TRANSPORT_ESPNOW = 1    // ESP-NOW（不连路由器，经 USB 网关接入 Pi）
};
extern TransportMode transportMode;   // 当前传输模式（configMutex 保护读写）

#endif // APP_CONFIG_H
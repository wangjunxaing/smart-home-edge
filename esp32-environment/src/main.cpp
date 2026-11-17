/*
 * ESP32 智能家居边缘节点 — 主程序（FreeRTOS 多任务架构）
 * ======================================================
 * v6 架构升级：从单循环 millis() 轮询重构为显式 FreeRTOS 三任务模型。
 *
 * 任务划分与双核分配:
 *   ┌─────────────┬──────┬──────┬────────┬──────────────────────────┐
 *   │ 任务        │ 核心 │ 优先级│ 栈(B)  │ 职责                     │
 *   ├─────────────┼──────┼──────┼────────┼──────────────────────────┤
 *   │ sensor_task │  1   │  2   │ 4096   │ I2C 传感器周期采集       │
 *   │ net_task    │  0   │  2   │ 8192   │ 全部 HTTP 通信/配置/OTA  │
 *   │ control_task│  1   │  3   │ 8192   │ 控制命令执行 + LED 指示  │
 *   └─────────────┴──────┴──────┴────────┴──────────────────────────┘
 *
 * 核心分配理由:
 *   - Arduino-ESP32 的 WiFi 驱动与 lwIP 协议栈运行在内核 0，
 *     net_task 固定在内核 0 靠近网络栈，减少跨核上下文切换
 *   - sensor_task 在内核 1，I2C 时序不受网络中断突发影响，采集周期更稳定
 *   - control_task 在内核 1，OTA 刷写（数十秒阻塞）不影响内核 0 的网络收发
 *
 * 任务间通信:
 *   sensor_task --SensorReading--> sensorQueue(8) --> net_task
 *   net_task    --ControlCommand-> controlQueue(2) -> control_task
 *   共享配置 (SEND_INTERVAL/deviceLocation) 由 configMutex 保护
 *
 * 模块划分:
 *   app_config.h / app_state.cpp  共享常量、数据类型、全局对象
 *   wifi_manager.h/.cpp           WiFi 多网络连接、mDNS 解析
 *   sensors_mod.h/.cpp            BH1750/AHT20 采集（支持部分降级）
 *   net_client.h/.cpp             HTTP 上报、配置拉取、OTA 检查
 *   control.h/.cpp                控制命令执行、LED 状态指示
 *   ota_update.h/.cpp             OTA 固件下载刷写（HTTPUpdate）
 *   mic_vad.cpp                   麦克风/VAD（暂时禁用，MIC_VAD_ENABLED=0）
 */
#include <Arduino.h>
#include "app_config.h"
#include "wifi_manager.h"
#include "sensors_mod.h"
#include "net_client.h"
#include "control.h"
#include "light_strip.h"
#include "ir_remote.h"
#include "audio_mic.h"
#include "ota_update.h"   // otaBootHealthCheck()
#include "tts_voice.h"    // ttsInit()
#include <esp_task_wdt.h>
#include "task_monitor.h"

// ===================== 任务参数配置 =====================
#define SENSOR_TASK_STACK   4096
#define NET_TASK_STACK      8192
// 5.10 收尾（2026-11-15）：实测 control 只用 17%（约 1.4KB / 8192B），
// 故由 8192 缩到 4096（仍有 ~2.9 倍余量），省 4KB RAM。
// ⚠ 若后续在 controlTask 里加重操作（如大文件 OTA、长 TTS 文本），需复查栈余量。
#define CONTROL_TASK_STACK  4096
#define AUDIO_TASK_STACK    8192
#define SENSOR_TASK_PRIO    2
#define NET_TASK_PRIO       2
#define CONTROL_TASK_PRIO   3
#define SENSOR_TASK_CORE    1
#define NET_TASK_CORE       0
#define CONTROL_TASK_CORE   1

// ===================== setup() - 系统初始化 =====================
// 初始化顺序:
//   1. 外设（LED、I2C 传感器）——不依赖网络
//   2. 通信对象（队列/互斥锁）——任务启动前必须就绪
//   3. WiFi 首次连接——可能耗时最长（每网络最多 15 秒）
//   4. 创建三个 FreeRTOS 任务
void setup() {
  Serial.begin(115200);
  Serial.println();
  Serial.println("======================================");
  Serial.printf("ESP32 智能家居边缘节点 v%s (FreeRTOS)\n", FW_VERSION);
  Serial.printf("ESP32 ID: %s\n", ESP32_ID.c_str());
  Serial.println("======================================");

  // 0a. OTA 引导健康检查（5.2）：新固件连续异常启动超阈值 → 自动回滚旧分区
  otaBootHealthCheck();

  // 0. 任务看门狗：15 秒无喂狗判定任务卡死，panic 自动重启（故障自愈）
  esp_task_wdt_deinit();
  esp_task_wdt_init(15, true);
  esp_task_wdt_add(NULL);   // 订阅 loopTask（setup/loop 所在任务）

  // 1. 外设初始化
  controlInit();        // LED 引脚
  lightStripInit();     // WS2812 灯带（上电全灭）
  irRemoteInit();       // 红外收发（RMT 通道 1/2）
  ttsInit();            // TTS 语音模块（UART2 9600，WT3000T-MO1）
  audioMicInit();       // INMP441 麦克风（I2S）
  sensorsInit();        // I2C 总线 + BH1750/AHT20（单传感器失败降级运行）

  // 2. 创建任务间通信对象
  configMutex = xSemaphoreCreateMutex();
  sensorQueue = xQueueCreate(8, sizeof(SensorReading));    // 8 条缓冲（断网暂存）
  controlQueue = xQueueCreate(2, sizeof(ControlCommand));
  irReportQueue = xQueueCreate(4, sizeof(IrReport));
  if (!configMutex || !sensorQueue || !controlQueue || !irReportQueue) {
    Serial.println("[系统] 队列/互斥锁创建失败，重启...");
    ESP.restart();
  }

  // 3. WiFi 首次连接（多网络模式）
  Serial.println("[WiFi] 开始连接（多网络模式）...");
  wifiConnect();
  if (wifiConnected()) resolveServerHost();

  // 4. 创建 FreeRTOS 任务（双核固定）
  //    5.10：保存任务句柄，供 task_monitor 读栈余量（uxTaskGetStackHighWaterMark）
  TaskHandle_t hSensor = NULL, hNet = NULL, hControl = NULL, hAudio = NULL;
  xTaskCreatePinnedToCore(sensorTask, "sensor",
                          SENSOR_TASK_STACK, NULL, SENSOR_TASK_PRIO,
                          &hSensor, SENSOR_TASK_CORE);
  xTaskCreatePinnedToCore(netTask, "net",
                          NET_TASK_STACK, NULL, NET_TASK_PRIO,
                          &hNet, NET_TASK_CORE);
  xTaskCreatePinnedToCore(controlTask, "control",
                          CONTROL_TASK_STACK, NULL, CONTROL_TASK_PRIO,
                          &hControl, CONTROL_TASK_CORE);
  xTaskCreatePinnedToCore(audioTask, "audio",
                          AUDIO_TASK_STACK, NULL, 2, &hAudio, 1);   // 音频采集/上报（内核1）

  // 5.10 任务监控注册：预期周期用于抖动统计（0 = 事件驱动，无固定周期）
  taskMonitorRegister("sensor", hSensor, 5000, SENSOR_TASK_STACK);
  taskMonitorRegister("net", hNet, 5000, NET_TASK_STACK);
  taskMonitorRegister("control", hControl, 0, CONTROL_TASK_STACK);
  taskMonitorRegister("audio", hAudio, 0, AUDIO_TASK_STACK);

  Serial.println("======================================");
  Serial.println("启动完成：FreeRTOS 四任务运行中（采集/网络/控制/音频）");
  Serial.println("[FreeRTOS] 任务监控已启用（每 30s 打印栈余量/周期抖动）");
  Serial.println("======================================");
}

// ===================== loop() =====================
// 所有工作由 FreeRTOS 任务承担，Arduino loop 让出 CPU。
// 看门狗超时 15s，这里 2s 喂一次（留足余量，避免调度抖动导致误判）。
// 5.10：顺带每 30s 打一次任务监控报告（栈余量 + 周期抖动）。
void loop() {
  esp_task_wdt_reset();   // loopTask 喂狗
  static uint32_t lastReport = 0;
  if (millis() - lastReport >= 30000) {
    lastReport = millis();
    taskMonitorReport();
  }
  vTaskDelay(pdMS_TO_TICKS(2000));
}
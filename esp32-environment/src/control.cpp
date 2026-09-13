/*
 * 控制任务模块
 * ============
 * 阻塞等待 controlQueue 命令，无命令时每 500ms 刷新一次 LED 状态。
 * OTA 升级期间任务阻塞在 otaPerform() 内，LED 交由 HTTPUpdate 自动闪烁，
 * 其余两个任务（采集/网络）不受影响——这正是多任务架构的优势。
 */
#include "control.h"
#include "app_config.h"
#include "wifi_manager.h"
#include "ota_update.h"
#include "light_strip.h"
#include "ir_remote.h"
#include "tts_voice.h"
#include <esp_task_wdt.h>

void controlInit() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
}

void controlTask(void *param) {
  (void)param;
  esp_task_wdt_add(NULL);   // 挂入任务看门狗
  ControlCommand cmd;

  for (;;) {
    // 500ms 超时等待：有命令立即响应，无命令周期刷新 LED
    if (xQueueReceive(controlQueue, &cmd, pdMS_TO_TICKS(500)) == pdTRUE) {
      switch (cmd.type) {
        case CTRL_OTA_UPDATE:
          Serial.printf("[控制] 收到 OTA 升级指令，目标版本 v%s\n", cmd.version);
          // OTA 刷写阻塞数十秒，超过看门狗超时：先退订，失败返回后再订阅
          esp_task_wdt_delete(NULL);
          otaPerform(cmd.path, cmd.version);   // 阻塞执行；成功则自动重启
          esp_task_wdt_add(NULL);
          break;
        case CTRL_LIGHT_SET:   // 灯带控制：即时应用，毫秒级完成
          lightStripApply(cmd.on, cmd.r, cmd.g, cmd.b, cmd.brightness, cmd.led_count);
          break;
        case CTRL_IR_SEND:      // 红外发射：一帧约 70ms
          irSendNEC(cmd.ir_data);
          break;
        case CTRL_TTS_SPEAK:    // TTS 播报（GB2312 hex → WT3000T-MO1，UART2）
          ttsSpeakHex(cmd.tts_hex);
          break;
      }
    }

    // 红外接收轮询（非阻塞）：学习模式下收到 NEC 码交给 net_task 上报
    uint32_t irData;
    if (irLearnMode && irReceiveNEC(&irData)) {
      Serial.printf("[红外] 学习到 NEC 0x%08X\n", (unsigned)irData);
      IrReport rep;
      rep.data = irData;
      xQueueSend(irReportQueue, &rep, 0);
    }

    // LED 状态派生：WiFi 已连接常亮，未连接熄灭
    digitalWrite(LED_PIN, wifiConnected() ? HIGH : LOW);
    esp_task_wdt_reset();   // 喂狗（循环周期 500ms）
  }
}
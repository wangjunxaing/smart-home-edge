/*
 * WS2812 灯带驱动模块 — 基于 ESP32 RMT 外设的手写驱动
 * ====================================================
 * 接线: S(信号)->GPIO4, V(电源)->VIN(5V), G(地)->GND
 *
 * 为什么用 RMT 而不是软件翻转 GPIO:
 *   WS2812 单总线协议对脉宽的要求是百纳秒级（T0H≈0.4us / T1H≈0.8us），
 *   软件延时翻转会被 WiFi 中断打断，导致灯珠颜色随机错乱。
 *   RMT（红外遥控）外设本质是一个"波形回放引擎"：CPU 把每位的高低电平
 *   持续时间填进内存描述符，RMT 硬件 + DMA 自动按时间播出，全程不占 CPU、
 *   不受中断影响——这正是它被借来驱动 WS2812 的原因。
 *
 * 时序参数（WS2812 数据手册）:
 *   码 0: 高 0.4us + 低 0.85us     码 1: 高 0.8us + 低 0.45us
 *   复位: 低电平保持 > 50us（本驱动取 300us，兼容 WS2812B-V5 的 280us 要求）
 *   RMT 时钟 80MHz / 分频 2 = 40MHz → 每 tick 25ns
 *   → T0H=16, T0L=34, T1H=32, T1L=18（单位 tick）
 *
 * 数据格式: 每颗灯珠 24bit，GRB 顺序（先绿后红再蓝），高位先发。
 */
#include "light_strip.h"
#include "app_config.h"
#include <driver/rmt.h>

static const rmt_channel_t LIGHT_RMT_CHANNEL = RMT_CHANNEL_0;

// 时序常量（单位: 25ns tick）
static const uint16_t WS_T0H = 16, WS_T0L = 34, WS_T1H = 32, WS_T1L = 18;

// RMT 波形描述符缓冲区：每灯珠 24 位 × 4 字节
static rmt_item32_t rmtItems[LED_STRIP_COUNT * 24];

void lightStripInit() {
  rmt_config_t config = RMT_DEFAULT_CONFIG_TX((gpio_num_t)LED_STRIP_PIN,
                                              LIGHT_RMT_CHANNEL);
  config.clk_div = 2;                 // 80MHz / 2 = 40MHz（25ns/tick）
  esp_err_t err = rmt_config(&config);
  if (err == ESP_OK) err = rmt_driver_install(LIGHT_RMT_CHANNEL, 0, 0);
  Serial.printf("[灯带] RMT 驱动初始化%s（GPIO%d，共 %d 颗灯珠，默认点亮 %d 颗）\n",
                err == ESP_OK ? "完成" : "失败", LED_STRIP_PIN,
                LED_STRIP_COUNT, LIGHT_DEFAULT_COUNT);
  lightStripApply(false, 0, 0, 0, 255, LIGHT_DEFAULT_COUNT);   // 上电全灭
}

void lightStripApply(bool on, uint8_t r, uint8_t g, uint8_t b,
                     uint8_t brightness, uint16_t ledCount) {
  // 亮度缩放（0~255 比例作用于每路颜色）
  uint32_t rr = on ? (r * brightness) / 255 : 0;
  uint32_t gg = on ? (g * brightness) / 255 : 0;
  uint32_t bb = on ? (b * brightness) / 255 : 0;

  // 点亮数量边界校验（1~LED_STRIP_COUNT；0 视为全部关闭）
  if (ledCount > LED_STRIP_COUNT) ledCount = LED_STRIP_COUNT;

  // 将 GRB 颜色流编码为 RMT 波形描述符：
  //   前 ledCount 颗灯珠显示颜色（on 时），其余灯珠置 0（熄灭）
  for (int led = 0; led < LED_STRIP_COUNT; led++) {
    uint32_t grb;
    if (on && (uint32_t)led < ledCount) {
      grb = ((uint32_t)gg << 16) | ((uint32_t)rr << 8) | bb;  // GRB 顺序
    } else {
      grb = 0;                                               // 熄灭
    }
    for (int bit = 23; bit >= 0; bit--) {           // 高位先发
      int idx = led * 24 + (23 - bit);
      if (grb & (1UL << bit)) {
        rmtItems[idx].level0 = 1; rmtItems[idx].duration0 = WS_T1H;
        rmtItems[idx].level1 = 0; rmtItems[idx].duration1 = WS_T1L;
      } else {
        rmtItems[idx].level0 = 1; rmtItems[idx].duration0 = WS_T0H;
        rmtItems[idx].level1 = 0; rmtItems[idx].duration1 = WS_T0L;
      }
    }
  }

  // 发送波形并等待完成，随后保持低电平形成复位信号
  rmt_write_items(LIGHT_RMT_CHANNEL, rmtItems, LED_STRIP_COUNT * 24, true);
  rmt_wait_tx_done(LIGHT_RMT_CHANNEL, portMAX_DELAY);
  ets_delay_us(300);                  // > 50us 低电平 = 复位锁存

  Serial.printf("[灯带] %s RGB(%d,%d,%d) 亮度 %d 点亮 %d/%d\n",
                on ? "开" : "关", r, g, b, brightness,
                on ? ledCount : 0, LED_STRIP_COUNT);
}
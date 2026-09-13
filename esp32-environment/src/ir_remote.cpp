/*
 * 红外遥控模块 — RMT 手写 NEC 协议收发驱动
 * ==========================================
 * 接线:
 *   发射模块: DAT->GPIO27, VCC->VIN(5V), GND->GND
 *   1838 接收头: DAT->GPIO34, VCC->3V3(务必 3.3V，保护 GPIO), GND->GND
 *
 * 原理:
 *   红外遥控用 38kHz 载波"有无"表示高低电平。1838 接收头内部完成解调，
 *   输出直接是电平信号；发射侧则由 RMT 外设自动生成 38kHz 载波。
 *   NEC 协议（最常见的家电遥控协议）:
 *     引导码: 9ms 载波 + 4.5ms 空闲
 *     位 0 : 562.5us 载波 + 562.5us 空闲
 *     位 1 : 562.5us 载波 + 1687.5us 空闲
 *     数据 : 32 位（地址 + 地址反码 + 命令 + 命令反码），低位先发
 *     结束 : 562.5us 载波
 *   RMT 时钟 80MHz / 80 分频 = 1MHz → 1 tick = 1us，时长值即微秒数。
 *
 * RMT 通道分配: 0=WS2812 灯带(TX)  1=红外发射(TX)  2=红外接收(RX)
 */
#include "ir_remote.h"
#include "app_config.h"
#include <driver/rmt.h>

static const rmt_channel_t IR_TX_CHANNEL = RMT_CHANNEL_1;
static const rmt_channel_t IR_RX_CHANNEL = RMT_CHANNEL_2;
static RingbufHandle_t irRxRingbuf = NULL;

void irRemoteInit() {
  // ---- 发射通道：1us/tick + 38kHz 载波 ----
  rmt_config_t txcfg = RMT_DEFAULT_CONFIG_TX((gpio_num_t)IR_TX_PIN, IR_TX_CHANNEL);
  txcfg.clk_div = 80;
  txcfg.tx_config.carrier_en = true;
  txcfg.tx_config.carrier_freq_hz = 38000;     // 由 1MHz 分频 ≈ 38.5kHz，接收头容差内
  txcfg.tx_config.carrier_duty_percent = 33;   // 1/3 占空比，兼顾发射管功耗
  txcfg.tx_config.idle_level = RMT_IDLE_LEVEL_LOW;
  txcfg.tx_config.idle_output_en = true;
  rmt_config(&txcfg);
  rmt_driver_install(IR_TX_CHANNEL, 0, 0);

  // ---- 接收通道：1us/tick + 毛刺滤波 + 帧结束判定 ----
  rmt_config_t rxcfg = RMT_DEFAULT_CONFIG_RX((gpio_num_t)IR_RX_PIN, IR_RX_CHANNEL);
  rxcfg.clk_div = 80;
  rxcfg.rx_config.filter_en = true;
  rxcfg.rx_config.filter_ticks_thresh = 100;   // 滤除 <100us 的毛刺（真实脉宽 >= 562us）
  rxcfg.rx_config.idle_threshold = 12000;      // 12ms 无信号 = 一帧结束
  rmt_config(&rxcfg);
  rmt_driver_install(IR_RX_CHANNEL, 2048, 0);
  rmt_get_ringbuf_handle(IR_RX_CHANNEL, &irRxRingbuf);
  rmt_rx_start(IR_RX_CHANNEL, true);

  Serial.printf("[红外] 初始化完成（发射 GPIO%d / 接收 GPIO%d，38kHz NEC）\n",
                IR_TX_PIN, IR_RX_PIN);
}

void irSendNEC(uint32_t data) {
  rmt_item32_t items[67];
  int n = 0;
  // 填一对电平：先载波（高）后空闲（低），时长单位 us
  #define IR_FILL(highUs, lowUs) do { \
    items[n].level0 = 1; items[n].duration0 = (highUs); \
    items[n].level1 = 0; items[n].duration1 = (lowUs); \
    n++; \
  } while (0)

  IR_FILL(9000, 4500);                         // 引导码
  for (int i = 0; i < 32; i++) {               // 32 位，低位先发
    IR_FILL(563, (data >> i) & 1 ? 1687 : 563);
  }
  IR_FILL(563, 0);                             // 结束载波
  #undef IR_FILL

  rmt_write_items(IR_TX_CHANNEL, items, n, true);
  rmt_wait_tx_done(IR_TX_CHANNEL, portMAX_DELAY);
  Serial.printf("[红外] 发射 NEC 0x%08X\n", (unsigned)data);
}

bool irReceiveNEC(uint32_t *outData) {
  if (!irRxRingbuf) return false;
  size_t len = 0;
  rmt_item32_t *items = (rmt_item32_t *)xRingbufferReceive(irRxRingbuf, &len, 0);
  if (!items) return false;

  size_t count = len / sizeof(rmt_item32_t);
  bool ok = false;
  uint32_t data = 0;

  if (count >= 33) {
    // 定位引导码（9ms + 4.5ms，容差 ±2ms）；RMT RX 可能把帧前空闲也记为一项
    int start = -1;
    for (size_t i = 0; i + 1 < count; i++) {
      if (items[i].duration0 > 7000 && items[i].duration0 < 11000 &&
          items[i].duration1 > 3500 && items[i].duration1 < 5500) {
        start = (int)i;
        break;
      }
    }
    if (start >= 0 && (int)count - start - 1 >= 32) {
      ok = true;
      for (int i = 0; i < 32; i++) {
        // 位判定看"空闲段"时长：长空闲 = 1
        if (items[start + 1 + i].duration1 > 1000)
          data |= (1UL << i);
      }
    }
  }

  vRingbufferReturnItem(irRxRingbuf, items);
  if (ok) *outData = data;
  return ok;
}
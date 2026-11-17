/*
 * ESP-NOW 无线节点固件（阶段二 5.4）+ 低功耗实验（5.9 / T9）
 * ============================================================
 * 定位：不连路由器/WiFi 的传感器节点，周期性把读数经 ESP-NOW 发给 USB 网关；
 *       网关回 ACK（含 RSSI）→ 节点做超时重传与统计，供稳定性测试使用。
 *
 *   节点 --DATA(seq)--> 网关          网关 --ACK(seq,rssi)--> 节点
 *   网关 --CMD--> 节点                节点 --ACK--> 网关（服务器测下行时延）
 *
 * 5.9 低功耗实验新增（2026-10-29）
 * --------------------------------
 *   目的：回答"ESP-NOW + light sleep 能否让功耗反超 MQTT"（见 report_mqtt_vs_espnow）。
 *   做法：
 *     - 板载 INA219 串在供电回路（0.1Ω），高频采样电流
 *     - **运行时**通过串口切换两种模式（不用重烧固件）：
 *         '0' → 常开（基线，射频持续开启）
 *         '1' → light sleep（发送后进入，定时唤醒）
 *       实测基线：ESP-NOW 常开 143.9 mA / WiFi+MQTT 113.3 mA（历史数据）
 *     - 输出 `[PWR]` 行供 PC 端解析统计
 *
 *   物理约束（写入论文"实验方法"）：
 *     - ESP32 进入 light sleep 会关闭 WiFi 射频 → ESP-NOW 断开，醒来必须重新初始化；
 *       这个**重初始化开销**正是要量化的成本。
 *     - INA219 由 3.3V 供电，light sleep 期间仍持续转换，故醒来后立即读可拿到
 *       "睡眠期间电流"的近似值（取多次最小值）。
 *     - Deep sleep 时 I2C 关闭、无法读 INA219 → 深睡电流只能用 datasheet 值理论估算。
 *
 * 统计口径（与本文件一致，论文可直接引用）：
 *   tx      已发送 DATA 帧次数（含重传）
 *   ack     收到匹配 ACK 次数
 *   retry   因超时触发的重传次数
 *   lost    一次发送周期内所有重传都失败（未收到 ACK）的次数
 *   丢包率  = lost / 发送周期数
 *
 * 协议见 shared/espnow_proto.h；默认发往广播地址（网关会回 ACK），
 * 若已知网关 MAC 可设置 GATEWAY_MAC_* 改为单播（链路层也会回 ACK，更省电）。
 */
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_sleep.h>
#include <Wire.h>
#include <BH1750.h>
#include <Adafruit_AHTX0.h>
#include "espnow_proto.h"

// ===================== 配置 =====================
#define ESPNOW_CHANNEL       6
#define SEND_INTERVAL_MS     5000        // 上报周期
#define ACK_TIMEOUT_MS       200         // 单次等待 ACK 超时
#define MAX_RETRY            3           // 超时后重传次数
#define I2C_SDA_PIN          21
#define I2C_SCL_PIN          22
#define NODE_LIGHT_SLEEP     0           // 编译期默认：0=常开（运行时可串口切换）
#define PWR_SAMPLE_MS        100         // INA219 采样周期（ms）

// 网关地址：默认广播；填网关 12 位 ID 可改为单播（例如 "A1B2C3D4E5F6"）
static const char *GATEWAY_ID = "FFFFFFFFFFFF";

// ===================== 状态 =====================
static uint32_t gSeq = 0;
static uint32_t gTx = 0, gAck = 0, gRetry = 0, gLost = 0, gCycles = 0;
static volatile bool gAckOk = false;
static volatile uint16_t gAckSeq = 0xFFFF;
static volatile int8_t gLastRssi = 0;
static bool bhOk = false, ahtOk = false;
static BH1750 lightMeter;
static Adafruit_AHTX0 aht;
static bool gUseBroadcast = true;
static uint8_t gGatewayMac[6];

// ---- 5.9 低功耗实验状态 ----
static bool gSleepMode = (NODE_LIGHT_SLEEP != 0);   // 运行时可通过串口切换
static uint32_t gSleepWakes = 0;                     // light sleep 唤醒次数
static uint32_t gSleepMsTotal = 0;                   // 累计睡眠毫秒
static float gSleepMaMin = -1;                       // 最近一次醒来读到的睡眠电流

// ===================== INA219（0.1Ω 采样，与主固件一致）=====================
#define INA219_ADDR        0x40
#define INA219_REG_CONFIG  0x00
#define INA219_REG_SHUNT   0x01
#define INA219_SHUNT_MOHM  100.0f     // 0.1Ω = 100mΩ
static bool inaOk = false;

static bool inaWrite16(uint8_t reg, uint16_t val) {
  Wire.beginTransmission(INA219_ADDR);
  Wire.write(reg);
  Wire.write((uint8_t)(val >> 8));
  Wire.write((uint8_t)(val & 0xFF));
  return Wire.endTransmission() == 0;
}

static bool inaRead16(uint8_t reg, int16_t *out) {
  Wire.beginTransmission(INA219_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)INA219_ADDR, 2) != 2) return false;
  uint8_t hi = Wire.read(), lo = Wire.read();
  *out = (int16_t)((hi << 8) | lo);
  return true;
}

static void inaInit() {
  Wire.beginTransmission(INA219_ADDR);
  if (Wire.endTransmission() != 0) {
    Serial.println("[INA219] 未检测到（功耗统计停用）");
    inaOk = false;
    return;
  }
  // 0x399F：32V 量程 / ±320mV / 12bit / 连续采样（睡眠期间也持续转换）
  inaOk = inaWrite16(INA219_REG_CONFIG, 0x399F);
  Serial.printf("[INA219] 初始化%s（0.1Ω 采样）\n", inaOk ? "OK" : "失败");
}

// 读取瞬时电流（mA）；无效返回 -1
// LSB = 10µV；I(mA) = Ushunt(µV) / R_shunt(mΩ) = raw*10 / 100 = raw/10
static float inaReadMa() {
  if (!inaOk) return -1.0f;
  int16_t raw;
  if (!inaRead16(INA219_REG_SHUNT, &raw)) return -1.0f;
  return (float)raw * 10.0f / INA219_SHUNT_MOHM;
}

// ===================== 工具 =====================
static void hexToMac(const char *hex, uint8_t mac[6]) {
  for (int i = 0; i < 6; i++) {
    char b[3] = {hex[i * 2], hex[i * 2 + 1], 0};
    mac[i] = (uint8_t)strtol(b, nullptr, 16);
  }
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  const uint8_t *mac = info->src_addr;
  int8_t rssi = info->rx_ctrl ? (int8_t)info->rx_ctrl->rssi : 0;
#else
static void onRecv(const uint8_t *mac, const uint8_t *data, int len) {
  int8_t rssi = 0;
#endif
  EwAirFrame f;
  if (!ew_air_parse(data, (size_t)len, &f)) return;

  if (f.type == EW_T_ACK && f.len >= 4) {
    uint16_t acked = (uint16_t)(f.payload[0] | (f.payload[1] << 8));
    if (acked == gAckSeq) {
      gAckOk = true;
      gLastRssi = (int8_t)f.payload[2];
    }
    return;
  }

  if (f.type == EW_T_CMD) {
    // 收到服务器下行命令：打印 + 回 ACK（服务器据此测下行时延/丢包）
    char text[201];
    size_t n = f.len < sizeof(text) - 1 ? f.len : sizeof(text) - 1;
    memcpy(text, f.payload, n);
    text[n] = '\0';
    Serial.printf("[NODE] 收到 CMD seq=%u: %s\n", (unsigned)f.seq, text);
    uint8_t ack[EW_AIR_MAX_FRAME];
    uint8_t ap[4] = {(uint8_t)(f.seq & 0xFF), (uint8_t)(f.seq >> 8), (uint8_t)rssi, 0x00};
    size_t an = ew_air_build(ack, EW_T_ACK, f.dev, f.seq, 0, ap, sizeof(ap));
    if (an) esp_now_send(f.dev, ack, an);
  }
}

static void onSend(const uint8_t *mac, esp_now_send_status_t status) {
  (void)mac; (void)status;   // 应用级 ACK 由 onRecv 判定，这里不重复计数
}

static void sensorsInit() {
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(100000);
  bhOk = lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE, 0x23, &Wire);
  ahtOk = aht.begin(&Wire);
  inaInit();                       // 5.9：功耗采样
  Serial.printf("[NODE] 传感器 BH1750=%s AHT20=%s INA219=%s\n",
                bhOk ? "OK" : "-", ahtOk ? "OK" : "-", inaOk ? "OK" : "-");
}

static void readSensors(float *lux, float *t, float *h) {
  *lux = bhOk ? lightMeter.readLightLevel() : -1.0f;
  if (ahtOk) {
    sensors_event_t te, he;
    if (aht.getEvent(&he, &te)) {
      *t = te.temperature;
      *h = he.relative_humidity;
    } else { *t = -1.0f; *h = -1.0f; }
  } else { *t = -1.0f; *h = -1.0f; }
  if (*lux < 0 || isnan(*lux)) *lux = -1.0f;
}

// 实际发送（自己的 MAC 作为 dev 字段；目标=网关）
static bool sendPayload(uint8_t type, uint16_t seq, const uint8_t *payload, uint16_t len, bool needAck) {
  uint8_t self[6];
  WiFi.macAddress(self);
  uint8_t frame[EW_AIR_MAX_FRAME];
  size_t n = ew_air_build(frame, type, self, seq, needAck ? 0x01 : 0x00, payload, len);
  if (n == 0) return false;
  gTx++;
  static uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  const uint8_t *dst = gUseBroadcast ? bcast : gGatewayMac;
  esp_err_t r = esp_now_send(dst, frame, n);
  return r == ESP_OK;
}

// ===================== ESP-NOW 重新初始化（light sleep 唤醒后必须做）=====================
// light sleep 会关闭 WiFi 射频，唤醒后 ESP-NOW 状态失效，必须重新 init + 注册 peer。
// 这段开销（约 100~300ms 高电流）正是 T9 要量化的"睡眠代价"。
static void espnowReinit() {
  esp_now_deinit();
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) {
    Serial.println("[NODE] ESP-NOW 重初始化失败");
    return;
  }
  esp_now_register_recv_cb(onRecv);
  esp_now_register_send_cb(onSend);

  esp_now_peer_info_t info = {};
  memset(info.peer_addr, 0xFF, 6);
  info.channel = ESPNOW_CHANNEL;
  info.encrypt = false;
  esp_now_add_peer(&info);
  if (!gUseBroadcast) {
    esp_now_peer_info_t gw = {};
    memcpy(gw.peer_addr, gGatewayMac, 6);
    gw.channel = ESPNOW_CHANNEL;
    gw.encrypt = false;
    esp_now_add_peer(&gw);
  }
}

// ===================== setup =====================
void setup() {
  Serial.begin(115200);
  delay(300);
  uint8_t self[6];
  WiFi.macAddress(self);
  char id[13];
  ew_mac_to_id(self, id);
  Serial.println();
  Serial.println("======================================");
  Serial.printf("ESP-NOW 无线节点 (5.4) + 低功耗实验 (5.9)  ID=%s  CH=%d\n",
                id, ESPNOW_CHANNEL);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) {
    Serial.println("[NODE] ESP-NOW 初始化失败，重启");
    delay(1000);
    ESP.restart();
  }
  esp_now_register_recv_cb(onRecv);
  esp_now_register_send_cb(onSend);

  hexToMac(GATEWAY_ID, gGatewayMac);
  uint8_t bcast[6];
  memset(bcast, 0xFF, 6);
  gUseBroadcast = (memcmp(gGatewayMac, bcast, 6) == 0);

  esp_now_peer_info_t info = {};
  memset(info.peer_addr, 0xFF, 6);
  info.channel = ESPNOW_CHANNEL;
  info.encrypt = false;
  esp_now_add_peer(&info);
  if (!gUseBroadcast) {
    esp_now_peer_info_t gw = {};
    memcpy(gw.peer_addr, gGatewayMac, 6);
    gw.channel = ESPNOW_CHANNEL;
    gw.encrypt = false;
    esp_now_add_peer(&gw);
  }
  Serial.printf("[NODE] 目标=%s 模式=%s\n", GATEWAY_ID, gUseBroadcast ? "广播" : "单播");

  sensorsInit();

  Serial.println("[PWR] 串口命令：'0'=常开模式  '1'=light sleep 模式  'p'=打印当前姿态");
  Serial.printf("[PWR] 当前模式=%s 采样周期=%dms 上报周期=%dms\n",
                gSleepMode ? "light sleep" : "常开", PWR_SAMPLE_MS, SEND_INTERVAL_MS);

  char hello[128];
  snprintf(hello, sizeof(hello), "{\"id\":\"%s\",\"fw\":\"5.9-lowpower\",\"note\":\"boot\"}", id);
  sendPayload(EW_T_HELLO, 0, (const uint8_t *)hello, strlen(hello), true);
  Serial.println("======================================");
}

// ===================== loop =====================
void loop() {
  unsigned long now = millis();

  // 1) 串口命令：运行时切换模式（避免反复烧录）
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '0') {
      gSleepMode = false;
      Serial.println("[PWR] 模式切换 → 常开（射频持续开启）");
    } else if (c == '1') {
      gSleepMode = true;
      Serial.println("[PWR] 模式切换 → light sleep（发送后休眠，定时唤醒）");
    } else if (c == 'p') {
      Serial.printf("[PWR] 姿态：mode=%s awake_ma=%.1f sleep_ma=%.1f wakes=%lu sleep_ms=%lu\n",
                    gSleepMode ? "sleep" : "awake", inaReadMa(), gSleepMaMin,
                    (unsigned long)gSleepWakes, (unsigned long)gSleepMsTotal);
    }
  }

  // 2) 到上报周期：读传感器 + 发送 + (可选)休眠
  static unsigned long lastSend = 0;
  if (now - lastSend < SEND_INTERVAL_MS) {
    delay(5);
    return;
  }
  lastSend = now;

  float lux, t, h;
  readSensors(&lux, &t, &h);

  char self[13];
  uint8_t mac[6];
  WiFi.macAddress(mac);
  ew_mac_to_id(mac, self);

  // 5.9：发送前读一次工作电流
  float maAwake = inaReadMa();

  char json[256];
  snprintf(json, sizeof(json),
           "{\"id\":\"%s\",\"lux\":%.1f,\"temperature\":%.2f,\"humidity\":%.2f,"
           "\"current_ma\":%.1f,\"mode\":\"%s\","
           "\"tx\":%lu,\"ack\":%lu,\"retry\":%lu,\"lost\":%lu}",
           self, lux, t, h, maAwake, gSleepMode ? "sleep" : "awake",
           (unsigned long)gTx, (unsigned long)gAck,
           (unsigned long)gRetry, (unsigned long)gLost);

  unsigned long tSend0 = millis();
  gCycles++;
  bool ok = false;
  for (int attempt = 0; attempt <= MAX_RETRY && !ok; attempt++) {
    if (attempt > 0) gRetry++;
    gAckOk = false;
    gAckSeq = (uint16_t)(gSeq & 0xFFFF);
    sendPayload(EW_T_DATA, (uint16_t)gSeq, (const uint8_t *)json, strlen(json), true);

    unsigned long t0 = millis();
    while (millis() - t0 < ACK_TIMEOUT_MS && !gAckOk) delay(1);
    if (gAckOk) ok = true;
  }
  if (ok) gAck++; else gLost++;
  gSeq++;
  unsigned long sendMs = millis() - tSend0;

  Serial.printf("[NODE] seq=%lu %s lux=%.1f T=%.2f H=%.2f | tx=%lu ack=%lu retry=%lu lost=%lu rssi=%d\n",
                (unsigned long)(gSeq - 1), ok ? "ACK " : "LOST",
                lux, t, h, (unsigned long)gTx, (unsigned long)gAck,
                (unsigned long)gRetry, (unsigned long)gLost, (int)gLastRssi);
  Serial.printf("[PWR] awake_ma=%.1f send_ms=%lu mode=%s\n",
                maAwake, sendMs, gSleepMode ? "sleep" : "awake");

#if NODE_LIGHT_SLEEP
  // 编译期强制休眠（保留旧的复现路径）
#endif

  // 5.9：运行时 light sleep
  if (gSleepMode) {
    // 剩余睡眠时间 = 上报周期 - 本轮已耗时（发送+打印）
    unsigned long elapsed = millis() - now;
    unsigned long sleepMs = (elapsed < SEND_INTERVAL_MS) ? (SEND_INTERVAL_MS - elapsed) : 100;
    if (sleepMs < 50) sleepMs = 50;

    esp_sleep_enable_timer_wakeup((uint64_t)sleepMs * 1000ULL);
    unsigned long tSleep0 = millis();
    esp_light_sleep_start();                    // ← 阻塞到定时唤醒
    unsigned long slept = millis() - tSleep0;

    // 醒来第一件事：多次读 INA219 取最小值 ≈ 睡眠期间电流
    // （INA219 连续采样，睡眠期间持续转换；醒来后 CPU 电流会迅速升高，故取最小值）
    float best = 999.0f;
    for (int i = 0; i < 5; i++) {
      float m = inaReadMa();
      if (m >= 0 && m < best) best = m;
    }
    gSleepMaMin = (best >= 999.0f) ? -1.0f : best;
    gSleepWakes++;
    gSleepMsTotal += slept;

    // light sleep 关闭了 WiFi 射频 → 必须重新初始化 ESP-NOW
    unsigned long tRinit0 = millis();
    espnowReinit();
    unsigned long reinitMs = millis() - tRinit0;

    Serial.printf("[PWR] sleep_ms=%lu sleep_ma=%.1f reinit_ms=%lu wakes=%lu\n",
                  slept, gSleepMaMin, reinitMs, (unsigned long)gSleepWakes);
  }
}

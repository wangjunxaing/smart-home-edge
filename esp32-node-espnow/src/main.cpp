/*
 * ESP-NOW 无线节点固件（阶段二 5.4）
 * ==================================
 * 定位：不连路由器/WiFi 的传感器节点，周期性把读数经 ESP-NOW 发给 USB 网关；
 *       网关回 ACK（含 RSSI）→ 节点做超时重传与统计，供稳定性测试使用。
 *
 *   节点 --DATA(seq)--> 网关          网关 --ACK(seq,rssi)--> 节点
 *   网关 --CMD--> 节点                节点 --ACK--> 网关（服务器测下行时延）
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
#define NODE_LIGHT_SLEEP     0           // 1=发送后进入 light sleep（低功耗实验用）

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
  Serial.printf("[NODE] 传感器 BH1750=%s AHT20=%s\n", bhOk ? "OK" : "-", ahtOk ? "OK" : "-");
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

void setup() {
  Serial.begin(115200);
  delay(300);
  uint8_t self[6];
  WiFi.macAddress(self);
  char id[13];
  ew_mac_to_id(self, id);
  Serial.println();
  Serial.println("======================================");
  Serial.printf("ESP-NOW 无线节点 (5.4)  ID=%s  CH=%d\n", id, ESPNOW_CHANNEL);

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

  // 广播 peer 始终注册（ESP-NOW 发送广播必须有 peer 条目）；单播模式再加网关 peer
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

  char hello[128];
  snprintf(hello, sizeof(hello), "{\"id\":\"%s\",\"fw\":\"5.4-node\",\"note\":\"boot\"}", id);
  sendPayload(EW_T_HELLO, 0, (const uint8_t *)hello, strlen(hello), true);
  Serial.println("======================================");
}

void loop() {
  static unsigned long lastSend = 0;
  unsigned long now = millis();
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

  char json[256];
  snprintf(json, sizeof(json),
           "{\"id\":\"%s\",\"lux\":%.1f,\"temperature\":%.2f,\"humidity\":%.2f,"
           "\"tx\":%lu,\"ack\":%lu,\"retry\":%lu,\"lost\":%lu}",
           self, lux, t, h, (unsigned long)gTx, (unsigned long)gAck,
           (unsigned long)gRetry, (unsigned long)gLost);

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

  Serial.printf("[NODE] seq=%lu %s lux=%.1f T=%.2f H=%.2f | tx=%lu ack=%lu retry=%lu lost=%lu rssi=%d\n",
                (unsigned long)(gSeq - 1), ok ? "ACK " : "LOST",
                lux, t, h, (unsigned long)gTx, (unsigned long)gAck,
                (unsigned long)gRetry, (unsigned long)gLost, (int)gLastRssi);

#if NODE_LIGHT_SLEEP
  esp_sleep_enable_timer_wakeup(SEND_INTERVAL_MS * 1000ULL);
  esp_light_sleep_start();
#endif
}

/*
 * ESP-NOW 节点客户端实现（5.4）
 * ============================
 * 依赖 WiFi（仅用到射频，不连接路由器）与 esp_now。
 */
#include "espnow_client.h"
#include "wifi_manager.h"
#include "net_client.h"          // applyDownlinkCmd()
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <ArduinoJson.h>
#include <esp_timer.h>
#include "espnow_proto.h"

#define ESPNOW_CHANNEL      6
#define ACK_TIMEOUT_MS      200
#define MAX_RETRY           3

static bool inited = false;
static bool useBroadcast = true;
static uint8_t gwMac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static uint8_t selfMac[6] = {0};      // 启动时缓存：回调里调 WiFi.macAddress() 会争 WiFi 锁
static volatile bool ackOk = false;
static volatile uint16_t pendingSeq = 0xFFFF;
static volatile int8_t lastRssi = 0;
static uint32_t txCount = 0, ackCount = 0, lostCount = 0, retryCount = 0;

#if ESP_ARDUINO_VERSION_MAJOR >= 3
static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  const uint8_t *mac = info->src_addr;
  int8_t rssi = info->rx_ctrl ? (int8_t)info->rx_ctrl->rssi : 0;
#else
static void onRecv(const uint8_t *mac, const uint8_t *data, int len) {
  int8_t rssi = 0;
#endif
  (void)mac;
  EwAirFrame f;
  if (!ew_air_parse(data, (size_t)len, &f)) return;

  if (f.type == EW_T_ACK && f.len >= 4) {
    uint16_t acked = (uint16_t)(f.payload[0] | (f.payload[1] << 8));
    if (acked == pendingSeq) {
      ackOk = true;
      lastRssi = (int8_t)f.payload[2];
    }
    return;
  }

  if (f.type == EW_T_CMD) {
    // 复用与 MQTT 相同的下行分发（light / ir / tts / ir_learn / config …）
    char buf[EW_AIR_MAX_PAYLOAD + 1];
    size_t n = f.len < EW_AIR_MAX_PAYLOAD ? f.len : EW_AIR_MAX_PAYLOAD;
    memcpy(buf, f.payload, n);
    buf[n] = '\0';
    uint32_t ackSeq = applyDownlinkCmd(buf, n);
    // ACK 帧头 dev 必须是「节点自己」（网关据此识别谁回的），发送目标才是网关：
    // 网关下发的 CMD 帧 dev 字段填的是目标节点，若照抄会导致 ACK 发给自己。
    // 注意：回调里不能调 WiFi.macAddress()（会争 WiFi 锁，实测引入秒级长尾），用启动缓存。
    uint8_t ack[EW_AIR_MAX_FRAME];
    uint8_t ap[4] = {(uint8_t)(f.seq & 0xFF), (uint8_t)(f.seq >> 8), (uint8_t)rssi, 0x00};
    size_t an = ew_air_build(ack, EW_T_ACK, selfMac, f.seq, 0, ap, sizeof(ap));
    static uint8_t bcast6[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    if (an) esp_now_send(useBroadcast ? bcast6 : gwMac, ack, an);
    (void)ackSeq;
  }
}

static void onSend(const uint8_t *mac, esp_now_send_status_t status) {
  (void)mac; (void)status;   // 应用级 ACK 由 onRecv 判定
}

void espnowInit() {
  if (inited) return;
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();                       // 不连接路由器
  WiFi.setSleep(false);                    // 关 modem sleep：ESP-NOW 低时延的关键（实测 p50 189ms→~20ms）
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) {
    Serial.println("[ESPNOW] 初始化失败");
    return;
  }
  esp_now_register_recv_cb(onRecv);
  esp_now_register_send_cb(onSend);
  WiFi.macAddress(selfMac);        // 缓存自身 MAC（回调里不再调用 WiFi API）
  esp_now_peer_info_t b = {};
  memset(b.peer_addr, 0xFF, 6);
  b.channel = ESPNOW_CHANNEL;
  b.encrypt = false;
  esp_now_add_peer(&b);
  inited = true;
  Serial.printf("[ESPNOW] 就绪（CH=%d，目标=%s）\n", ESPNOW_CHANNEL,
                useBroadcast ? "广播" : "单播");
}

void espnowDeinit() {
  if (!inited) return;
  esp_now_deinit();
  inited = false;
  Serial.println("[ESPNOW] 已关闭");
}

bool espnowReady() { return inited; }

void espnowSetGatewayId(const String &id12) {
  if (id12.length() != 12) return;
  bool bcast = true;
  for (int i = 0; i < 6; i++) {
    char t[3] = {id12[i * 2], id12[i * 2 + 1], 0};
    gwMac[i] = (uint8_t)strtol(t, nullptr, 16);
    if (gwMac[i] != 0xFF) bcast = false;
  }
  useBroadcast = bcast;
  if (inited) {
    esp_now_peer_info_t p = {};
    memcpy(p.peer_addr, gwMac, 6);
    p.channel = ESPNOW_CHANNEL;
    p.encrypt = false;
    esp_now_add_peer(&p);      // 已存在时返回错误，忽略
    Serial.printf("[ESPNOW] 目标改为 %s（%s）\n", id12.c_str(), bcast ? "广播" : "单播");
  }
}

void espnowLoop() { /* 收发均异步完成；预留 */ }

bool espnowPublishSensor(const SensorReading &r) {
  if (!inited) return false;
  StaticJsonDocument<512> doc;
  doc["id"] = ESP32_ID;
  if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
    doc["location"] = deviceLocation;
    xSemaphoreGive(configMutex);
  }
  if (r.lux >= 0) doc["lux"] = r.lux;
  if (r.temperature >= 0) doc["temperature"] = r.temperature;
  if (r.humidity >= 0) doc["humidity"] = r.humidity;
  if (r.current_ma >= 0) doc["current_ma"] = r.current_ma;
  doc["rssi"] = r.rssi;
  doc["t0_ms"] = r.t0_ms;
  // T5 载荷扫描：按服务器下发的 payload_pad(0~80) 追加 pad 字段放大上行帧
  int pad = 0;
  if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
    pad = payloadPad;
    xSemaphoreGive(configMutex);
  }
  if (pad > 0) {
    char filler[96];
    if (pad > (int)sizeof(filler) - 1) pad = (int)sizeof(filler) - 1;
    memset(filler, 'x', pad);
    filler[pad] = '\0';
    doc["pad"] = filler;
  }
  String payload;
  serializeJson(doc, payload);

  uint8_t self[6];
  memcpy(self, selfMac, 6);       // 用缓存（每 5s 上报都调 WiFi.macAddress 会拖慢链路）
  static uint16_t tSeq = 0;
  uint16_t seq = ++tSeq;          // 递增序号：网关据此统计上行丢包（缺口）
  bool ok = false;
  for (int attempt = 0; attempt <= MAX_RETRY && !ok; attempt++) {
    if (attempt > 0) retryCount++;
    uint8_t frame[EW_AIR_MAX_FRAME];
    size_t n = ew_air_build(frame, EW_T_DATA, self, seq, 0x01,
                            (const uint8_t *)payload.c_str(), (uint16_t)payload.length());
    if (n == 0) return false;
    ackOk = false;
    pendingSeq = seq;
    txCount++;
    static uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    esp_err_t e = esp_now_send(useBroadcast ? bcast : gwMac, frame, n);
    if (e != ESP_OK) continue;
    unsigned long t0 = millis();
    while (millis() - t0 < ACK_TIMEOUT_MS && !ackOk) delay(1);
    if (ackOk) ok = true;
  }
  if (ok) ackCount++; else lostCount++;
  return ok;
}

void espnowGetStats(uint32_t *tx, uint32_t *ack, uint32_t *lost, uint32_t *retry) {
  if (tx) *tx = txCount;
  if (ack) *ack = ackCount;
  if (lost) *lost = lostCount;
  if (retry) *retry = retryCount;
}

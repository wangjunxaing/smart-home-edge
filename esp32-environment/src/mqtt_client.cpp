/*
 * MQTT 传输层实现（阶段二 5.1）
 * ============================
 * 依赖 PubSubClient（knolleary 经典库）。语义：
 *   - 下行 cmd 订阅 QoS1 + 遗嘱 LWT QoS1（异常掉线 broker 代发 "offline"）
 *   - 上报 publish 为 QoS0（经典库限制；断线重连 + 队列缓冲 + LWT 兜底）
 *   - keepalive 60s（PubSubClient.loop() 内部自动 PING）
 *   - 断线 3s 退避重连；broker 地址每次重连前经 mDNS 刷新
 *
 * 下行命令 JSON（来自服务器 mqtt_bridge.publish_cmd）:
 *   {"type":"light", "seq":N, "on":.., "r":.., "g":.., "b":..,
 *    "brightness":.., "led_count":..}
 *   {"type":"ir",     "seq":N, "data":0x....}
 *   {"type":"ir_learn","learn":true|false}
 * 收到后交 net_client.applyMqttCmd() 解析入队，成功则回发 ack。
 */
#include "mqtt_client.h"
#include "app_config.h"
#include "wifi_manager.h"
#include "net_client.h"
#include "mmwave.h"      // 5.8 毫米波链路统计（远程诊断）
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <esp_timer.h>

// ===================== 模块内部状态 =====================
static WiFiClient mqttWifiClient;
static PubSubClient mqtt(mqttWifiClient);
static bool mqttInited = false;
static unsigned long lastConnAttempt = 0;

// ===================== 话题构造 =====================
static String tSensors() { return "/esp32/" + ESP32_ID + "/sensors"; }
static String tCmd()     { return "/esp32/" + ESP32_ID + "/cmd"; }
static String tAck()     { return "/esp32/" + ESP32_ID + "/ack"; }
static String tLwt()     { return "/esp32/" + ESP32_ID + "/lwt"; }

// ===================== 订阅回调（cmd 下行） =====================
static void mqttOnCmd(char *topic, byte *payload, unsigned int length) {
  if (String(topic) != tCmd()) return;

  // 拷贝并补 '\0'（MQTT payload 不保证以 0 结尾）
  static char buf[512];
  if (length >= sizeof(buf)) length = sizeof(buf) - 1;
  memcpy(buf, payload, length);
  buf[length] = '\0';

  uint32_t ackSeq = applyDownlinkCmd(buf, length);
  if (ackSeq > 0) {
    if (!mqttPublishAck(ackSeq))
      Serial.printf("[MQTT] ack 发送失败 seq=%lu\n", (unsigned long)ackSeq);
  }
}

// ===================== 初始化 =====================
void mqttInit() {
  if (mqttInited) return;
  mqttInited = true;
  mqtt.setServer(wifiServerIP(), 1883);
  mqtt.setCallback(mqttOnCmd);
  mqtt.setKeepAlive(60);
  mqtt.setBufferSize(512);
  mqtt.setSocketTimeout(8);
  lastConnAttempt = 0;
  Serial.printf("[MQTT] 初始化完成（broker %s:1883）\n",
                wifiServerIP().toString().c_str());
}

// ===================== 连接维护 =====================
void mqttLoop() {
  if (!mqttInited) mqttInit();
  if (WiFi.status() != WL_CONNECTED) return;   // 断网由 wifiCheck 处理

  if (!mqtt.connected()) {
    unsigned long now = millis();
    if (now - lastConnAttempt < 3000) return;  // 3s 退避

    lastConnAttempt = now;
    mqtt.setServer(wifiServerIP(), 1883);      // 地址可能变化，重连前刷新

    // connect(id, willTopic, willQos, willRetain, willMessage)：遗嘱随 CONNECT 携带
    if (mqtt.connect(ESP32_ID.c_str(), tLwt().c_str(), 1, true, "offline")) {
      Serial.println("[MQTT] 已连接 broker");
      if (mqtt.subscribe(tCmd().c_str(), 1))
        Serial.printf("[MQTT] 已订阅 %s\n", tCmd().c_str());
      else
        Serial.println("[MQTT] 订阅 cmd 失败");
    } else {
      Serial.printf("[MQTT] 连接失败 rc=%d，3s 后重试\n", mqtt.state());
    }
    return;
  }

  mqtt.loop();   // 保活 + 收包（回调在 net_task 上下文执行）
}

bool mqttConnected() {
  return mqtt.connected();
}

void mqttDisconnect() {
  if (mqtt.connected()) {
    mqtt.disconnect();   // 正常 DISCONNECT，broker 不发遗嘱
    Serial.println("[MQTT] 已主动断开（切回 HTTP）");
  }
  mqttInited = false;    // 下次进入 MQTT 模式重新初始化
}

// ===================== 传感器上报（同构 HTTP JSON + t0_ms） =====================
bool mqttPublishSensor(const SensorReading &r) {
  if (!mqtt.connected()) return false;

  StaticJsonDocument<560> doc;      // 384→560：+LD2450 坐标(mm_x/y/speed)/mm_ver/mm_hex
  doc["id"] = ESP32_ID;
  if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
    doc["location"] = deviceLocation;
    xSemaphoreGive(configMutex);
  }
  if (r.lux >= 0) doc["lux"] = r.lux;
  if (r.temperature >= 0) doc["temperature"] = r.temperature;
  if (r.humidity >= 0) doc["humidity"] = r.humidity;
  if (r.current_ma >= 0) doc["current_ma"] = r.current_ma;   // INA219 功耗
  doc["rssi"] = r.rssi;
  doc["t0_ms"] = r.t0_ms;          // 采集完成打点（论文时延测量）

  // 5.8 毫米波：仅在收到有效帧时上报（未接模块的节点不带这些字段）
  if (r.person >= 0) {
    doc["person"] = r.person;                       // 1=有人 0=无人
    doc["mm_state"] = r.mm_target_state;            // 0无人/1运动/2静止/3运动+静止
    if (r.mm_moving_dist >= 0) doc["mm_move_dist"] = r.mm_moving_dist;
    if (r.mm_still_dist >= 0) doc["mm_still_dist"] = r.mm_still_dist;
    if (r.mm_detect_dist >= 0) doc["mm_detect_dist"] = r.mm_detect_dist;
    doc["mm_move_energy"] = r.mm_moving_energy;
    doc["mm_still_energy"] = r.mm_still_energy;
    doc["mm_proto"] = r.mm_proto;                   // 1=LD2410C 2=LD2450
    // LD2450：平面坐标（2D）
    if (r.mm_proto == 2) {
      doc["mm_x"] = r.mm_x_mm;                      // mm，正=右
      doc["mm_y"] = r.mm_y_mm;                      // mm，正=前方
      doc["mm_speed"] = r.mm_speed_cms;             // cm/s，正=靠近
    }
  }
#if MMWAVE_ENABLED
  // 毫米波链路统计：远程判断"没数据(rx=0)"还是"解析失败(rx>0,frames=0)"
  MmwaveStats mms = mmwaveStats();
  doc["mm_rx"] = mms.rx_bytes;
  doc["mm_frames"] = mms.frames_ok;
  doc["mm_bad"] = mms.frames_bad;
  doc["mm_baud"] = mms.baud;
  doc["mm_tries"] = mms.baud_tries;
  doc["mm_ver"] = mms.protocol;                     // 固件识别出的协议版本
  // 原始字节（前 32B hex）：万一对不上协议，远程一眼看出模块吐的是什么
  if (r.person < 0) {
    char hex[68];
    if (mmwaveGetRawHex(hex, sizeof(hex)) > 0) doc["mm_hex"] = hex;
  }
#endif

  String payload;
  serializeJson(doc, payload);
  // QoS0 发布（knolleary 经典库 publish 仅支持 QoS0；订阅/遗嘱为 QoS1）
  bool ok = mqtt.publish(tSensors().c_str(), payload.c_str());
  if (ok)
    Serial.printf("[MQTT] publish sensors ok\n");
  else
    Serial.printf("[MQTT] publish sensors 失败\n");
  return ok;
}

// ===================== 控制回执 =====================
bool mqttPublishAck(uint32_t seq) {
  if (!mqtt.connected()) return false;
  StaticJsonDocument<96> doc;
  doc["seq"] = seq;
  doc["t_ms"] = (uint64_t)(esp_timer_get_time() / 1000);
  String payload;
  serializeJson(doc, payload);
  bool ok = mqtt.publish(tAck().c_str(), payload.c_str());
  if (ok)
    Serial.printf("[MQTT] ack seq=%lu\n", (unsigned long)seq);
  return ok;
}

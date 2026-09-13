/*
 * 网络通信模块（v1.9.0：MQTT / ESP-NOW 双传输，去掉 HTTP 数据通道）
 * ================================================================
 * 传输模式（服务器 config 下发，≤30s 生效）：
 *   TRANSPORT_MQTT   —— 连路由器，传感器 publish、命令订阅（QoS1 遗嘱）
 *   TRANSPORT_ESPNOW —— 不连路由器，传感器经 ESP-NOW 发 USB 网关；命令经网关下发
 *
 * HTTP 仅保留为"控制面"（不再是数据通道）：
 *   - 配置拉取 / OTA 版本检查：MQTT 模式每 30s；ESP-NOW 模式每 10min 开一次
 *     "WiFi 维护窗口"（连路由 → 拉配置 + 查 OTA → 断开 → 恢复 ESP-NOW 信道）
 *   - OTA 固件下载在维护窗口内走 HTTP（快），由控制任务执行
 *
 * 下行命令统一入口 applyDownlinkCmd()：MQTT 回调与 ESP-NOW 回调共用，
 * 支持 light / ir / tts / ir_learn / config（含 transport 切换、gw_id）。
 */
#include "net_client.h"
#include "app_config.h"
#include "wifi_manager.h"
#include "mqtt_client.h"
#include "espnow_client.h"
#include "ota_update.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <esp_task_wdt.h>

// ===================== 模块内部状态 =====================
static WiFiClient wifiClient;                                // 控制面 HTTP 用
static const unsigned long WIFI_CHECK_INTERVAL = 10000;      // MQTT 模式 WiFi 监控
static const unsigned long CONFIG_FETCH_INTERVAL = 30000;    // MQTT 模式配置/OTA 周期
static const unsigned long ESPNOW_MAINT_INTERVAL = 600000;   // ESP-NOW 维护窗口周期（10min）
static const unsigned long ESPNOW_OTA_WAIT_MS = 150000;      // 有 OTA 时的窗口延长上限（下载~1MB 需数十秒）
static uint32_t lastLightSeq = 0xFFFFFFFF;   // 已应用的灯带序号（跨传输去重）
static uint32_t lastIrSeq = 0xFFFFFFFF;      // 已执行的红外序号
static uint32_t lastTtsSeq = 0xFFFFFFFF;     // 已播报的 TTS 序号
static bool gOtaPending = false;             // 维护窗口内已发现新固件（需延长窗口等 OTA）

// ===================== 网络自适应（MQTT 主 → 断网自动降级 ESP-NOW → 恢复自动切回） ======
// 策略：MQTT 模式下每 10s 检查一次连通性，连续 60s 不可用则自动降级到 ESP-NOW（不依赖路由器）；
//       降级期间每 2 分钟开一次"探测窗口"连路由器 + HTTP 探测，连续 3 次成功（≈6 分钟稳定连通）
//       才切回 MQTT，避免网络抖动导致来回切换。服务器配置里的 transport=mqtt 在自动降级期间
//       会被忽略（否则维护窗口拉配置会立刻把设备拉回不通的网络）。
static const unsigned long NET_FAIL_INTERVAL = WIFI_CHECK_INTERVAL;   // 失败判定节拍 10s
static const int NET_FAIL_LIMIT = 6;                                  // 6×10s = 60s 判定断网
static const unsigned long NET_PROBE_INTERVAL = 120000;               // 降级期间探测间隔 2min
static const int NET_PROBE_OK_LIMIT = 3;                              // 连续 3 次成功才切回
static bool autoDowngraded = false;          // 是否因断网自动降级（区别于服务器显式配置）
static int netFailCount = 0;                 // MQTT 模式连续失败次数
static int probeOkCount = 0;                 // 降级期间连续探测成功次数
static unsigned long lastNetProbe = 0;       // 上次探测时刻

// ===================== 传输模式读写/切换 =====================
static TransportMode currentMode() {
  TransportMode m = TRANSPORT_MQTT;
  if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
    m = transportMode;
    xSemaphoreGive(configMutex);
  }
  return m;
}

static void setMode(TransportMode m) {
  if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
    transportMode = m;
    xSemaphoreGive(configMutex);
  }
}

// 应用传输模式（含射频切换）
static void applyTransportMode(TransportMode want) {
  if (want == currentMode()) return;
  setMode(want);
  if (want == TRANSPORT_ESPNOW) {
    Serial.println("[传输] → ESP-NOW（断开 WiFi，仅保留射频）");
    WiFi.disconnect(true);
    vTaskDelay(pdMS_TO_TICKS(200));
    espnowInit();
  } else {
    Serial.println("[传输] → MQTT（连路由器）");
    espnowDeinit();
    wifiConnect();
    if (wifiConnected()) resolveServerHost();
  }
}

// ===================== 控制面：配置拉取（HTTP） =====================
static void fetchConfig() {
  HTTPClient http;
  http.begin(wifiClient, makeURL(("/api/esp32/config?device_id=" + ESP32_ID).c_str()));
  http.setTimeout(3000);
  int httpCode = http.GET();
  if (httpCode == 200) {
    DynamicJsonDocument doc(384);
    if (!deserializeJson(doc, http.getString())) {
      if (doc.containsKey("send_interval")) {
        unsigned long v = doc["send_interval"].as<unsigned long>();
        if (v >= 1000 && v <= 60000) {
          if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
            if (SEND_INTERVAL != v) { SEND_INTERVAL = v; Serial.printf("[配置] 间隔→%lu ms\n", v); }
            xSemaphoreGive(configMutex);
          }
        }
      }
      if (doc.containsKey("location")) {
        String loc = doc["location"].as<String>();
        if (loc.length() > 0) {
          if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
            if (deviceLocation != loc) { deviceLocation = loc; Serial.printf("[配置] 位置→%s\n", loc.c_str()); }
            xSemaphoreGive(configMutex);
          }
        }
      }
      if (doc.containsKey("gw_id")) espnowSetGatewayId(doc["gw_id"].as<String>());
      if (doc.containsKey("payload_pad")) {
        int v = doc["payload_pad"].as<int>();
        v = v < 0 ? 0 : (v > 80 ? 80 : v);
        if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
          if (payloadPad != v) { payloadPad = v; Serial.printf("[配置] 载荷填充→%dB\n", v); }
          xSemaphoreGive(configMutex);
        }
      }
      if (doc.containsKey("transport")) {
        String t = doc["transport"].as<String>();
        if (t == "espnow") {
          autoDowngraded = false;                 // 服务器显式要求 ESP-NOW：清除自动降级状态
          applyTransportMode(TRANSPORT_ESPNOW);
        } else if (t == "mqtt" && !autoDowngraded) {
          applyTransportMode(TRANSPORT_MQTT);     // 自动降级期间忽略（等探测确认稳定恢复）
        } else if (t == "mqtt") {
          Serial.println("[网络] 配置要求 MQTT，但当前处于自动降级状态，等待探测确认");
        }
      }
    }
  }
  http.end();
}

// ===================== 控制面：OTA 检查（HTTP） =====================
static void checkFirmware() {
  if (!otaShouldAttempt()) return;
  HTTPClient http;
  String url = makeURL(("/api/esp32/firmware/check?device_id=" + ESP32_ID +
                        "&current=" + String(FW_VERSION)).c_str());
  http.begin(wifiClient, url);
  http.setTimeout(3000);
  if (http.GET() == 200) {
    DynamicJsonDocument doc(384);
    if (!deserializeJson(doc, http.getString())) {
      if (doc["update"].as<bool>()) {
        ControlCommand cmd;
        cmd.type = CTRL_OTA_UPDATE;
        strlcpy(cmd.path, doc["path"] | "/firmware/file", sizeof(cmd.path));
        strlcpy(cmd.version, doc["version"] | "?", sizeof(cmd.version));
        if (xQueueSend(controlQueue, &cmd, 0) == pdTRUE) {
          gOtaPending = true;
          Serial.printf("[OTA] 发现 v%s，已通知控制任务\n", cmd.version);
        }
      }
    }
  }
  http.end();
}

// ===================== 网络恢复探测（自动降级期间使用） =====================
// 临时连路由器 + HTTP 探测服务器；探测完恢复 ESP-NOW（与维护窗口同样的开关机制）。
// 注意：探测期间 ESP-NOW 上行会中断约 5~8s，因此间隔取 2 分钟以控制影响。
static bool probeNetwork() {
  Serial.println("[网络] 探测窗口：尝试连接路由器...");
  espnowDeinit();
  WiFi.mode(WIFI_STA);
  wifiConnect();
  bool ok = false;
  if (wifiConnected() && resolveServerHost()) {
    HTTPClient http;
    http.begin(wifiClient, makeURL(("/api/esp32/config?device_id=" + ESP32_ID).c_str()));
    http.setTimeout(3000);
    ok = (http.GET() == 200);
    http.end();
  }
  WiFi.disconnect(true);
  vTaskDelay(pdMS_TO_TICKS(200));
  espnowInit();
  Serial.printf("[网络] 探测结果：%s\n", ok ? "可达" : "不可达");
  return ok;
}

// ===================== ESP-NOW 维护窗口 =====================
// 断开 ESP-NOW → 连路由 → 拉配置/查 OTA → 等控制任务处理 → 断开 → 恢复 ESP-NOW
static void maintenanceWindow() {
  Serial.println("[维护] 打开 WiFi 窗口（配置/OTA）...");
  espnowDeinit();
  WiFi.mode(WIFI_STA);
  wifiConnect();
  if (wifiConnected() && resolveServerHost()) {
    // ① 先查 OTA：此刻 WiFi 是直连的。若先 fetchConfig()，重启后的首次窗口会因
    //    applyTransportMode(ESPNOW) 执行 WiFi.disconnect(true) 而让 OTA 检查发不出去。
    checkFirmware();
    if (!gOtaPending) {
      // ② 只有本次不升级时才拉配置（可能切换传输模式并断开 WiFi）
      fetchConfig();
      if (currentMode() == TRANSPORT_MQTT) return;   // 配置要求切回 MQTT：直接返回（已连 WiFi）
    } else {
      Serial.println("[维护] 已有 OTA 任务，本次跳过配置拉取（下次窗口再拉）");
    }
    // 有更新时延长窗口等 OTA 完成（~1MB 需数十秒；成功后设备重启，不会走到关窗）；
    // 无更新/冷却中则几秒内关窗，尽量减少 ESP-NOW 上行中断。
    unsigned long waitMs = gOtaPending ? ESPNOW_OTA_WAIT_MS : 4000;
    unsigned long t0 = millis();
    while (millis() - t0 < waitMs) {               // 留时间给控制任务执行 OTA
      vTaskDelay(pdMS_TO_TICKS(200));
      esp_task_wdt_reset();
    }
    if (gOtaPending) {
      Serial.println("[维护] OTA 等待超时（未完成），关闭窗口");
      gOtaPending = false;
    }
  }
  WiFi.disconnect(true);
  vTaskDelay(pdMS_TO_TICKS(200));
  espnowInit();
  Serial.println("[维护] 窗口关闭，恢复 ESP-NOW");
}

// ===================== 下行命令统一分发 =====================
// MQTT 回调（mqtt_client）与 ESP-NOW 回调（espnow_client）共用。
// 返回需回执的 seq（0=无需回执/已去重/队列满）。
uint32_t applyDownlinkCmd(const char *json, size_t len) {
  DynamicJsonDocument doc(512);
  if (deserializeJson(doc, json, len)) {
    Serial.println("[下行] JSON 解析失败");
    return 0;
  }
  String type = doc["type"] | "";
  uint32_t seq = doc["seq"] | 0;

  if (type == "config") {          // 配置下发（走数据通道，不再等 HTTP）
    if (doc.containsKey("send_interval")) {
      unsigned long v = doc["send_interval"].as<unsigned long>();
      if (v >= 1000 && v <= 60000) {
        if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
          SEND_INTERVAL = v; xSemaphoreGive(configMutex);
        }
        Serial.printf("[配置] 下行间隔→%lu ms\n", v);
      }
    }
    if (doc.containsKey("location")) {
      String loc = doc["location"].as<String>();
      if (loc.length() > 0) {
        if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
          deviceLocation = loc; xSemaphoreGive(configMutex);
        }
        Serial.printf("[配置] 下行位置→%s\n", loc.c_str());
      }
    }
    if (doc.containsKey("gw_id")) espnowSetGatewayId(doc["gw_id"].as<String>());
    if (doc.containsKey("payload_pad")) {          // T5 载荷扫描：立即生效
      int v = doc["payload_pad"].as<int>();
      v = v < 0 ? 0 : (v > 80 ? 80 : v);
      if (xSemaphoreTake(configMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
        if (payloadPad != v) { payloadPad = v; Serial.printf("[配置] 载荷填充→%dB\n", v); }
        xSemaphoreGive(configMutex);
      }
    }
    if (doc.containsKey("transport")) {
      String t = doc["transport"].as<String>();
      if (t == "espnow") applyTransportMode(TRANSPORT_ESPNOW);
      else if (t == "mqtt") applyTransportMode(TRANSPORT_MQTT);
    }
    return 0;
  }

  if (type == "light") {
    if (seq == 0 || seq == lastLightSeq) return 0;
    ControlCommand cmd;
    cmd.type = CTRL_LIGHT_SET; cmd.seq = seq;
    cmd.on = doc["on"].as<bool>() ? 1 : 0;
    cmd.r = doc["r"] | 255; cmd.g = doc["g"] | 255; cmd.b = doc["b"] | 255;
    cmd.brightness = doc["brightness"] | 128;
    cmd.led_count = (uint8_t)((doc["led_count"] | LIGHT_DEFAULT_COUNT) & 0xFF);
    if (cmd.led_count < 1) cmd.led_count = 1;
    if (cmd.led_count > LED_STRIP_COUNT) cmd.led_count = LED_STRIP_COUNT;
    if (xQueueSend(controlQueue, &cmd, 0) == pdTRUE) {
      lastLightSeq = seq;
      Serial.printf("[灯带] 下行 seq=%lu 已入队\n", (unsigned long)seq);
      return seq;
    }
    return 0;
  }

  if (type == "ir") {
    if (seq == 0 || seq == lastIrSeq) return 0;
    ControlCommand cmd;
    cmd.type = CTRL_IR_SEND; cmd.seq = seq;
    cmd.ir_data = doc["data"].as<uint32_t>();
    if (xQueueSend(controlQueue, &cmd, 0) == pdTRUE) {
      lastIrSeq = seq;
      Serial.printf("[红外] 下行 seq=%lu data=0x%08X\n", (unsigned long)seq, (unsigned)cmd.ir_data);
      return seq;
    }
    return 0;
  }

  if (type == "tts") {
    if (seq == 0 || seq == lastTtsSeq) return 0;
    const char *hex = doc["hex"] | "";
    if (strlen(hex) == 0) return 0;
    ControlCommand cmd;
    cmd.type = CTRL_TTS_SPEAK; cmd.seq = seq;
    strlcpy(cmd.tts_hex, hex, sizeof(cmd.tts_hex));
    if (xQueueSend(controlQueue, &cmd, 0) == pdTRUE) {
      lastTtsSeq = seq;
      Serial.printf("[TTS] 下行 seq=%lu\n", (unsigned long)seq);
      return seq;
    }
    return 0;
  }

  if (type == "ir_learn") {
    irLearnMode = doc["learn"].as<bool>();
    Serial.printf("[红外] 学习模式→%s\n", irLearnMode ? "开" : "关");
    return 0;
  }

  return 0;
}

// ===================== 网络任务 =====================
void netTask(void *param) {
  (void)param;
  esp_task_wdt_add(NULL);
  unsigned long lastWiFiCheck = millis();
  // 启动后 60s 先开一次维护窗口（配置/OTA 检查），之后恢复 10 分钟周期：
  // 这样 OTA 重启后约 1 分钟设备就会自报新版本，管理界面不再滞后一整个窗口。
  // 无符号回绕运算（millis() - (INTERVAL - 60000)）恰好使首次窗口在 T=60s 触发。
  unsigned long lastControl = millis() - (ESPNOW_MAINT_INTERVAL - 60000);
  SensorReading r;

  for (;;) {
    esp_task_wdt_reset();
    otaHealthTick();
    unsigned long now = millis();
    TransportMode mode = currentMode();

    if (mode == TRANSPORT_MQTT) {
      // ---- MQTT：连路由器，数据 + 30s 控制面 ----
      bool conn = wifiConnected() && resolveServerHost();
      if (now - lastWiFiCheck >= WIFI_CHECK_INTERVAL) {
        lastWiFiCheck = now;
        wifiCheck();
        // 连通性判定：WiFi / 服务器解析 / MQTT 连接任一失败都计一次不可用
        bool up = wifiConnected() && resolveServerHost() && mqttConnected();
        if (up) {
          if (netFailCount >= NET_FAIL_LIMIT)
            Serial.printf("[网络] 已恢复（此前连续失败 %d 次）\n", netFailCount);
          netFailCount = 0;
        } else if (++netFailCount >= NET_FAIL_LIMIT) {
          Serial.println("[网络] 连续 60s 不可用 → 自动降级为 ESP-NOW（网络恢复后自动切回）");
          autoDowngraded = true;
          probeOkCount = 0;
          lastNetProbe = millis();
          applyTransportMode(TRANSPORT_ESPNOW);
          continue;                       // 本轮不再走 MQTT 分支
        }
      }
      if (conn) {
        mqttLoop();
        if (now - lastControl >= CONFIG_FETCH_INTERVAL) {
          lastControl = now;
          fetchConfig();
          checkFirmware();
        }
      }
      if (xQueuePeek(sensorQueue, &r, pdMS_TO_TICKS(200)) == pdTRUE) {
        if (conn && mqttConnected()) {
          xQueueReceive(sensorQueue, &r, 0);
          if (!mqttPublishSensor(r)) wifiInvalidateServerIP();
          WiFi.setSleep(true);
        } else {
          vTaskDelay(pdMS_TO_TICKS(500));
        }
      }
    } else {
      // ---- ESP-NOW：不连路由器，数据走空口；控制面走维护窗口 ----
      if (!espnowReady()) espnowInit();
      espnowLoop();
      // 自动降级期间：定期探测网络是否恢复，连续成功 NET_PROBE_OK_LIMIT 次才切回 MQTT
      if (autoDowngraded && now - lastNetProbe >= NET_PROBE_INTERVAL) {
        lastNetProbe = now;
        if (probeNetwork()) {
          probeOkCount++;
          Serial.printf("[网络] 探测成功 %d/%d\n", probeOkCount, NET_PROBE_OK_LIMIT);
          if (probeOkCount >= NET_PROBE_OK_LIMIT) {
            Serial.println("[网络] 连续探测成功 → 判定网络稳定恢复，切回 MQTT（主通道）");
            autoDowngraded = false;
            probeOkCount = 0;
            netFailCount = 0;
            applyTransportMode(TRANSPORT_MQTT);
            continue;
          }
        } else {
          if (probeOkCount) Serial.println("[网络] 探测失败，成功计数清零，继续 ESP-NOW");
          probeOkCount = 0;
        }
      }
      if (now - lastControl >= ESPNOW_MAINT_INTERVAL) {
        lastControl = now;
        maintenanceWindow();
        lastControl = millis();
      }
      if (xQueuePeek(sensorQueue, &r, pdMS_TO_TICKS(200)) == pdTRUE) {
        if (espnowReady()) {
          xQueueReceive(sensorQueue, &r, 0);
          if (!espnowPublishSensor(r)) {
            uint32_t tx, ack, lost, retry;
            espnowGetStats(&tx, &ack, &lost, &retry);
            Serial.printf("[ESPNOW] 上报失败 tx=%lu ack=%lu lost=%lu\n",
                          (unsigned long)tx, (unsigned long)ack, (unsigned long)lost);
          }
        } else {
          vTaskDelay(pdMS_TO_TICKS(300));
        }
      }
    }
  }
}

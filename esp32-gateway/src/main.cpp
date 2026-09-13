/*
 * ESP32-S3 USB Dongle 网关固件（阶段二 5.4）
 * ==========================================
 * 职责：ESP-NOW（2.4G）无线内网汇聚 ↔ USB CDC（Pi /dev/ttyACM0）
 *
 *   节点 --ESP-NOW DATA--> 网关 --USB(0x10 DATA_EVENT)--> Pi usb_gateway.py --> /api/esp32/sensors
 *   Pi --USB(0x20 CMD_TO_NODE)--> 网关 --ESP-NOW CMD--> 节点
 *   节点收到 CMD 后回 ACK；网关把 ACK 以 USB(0x11) 上报（供服务器测时延/丢包）
 *   网关每 5s 上报 USB(0x12 STATUS)，内含各节点 rx 计数/RSSI/静默时长 → 稳定性测试数据源
 *
 * 协议：见 shared/espnow_proto.h（与 server/deploy_scripts/espnow_proto.py 一致）
 * 目标板：MuseLab ESP32-S3FN8（8MB Flash / 无 PSRAM）
 */
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include "espnow_proto.h"

// ===================== 配置 =====================
#define ESPNOW_CHANNEL        6        // 必须与节点一致；与路由器同信道可共存
#define USB_BAUD              115200
#define STATUS_INTERVAL_MS    5000     // 网关状态上报周期（稳定性测试数据）
#define MAX_PEERS             8

// ===================== 运行统计 =====================
static uint32_t gRxFrames = 0;         // 收到并校验通过的空口帧
static uint32_t gCrcErrors = 0;        // 空口帧校验失败
static uint32_t gUsbTx = 0;            // 写入 Pi 的 USB 帧数
static uint32_t gCmdTx = 0;            // 下发到节点的命令数
static uint32_t gCmdAck = 0;           // 收到节点 ACK 数
static uint32_t gCmdFail = 0;          // esp_now_send 回调失败次数
static uint32_t gUsbCrcErrors = 0;     // Pi 下行帧校验失败
static uint32_t gLastCmdSeq = 0;
static unsigned long gLastStatus = 0;

struct PeerStat {
  bool used = false;
  uint8_t mac[6];
  uint32_t rx = 0;
  int8_t rssi = 0;
  unsigned long lastMs = 0;
};
static PeerStat gPeers[MAX_PEERS];

// ===================== USB 下行解析（Pi → 网关）=====================
static uint8_t usbBuf[EW_USB_MAX_FRAME * 2];
static size_t usbLen = 0;

static PeerStat *peerFind(const uint8_t mac[6]) {
  for (int i = 0; i < MAX_PEERS; i++)
    if (gPeers[i].used && memcmp(gPeers[i].mac, mac, 6) == 0) return &gPeers[i];
  return nullptr;
}

static PeerStat *peerEnsure(const uint8_t mac[6]) {
  PeerStat *p = peerFind(mac);
  if (p) return p;
  for (int i = 0; i < MAX_PEERS; i++) {
    if (!gPeers[i].used) {
      gPeers[i].used = true;
      memcpy(gPeers[i].mac, mac, 6);
      gPeers[i].rx = 0;
      gPeers[i].lastMs = millis();
      if (!esp_now_is_peer_exist(mac)) {
        esp_now_peer_info_t info = {};
        memcpy(info.peer_addr, mac, 6);
        info.channel = ESPNOW_CHANNEL;
        info.encrypt = false;
        if (esp_now_add_peer(&info) != ESP_OK)
          Serial.println("[GW] 添加 ESP-NOW peer 失败");
      }
      return &gPeers[i];
    }
  }
  return nullptr;
}

static void usbSend(uint8_t type, const uint8_t *payload, uint16_t len) {
  uint8_t frame[EW_USB_MAX_FRAME];
  size_t n = ew_usb_build(frame, type, payload, len);
  if (n == 0) return;
  Serial.write(frame, n);
  gUsbTx++;
}

static void handleUsbFrame(const uint8_t *body, uint16_t len) {
  // body[0]=type, body[1..2]=len, body[3..]=payload（已去掉 magic 与 crc）
  if (len < 3) return;
  uint8_t type = body[0];
  uint16_t plen = ew_get_u16(body + 1);
  const uint8_t *payload = body + 3;
  if (type != EW_U_CMD_TO_NODE) return;

  EwAirFrame f;
  if (!ew_air_parse(payload, plen, &f) || f.type != EW_T_CMD) {
    gUsbCrcErrors++;
    Serial.println("[GW] 下行 CMD 帧解析失败");
    return;
  }
  uint8_t mac[6];
  memcpy(mac, f.dev, 6);
  peerEnsure(mac);
  gLastCmdSeq = f.seq;
  gCmdTx++;
  esp_err_t r = esp_now_send(mac, payload, plen);
  if (r != ESP_OK) {
    gCmdFail++;
    char id[13]; ew_mac_to_id(mac, id);
    Serial.printf("[GW] CMD → %s 发送失败 (%d)\n", id, (int)r);
  }
}

static void usbPoll() {
  while (Serial.available()) {
    if (usbLen >= sizeof(usbBuf)) usbLen = 0;   // 溢出保护
    usbBuf[usbLen++] = (uint8_t)Serial.read();
  }
  // 扫描合法帧
  size_t i = 0;
  while (i + 7 <= usbLen) {
    if (usbBuf[i] != EW_USB_MAGIC0 || usbBuf[i + 1] != EW_USB_MAGIC1) { i++; continue; }
    uint16_t plen = ew_get_u16(usbBuf + i + 3);
    size_t end = i + 5 + plen + 2;
    if (plen > EW_USB_MAX_PAYLOAD || end > usbLen) break;      // 半帧：等待更多数据
    uint16_t crc = ew_get_u16(usbBuf + end - 2);
    if (ew_crc16(usbBuf + i + 2, (end - 2) - (i + 2)) == crc) {
      handleUsbFrame(usbBuf + i + 2, (uint16_t)(plen + 3));
      i = end;
    } else {
      i++;
    }
  }
  if (i > 0) {
    memmove(usbBuf, usbBuf + i, usbLen - i);
    usbLen -= i;
  }
}

// ===================== ESP-NOW 回调 =====================
#if ESP_ARDUINO_VERSION_MAJOR >= 3
static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  const uint8_t *mac = info->src_addr;
  int8_t rssi = info->rx_ctrl ? (int8_t)info->rx_ctrl->rssi : 0;
#else
static void onRecv(const uint8_t *mac, const uint8_t *data, int len) {
  int8_t rssi = 0;
#endif
  EwAirFrame f;
  if (!ew_air_parse(data, (size_t)len, &f)) {
    gCrcErrors++;
    return;
  }
  gRxFrames++;
  PeerStat *p = peerEnsure(f.dev);
  if (p) {
    p->rx++;
    p->lastMs = millis();
    p->rssi = rssi;
  }

  // 对节点的 DATA/HELLO 立即回 ACK（含 RSSI，方案/测试用）
  if (f.type == EW_T_DATA || f.type == EW_T_HELLO) {
    uint8_t ack[EW_AIR_MAX_FRAME];
    uint8_t ap[4] = {(uint8_t)(f.seq & 0xFF), (uint8_t)(f.seq >> 8), (uint8_t)rssi, 0x00};
    size_t an = ew_air_build(ack, EW_T_ACK, f.dev, f.seq, 0, ap, sizeof(ap));
    if (an) esp_now_send(f.dev, ack, an);
  }

  // 节点对 CMD 的 ACK → 计入统计并上报
  if (f.type == EW_T_ACK) {
    gCmdAck++;
  }

  // 原帧透传 Pi
  uint8_t utype = (f.type == EW_T_DATA) ? EW_U_DATA_EVENT
                : (f.type == EW_T_ACK) ? EW_U_ACK_EVENT
                : (f.type == EW_T_HELLO) ? EW_U_HELLO_EVENT : EW_U_DATA_EVENT;
  usbSend(utype, data, (uint16_t)len);
}

static void onSend(const uint8_t *mac, esp_now_send_status_t status) {
  if (status != ESP_NOW_SEND_SUCCESS) gCmdFail++;
}

// ===================== RSSI 旁路采集 =====================
// Arduino core 2.x 的 ESP-NOW 接收回调不提供 rx_ctrl（core 3.x 才有），
// 因此用混杂模式嗅探 802.11 管理帧（ESP-NOW 即 vendor-specific action 帧），
// 按发送方 MAC 把 rx_ctrl.rssi 回填到对应 peer；不影响 ESP-NOW 正常收发。
static uint32_t gSniffAll = 0;         // 混杂模式回调总帧数（诊断 RSSI 旁路）
static uint32_t gSniffHit = 0;         // 命中已知节点 MAC 的帧数
static int gSniffRc = -99;             // esp_wifi_set_promiscuous 返回值
static int8_t gSniffLast = 0;          // 最近一帧 RSSI

static void snifferCb(void *buf, wifi_promiscuous_pkt_type_t type) {
  (void)type;
  gSniffAll++;
  const wifi_promiscuous_pkt_t *p = (const wifi_promiscuous_pkt_t *)buf;
  const uint8_t *f = p->payload;
  int len = p->rx_ctrl.sig_len;
  gSniffLast = (int8_t)p->rx_ctrl.rssi;
  if (len < 24) return;
  // 不做帧类型/category 判据（ESP-NOW 在各 IDF 版本下分类不一致）：
  // 只认「802.11 addr2 == 已注册节点 MAC」，误匹配概率极低。
  PeerStat *ps = peerFind(f + 10);
  if (ps) {
    ps->rssi = (int8_t)p->rx_ctrl.rssi;
    gSniffHit++;
  }
}

// ===================== 状态上报 =====================
static void sendStatus() {
  char json[480];
  char gw[13];
  uint8_t mac[6];
  WiFi.macAddress(mac);
  ew_mac_to_id(mac, gw);
  int pos = snprintf(json, sizeof(json),
                     "{\"gw\":\"%s\",\"ch\":%d,\"up\":%lu,\"rx\":%lu,\"crc_err\":%lu,"
                     "\"usb_tx\":%lu,\"cmd_tx\":%lu,\"cmd_ack\":%lu,\"cmd_fail\":%lu,"
                     "\"sniff\":[%lu,%lu,%d,%d],\"nodes\":[",
                     gw, ESPNOW_CHANNEL, (unsigned long)(millis() / 1000), (unsigned long)gRxFrames,
                     (unsigned long)gCrcErrors, (unsigned long)gUsbTx, (unsigned long)gCmdTx,
                     (unsigned long)gCmdAck, (unsigned long)gCmdFail,
                     (unsigned long)gSniffAll, (unsigned long)gSniffHit, gSniffRc, gSniffLast);
  bool first = true;
  for (int i = 0; i < MAX_PEERS; i++) {
    if (!gPeers[i].used) continue;
    char id[13];
    ew_mac_to_id(gPeers[i].mac, id);
    pos += snprintf(json + pos, sizeof(json) - pos,
                    "%s{\"id\":\"%s\",\"rx\":%lu,\"rssi\":%d,\"age_ms\":%lu}",
                    first ? "" : ",", id, (unsigned long)gPeers[i].rx,
                    gPeers[i].rssi, (unsigned long)(millis() - gPeers[i].lastMs));
    first = false;
  }
  snprintf(json + pos, sizeof(json) - pos, "]}");
  usbSend(EW_U_STATUS, (const uint8_t *)json, (uint16_t)strlen(json));
}

// ===================== 初始化 =====================
void setup() {
  Serial.begin(USB_BAUD);
  delay(300);
  Serial.println();
  Serial.println("======================================");
  Serial.println("ESP32-S3 ESP-NOW → USB 网关 (5.4)");
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char id[13];
  ew_mac_to_id(mac, id);
  Serial.printf("网关 MAC/ID: %s  CH=%d\n", id, ESPNOW_CHANNEL);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  WiFi.setSleep(false);                  // 关 modem sleep：ESP-NOW 低时延的关键
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) {
    Serial.println("[GW] ESP-NOW 初始化失败，重启");
    delay(1000);
    ESP.restart();
  }
  esp_now_register_recv_cb(onRecv);
  esp_now_register_send_cb(onSend);

  // RSSI 旁路采集（core 2.x 无 rx_ctrl，见 snifferCb 注释）
  esp_wifi_set_promiscuous_rx_cb(snifferCb);
  gSniffRc = (int)esp_wifi_set_promiscuous(true);
  Serial.printf("[GW] RSSI 旁路采集 rc=%d\n", gSniffRc);

  // 广播 peer（便于节点首次 HELLO 被发现）
  esp_now_peer_info_t bcast = {};
  memset(bcast.peer_addr, 0xFF, 6);
  bcast.channel = ESPNOW_CHANNEL;
  bcast.encrypt = false;
  esp_now_add_peer(&bcast);

  Serial.println("[GW] 就绪：等待节点上报 / 接收 Pi 命令");
  Serial.println("======================================");
}

void loop() {
  usbPoll();
  unsigned long now = millis();
  if (now - gLastStatus >= STATUS_INTERVAL_MS) {
    gLastStatus = now;
    sendStatus();
  }
  delay(5);
}

/*
 * 全局共享状态定义
 * ================
 * app_config.h 中声明的全局对象在这里分配实体。
 * 队列与互斥锁在 main.cpp 的 setup() 中创建。
 */
#include "app_config.h"
#include <esp_wifi.h>       // esp_read_mac()

// ===================== 服务器连接配置 =====================
// 使用 mDNS 主机名而非硬编码 IP——Orange Pi 的局域网 IP 可能因 DHCP 变化。
// wang-orange.local 由 Orange Pi 上的 Avahi 注册，ESP32 通过 WiFi.hostByName() 解析。
const char *SERVER_HOST = "wang-orange.local";
const int SERVER_PORT = 8080;

// ===================== 设备唯一标识 =====================
// 使用 esp_read_mac() 而非 WiFi.macAddress()，因为后者需要 WiFi 初始化后才能调用。
// 编译期确定，全局不变，整个生命周期只计算一次。
static String getESP32ID() {
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char macStr[18] = {0};
  sprintf(macStr, "%02X%02X%02X%02X%02X%02X",
          mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  return String(macStr);
}
const String ESP32_ID = getESP32ID();

// ===================== 任务间通信对象 =====================
QueueHandle_t sensorQueue = NULL;
QueueHandle_t controlQueue = NULL;
QueueHandle_t irReportQueue = NULL;
volatile bool irLearnMode = false;

// ===================== 共享运行配置 =====================
SemaphoreHandle_t configMutex = NULL;
unsigned long SEND_INTERVAL = 5000;     // 默认 5 秒上报一次（可被服务器动态修改）
String deviceLocation = "卧室";          // 默认位置分类（可被服务器动态修改）
int payloadPad = 0;                     // 上行 JSON 填充字节数（T5 载荷扫描用，0=不填充）

// ===================== 传输模式（v1.9.0：MQTT / ESP-NOW） =====================
// 默认 MQTT；服务器 config 下发 "transport":"espnow" 后由 net_task 切换。
TransportMode transportMode = TRANSPORT_MQTT;
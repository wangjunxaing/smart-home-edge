/*
 * WiFi 管理模块
 * =============
 * 多网络连接策略:
 *   1. 启动时按 WIFI_CREDENTIALS 数组顺序逐个尝试
 *   2. 连接成功后本次运行期间不再切换
 *   3. 断线后由 wifiCheck() 重新走完整连接流程
 *
 * 本模块只被 net_task 调用（网络单一所有者原则），
 * LED 状态指示交由 control_task 根据 wifiConnected() 派生。
 */
#include "wifi_manager.h"
#include "app_config.h"
#include <WiFi.h>
#include "wifi_config.h"    // 本地 WiFi 多网络配置（不进仓库）

// ===================== 模块内部状态 =====================
static IPAddress serverIP;                   // mDNS 解析后的实际 IP
static bool serverIPResolved = false;        // 解析成功标志
static unsigned long lastDNSResolveTime = 0; // 上一次解析时间戳（60 秒缓存）
static int currentWiFiIndex = -1;            // 当前连接的网络在凭证数组中的索引

// ===================== mDNS 主机名解析 =====================
bool resolveServerHost() {
  if (WiFi.status() != WL_CONNECTED) return false;

  unsigned long now = millis();
  // 60 秒缓存：DNS 查询是 UDP 开销，不必每次都查
  if (serverIPResolved && (now - lastDNSResolveTime < 60000)) return true;

  Serial.printf("[DNS] 解析 %s ... ", SERVER_HOST);
  IPAddress ip;
  if (WiFi.hostByName(SERVER_HOST, ip)) {
    serverIP = ip;
    serverIPResolved = true;
    lastDNSResolveTime = now;
    Serial.printf("-> %s\n", ip.toString().c_str());
    return true;
  }
  Serial.println("失败");
  return false;
}

// ===================== 构造服务器 URL =====================
String makeURL(const char *path) {
  if (!serverIPResolved) resolveServerHost();
  return String("http://") + serverIP.toString() + ":" + SERVER_PORT + path;
}

void wifiInvalidateServerIP() {
  serverIPResolved = false;
}

IPAddress wifiServerIP() {
  if (!serverIPResolved) resolveServerHost();
  return serverIP;
}

bool wifiConnected() {
  return WiFi.status() == WL_CONNECTED;
}

// ===================== 单网络尝试 =====================
// 外层循环：最多 WIFI_MAX_ATTEMPTS (3) 次重试
// 内层循环：每次等待 WIFI_ATTEMPT_TIMEOUT (5000)ms，每 500ms 检查一次
static bool tryConnectWiFi(int index) {
  if (index < 0 || index >= WIFI_NETWORK_COUNT) return false;
  Serial.printf("[WiFi] 尝试[%d] %s ... ", index, WIFI_CREDENTIALS[index][0]);
  WiFi.begin(WIFI_CREDENTIALS[index][0], WIFI_CREDENTIALS[index][1]);

  for (int att = 0; att < WIFI_MAX_ATTEMPTS; att++) {
    for (int w = 0; w < WIFI_ATTEMPT_TIMEOUT / 500; w++) {
      if (WiFi.status() == WL_CONNECTED) {
        Serial.println("OK");
        Serial.printf("[WiFi] IP: %s\n", WiFi.localIP().toString().c_str());
        currentWiFiIndex = index;
        return true;
      }
      vTaskDelay(pdMS_TO_TICKS(500));
      Serial.print(".");
    }
    if (att < WIFI_MAX_ATTEMPTS - 1)
      Serial.printf("\n[WiFi] 重试 %d/%d...\n", att + 2, WIFI_MAX_ATTEMPTS);
  }
  Serial.println("失败");
  return false;
}

// ===================== 多网络遍历连接 =====================
bool wifiConnect() {
  WiFi.mode(WIFI_STA);                  // 只做客户端（Station），不做 AP
  WiFi.setSleep(true);                  // Modem Sleep：空闲时关闭 RF 模块
  WiFi.setTxPower(WIFI_POWER_19_5dBm);  // 限制发射功率（19.5dBm ≈ 90mW）

  for (int i = 0; i < WIFI_NETWORK_COUNT; i++) {
    if (tryConnectWiFi(i)) return true;
  }

  Serial.println("[WiFi] 所有网络均连接失败");
  return false;
}

// ===================== 断线监控与自动恢复 =====================
void wifiCheck() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] 断开，开始重连...");
    WiFi.disconnect(true);   // 彻底断开（清除内部状态，释放内存）
    vTaskDelay(pdMS_TO_TICKS(100));
    wifiConnect();
    if (WiFi.status() == WL_CONNECTED) resolveServerHost();
  }
}
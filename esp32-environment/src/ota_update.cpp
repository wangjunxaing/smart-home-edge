/*
 * OTA 固件升级模块（5.2 签名 + A/B 双槽健康回滚）
 * ===============================================
 * 流程（由 control_task 调用，升级期间传感器采集照常）:
 *   net_task 轮询发现新版本 → controlQueue → otaPerform()
 *   ① HTTP 下载，响应头必须携带 x-SHA256 与 x-Signature（服务器 RSA-2048 签名）
 *   ② 流式写入空闲 OTA 分区，同时计算 SHA-256
 *   ③ 摘要与响应头一致 + RSA 验签通过 → NVS 记 pending → 切启动分区 → 重启
 *   ④ 失败：esp_ota_abort，进 10 分钟冷却，不切换分区（旧固件继续跑）
 *
 * 回滚策略（应用级，无需 bootloader rollback）:
 *   otaBootHealthCheck()：若 pending=1，每次启动 resets+1；
 *     超过 OTA_MAX_BOOT_TRIES 次仍未健康确认 → esp_ota_set_boot_partition(旧槽) 重启
 *   otaHealthTick()：新固件稳定运行超 OTA_HEALTH_GRACE_MS 且网络正常 → 清 pending，视为健康
 */
#include "ota_update.h"
#include "ota_pubkey.h"
#include "app_config.h"
#include "wifi_manager.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <Preferences.h>
#include <mbedtls/pk.h>
#include <mbedtls/md.h>
#include <mbedtls/base64.h>
#include <esp_task_wdt.h>

// ===================== 参数 =====================
static const unsigned long OTA_FAIL_COOLDOWN_MS = 10UL * 60UL * 1000UL; // 失败冷却
static const unsigned long OTA_HEALTH_GRACE_MS = 90000UL;               // 健康宽限期 90s
static const uint8_t OTA_MAX_BOOT_TRIES = 3;                            // 异常启动阈值

// ===================== 模块内部状态 =====================
static bool otaInProgress = false;
static unsigned long lastFailTime = 0;
static bool bootPending = false;        // 本次启动处于"待健康确认"
static unsigned long bootTimeMs = 0;    // 启动时刻（健康宽限计时起点）
static bool healthCleared = false;

// 签名公钥缓存（首次使用解码一次）
static mbedtls_pk_context otaPk;
static bool otaPkReady = false;

// ===================== 工具：base64 解码 =====================
static int b64Decode(const char *b64, uint8_t *out, size_t outMax) {
  size_t olen = 0;
  int rc = mbedtls_base64_decode(out, outMax, &olen,
                                 (const unsigned char *)b64, strlen(b64));
  if (rc != 0) {
    Serial.printf("[OTA] base64 解码失败 rc=%d\n", rc);
    return -1;
  }
  return (int)olen;
}

// ===================== 签名公钥初始化（首次验签前调用） =====================
static void ensurePubKey() {
  if (otaPkReady) return;
  mbedtls_pk_init(&otaPk);
  uint8_t der[400];
  int derLen = b64Decode(OTA_PUBKEY_DER_B64, der, sizeof(der));
  if (derLen <= 0) {
    Serial.println("[OTA] 公钥解码失败");
    return;
  }
  int rc = mbedtls_pk_parse_public_key(&otaPk, der, derLen);
  if (rc != 0) {
    Serial.printf("[OTA] 公钥解析失败 rc=%d\n", rc);
    mbedtls_pk_free(&otaPk);
    return;
  }
  otaPkReady = true;
}

// ===================== NVS 标志读写 =====================
static void otaNvsSetPending(bool pending, uint8_t resets) {
  Preferences p;
  p.begin("ota", false);
  p.putUChar("pending", pending ? 1 : 0);
  p.putUChar("resets", resets);
  p.end();
}

// ===================== 引导健康检查 =====================
// 在 setup 早期调用：若上次升级了固件且尚未确认健康，
// 每次启动计数；超过阈值说明新固件崩溃/卡死 → 自动回滚旧分区。
void otaBootHealthCheck() {
  bootTimeMs = millis();
  Preferences p;
  p.begin("ota", false);
  bootPending = (p.getUChar("pending", 0) != 0);
  if (bootPending) {
    uint8_t resets = p.getUChar("resets", 0) + 1;
    p.putUChar("resets", resets);
    Serial.printf("[OTA] 新固件第 %u 次启动，宽限 %lu ms 后确认健康\n",
                  (unsigned)resets, (unsigned long)OTA_HEALTH_GRACE_MS);
    if (resets > OTA_MAX_BOOT_TRIES) {
      // 连续异常启动 → 回滚到另一 OTA 槽
      const esp_partition_t *run = esp_ota_get_running_partition();
      const esp_partition_t *prev = NULL;
      if (run && run->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0)
        prev = esp_partition_find_first(ESP_PARTITION_TYPE_APP,
                                        ESP_PARTITION_SUBTYPE_APP_OTA_1, NULL);
      else
        prev = esp_partition_find_first(ESP_PARTITION_TYPE_APP,
                                        ESP_PARTITION_SUBTYPE_APP_OTA_0, NULL);
      p.putUChar("pending", 0);
      p.putUChar("resets", 0);
      p.end();
      if (prev) {
        Serial.println("[OTA] 新固件连续异常启动，回滚到旧分区并重启...");
        esp_ota_set_boot_partition(prev);
      }
      ESP.restart();
    }
  } else {
    p.putUChar("resets", 0);
  }
  p.end();
}

// ===================== 健康确认（周期调用） =====================
void otaHealthTick() {
  if (!bootPending || healthCleared) return;
  if ((unsigned long)(millis() - bootTimeMs) < OTA_HEALTH_GRACE_MS) return;
  if (!wifiConnected()) return;               // 网络异常不算健康运行
  Preferences p;
  p.begin("ota", false);
  p.putUChar("pending", 0);
  p.putUChar("resets", 0);
  p.end();
  bootPending = false;
  healthCleared = true;
  Serial.println("[OTA] 新固件运行正常，已确认健康（pending 清除）");
  // 尽力而为：通知 bootloader 本镜像有效（无回滚配置时无害）
  esp_ota_mark_app_valid_cancel_rollback();
}

bool otaShouldAttempt() {
  if (otaInProgress) return false;
  if (lastFailTime != 0 && (millis() - lastFailTime < OTA_FAIL_COOLDOWN_MS))
    return false;
  return true;
}

// ===================== SHA-256 十六进制工具 =====================
static void hexEncode(const unsigned char *in, size_t len, char *out) {
  static const char *HEXDIG = "0123456789abcdef";
  for (size_t i = 0; i < len; i++) {
    out[i * 2] = HEXDIG[in[i] >> 4];
    out[i * 2 + 1] = HEXDIG[in[i] & 0x0F];
  }
  out[len * 2] = '\0';
}

// ===================== RSA 验签（消息 = sha256 小写十六进制串的 SHA-256 摘要） =====================
static bool verifySignature(const char *sha256Hex, const char *sigB64) {
  ensurePubKey();
  if (!otaPkReady) {
    Serial.println("[OTA] 验签跳过：公钥未就绪");
    return false;
  }
  uint8_t sig[512];
  int sigLen = b64Decode(sigB64, sig, sizeof(sig));
  if (sigLen <= 0) return false;
  // 服务器签名对象是 "sha256 小写 hex 字符串" 的 SHA-256 摘要（32B）。
  // mbedtls_pk_verify 期望传入的 hash 就是该摘要，而不是 64 字符的 hex 串本身。
  unsigned char digest[32];
  const mbedtls_md_info_t *mi = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (mi == NULL || mbedtls_md(mi, (const unsigned char *)sha256Hex,
                               strlen(sha256Hex), digest) != 0) {
    Serial.println("[OTA] 摘要计算失败");
    return false;
  }
  int rc = mbedtls_pk_verify(&otaPk, MBEDTLS_MD_SHA256, digest, sizeof(digest),
                             sig, sigLen);
  if (rc != 0) {
    Serial.printf("[OTA] 签名验证失败 rc=%d（固件来源不可信）\n", rc);
    return false;
  }
  Serial.println("[OTA] 签名验证通过");
  return true;
}

// ===================== 执行升级 =====================
void otaPerform(const char *path, const char *version) {
  otaInProgress = true;
  String url = makeURL(path);
  Serial.printf("[OTA] v%s → v%s，下载 %s\n", FW_VERSION, version, url.c_str());

  WiFiClient client;
  HTTPClient http;
  if (!http.begin(client, url)) {
    Serial.println("[OTA] HTTP 初始化失败");
    lastFailTime = millis();
    otaInProgress = false;
    return;
  }
  // 关键：HTTPClient 默认不缓存响应头，header() 会读空；
  // 必须先 collectHeaders 声明需要的头（x-MD5 供 HTTPUpdate 兼容流；本模块读 x-SHA256/x-Signature）
  const char *wantHeaders[] = {"x-SHA256", "x-Signature", "x-MD5"};
  http.collectHeaders(wantHeaders, sizeof(wantHeaders) / sizeof(wantHeaders[0]));
  http.setTimeout(10000);

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("[OTA] HTTP %d\n", code);
    http.end();
    lastFailTime = millis();
    otaInProgress = false;
    return;
  }

  String shaHdr = http.header("x-SHA256");
  String sigHdr = http.header("x-Signature");
  if (shaHdr.length() != 64 || sigHdr.length() == 0) {
    Serial.printf("[OTA] 缺少签名响应头（sha=%d sig=%d），拒绝升级\n",
                  shaHdr.length(), sigHdr.length());
    http.end();
    lastFailTime = millis();
    otaInProgress = false;
    return;
  }

  int total = http.getSize();
  if (total <= 0) total = OTA_SIZE_UNKNOWN;
  const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
  if (next == NULL) {
    Serial.println("[OTA] 无可用升级分区");
    http.end();
    lastFailTime = millis();
    otaInProgress = false;
    return;
  }
  Serial.printf("[OTA] 目标分区 %s（大小 %d）\n", next->label, total);

  esp_ota_handle_t h;
  esp_err_t err = esp_ota_begin(next, (size_t)total, &h);
  if (err != ESP_OK) {
    Serial.printf("[OTA] esp_ota_begin 失败: %s\n", esp_err_to_name(err));
    http.end();
    lastFailTime = millis();
    otaInProgress = false;
    return;
  }

  // 流式写盘 + SHA-256
  mbedtls_md_context_t md;
  mbedtls_md_init(&md);
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  bool shaOk = (info != NULL) && (mbedtls_md_setup(&md, info, 0) == 0);
  if (shaOk) mbedtls_md_starts(&md);

  Stream *s = http.getStreamPtr();
  uint8_t buf[4096];
  size_t written = 0;
  bool writeFail = false;
  while (http.connected() || (s != NULL && s->available() > 0)) {
    size_t n = (s != NULL) ? s->readBytes(buf, sizeof(buf)) : 0;
    if (n == 0) break;
    if (esp_ota_write(h, buf, n) != ESP_OK) {
      writeFail = true;
      break;
    }
    if (shaOk) mbedtls_md_update(&md, buf, n);
    written += n;
    esp_task_wdt_reset();
  }
  http.end();

  char shaHex[65] = {0};
  bool digestOk = false;
  if (!writeFail && shaOk) {
    unsigned char digest[32];
    mbedtls_md_finish(&md, digest);
    hexEncode(digest, 32, shaHex);
    mbedtls_md_free(&md);
    digestOk = true;
  }
  if (!writeFail && !digestOk) writeFail = true;

  // 校验摘要 + 验签
  bool verified = false;
  if (!writeFail) {
    if (String(shaHex) != shaHdr) {
      Serial.printf("[OTA] SHA-256 不符：本地 %s / 期望 %s\n", shaHex, shaHdr.c_str());
    } else if (verifySignature(shaHex, sigHdr.c_str())) {
      verified = true;
    }
  }

  if (!verified) {
    esp_ota_abort(h);   // 丢弃新分区内容
    Serial.printf("[OTA] 校验未通过，放弃升级（已写入 %u 字节）\n", (unsigned)written);
    lastFailTime = millis();
    otaInProgress = false;
    return;
  }

  err = esp_ota_end(h);
  if (err != ESP_OK) {
    Serial.printf("[OTA] esp_ota_end 失败: %s\n", esp_err_to_name(err));
    lastFailTime = millis();
    otaInProgress = false;
    return;
  }

  // 记 pending（下次启动进入健康检查流程），切分区重启
  otaNvsSetPending(true, 0);
  err = esp_ota_set_boot_partition(next);
  if (err != ESP_OK) {
    Serial.printf("[OTA] 切换分区失败: %s\n", esp_err_to_name(err));
    lastFailTime = millis();
    otaInProgress = false;
    return;
  }
  Serial.println("[OTA] 验签通过，切换到新分区，5s 后重启...");
  delay(500);
  otaInProgress = false;
  ESP.restart();
}

/*
 * TTS 语音播报模块实现（5.3，WT3000T-MO1）
 * ========================================
 * 严格按官方库 WTSerial_V1.05 的帧格式（2 字节长度 + 累加和校验）：
 *   7E | LEN_HI LEN_LO(=数据区长度+3) | 数据区 | SUM | EF
 *   SUM = (LEN_HI + LEN_LO + 数据区各字节) & 0xFF
 */
#include "tts_voice.h"
#include "app_config.h"

#define TTS_UART_BAUD   9600
#define TTS_PIN_TX      17
#define TTS_PIN_RX      16
#define TTS_MAX_TEXT    250        // 单包文本上限（协议 ≤255）
#define TTS_DEFAULT_VOL 25         // 0~30

static const uint8_t TTS_HDR[3] = {0x01, 0x00, 0x03};   // 流水号/应答标志/来源

static void ttsSendFrame(const uint8_t *data, uint16_t len) {
  uint16_t frameLen = (uint16_t)(len + 3);              // 数据区 + 校验 + 结束 + 长度高
  uint8_t sum = (uint8_t)((frameLen >> 8) + (frameLen & 0xFF));
  Serial2.write(0x7E);
  Serial2.write((frameLen >> 8) & 0xFF);
  Serial2.write(frameLen & 0xFF);
  Serial2.write(data, len);
  for (uint16_t i = 0; i < len; i++) sum += data[i];
  Serial2.write(sum);
  Serial2.write(0xEF);
  Serial2.flush();
}

void ttsSetVolume(uint8_t vol) {
  uint8_t d[7] = {0x01, 0x00, 0x03, 0x00, 0xCC, 0x01, (uint8_t)(vol > 30 ? 30 : vol)};
  ttsSendFrame(d, sizeof(d));
  delay(20);
}

static void ttsSetCoderUtf8() {
  uint8_t d[7] = {0x01, 0x00, 0x03, 0x03, 0xEF, 0x01, 0x06};   // 0x06 = UTF-8
  ttsSendFrame(d, sizeof(d));
  delay(20);
}

void ttsInit() {
  Serial2.begin(TTS_UART_BAUD, SERIAL_8N1, TTS_PIN_RX, TTS_PIN_TX);
  delay(100);
  ttsSetCoderUtf8();          // 默认就是 UTF-8，显式再设一次更稳
  ttsSetVolume(TTS_DEFAULT_VOL);
  Serial.printf("[TTS] UART2 就绪（%d baud, TX=%d RX=%d，UTF-8，音量 %d）\n",
                TTS_UART_BAUD, TTS_PIN_TX, TTS_PIN_RX, TTS_DEFAULT_VOL);
}

static int hexVal(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// 发送一段文本字节（自动按 255B 分包，数据区 = 包头 + [03 E8 len data]...）
static void ttsSendTextBytes(const uint8_t *text, size_t len) {
  if (len == 0) return;
  uint8_t buf[TTS_MAX_TEXT + 8];
  size_t off = 0;
  while (off < len) {
    size_t chunk = len - off;
    if (chunk > TTS_MAX_TEXT) chunk = TTS_MAX_TEXT;
    size_t pos = 0;
    memcpy(&buf[pos], TTS_HDR, 3); pos += 3;
    buf[pos++] = 0x03;
    buf[pos++] = 0xE8;
    buf[pos++] = (uint8_t)chunk;
    memcpy(&buf[pos], text + off, chunk); pos += chunk;
    ttsSendFrame(buf, (uint16_t)pos);
    delay(20);               // 官方库建议帧间隔 ≥20ms
    off += chunk;
  }
  Serial.printf("[TTS] 已发送播报文本 %u 字节\n", (unsigned)len);
}

void ttsSpeakHex(const char *hexStr) {
  if (hexStr == NULL) return;
  size_t n = strlen(hexStr);
  if (n % 2 != 0) n--;
  size_t bytes = n / 2;
  if (bytes > TTS_MAX_TEXT) bytes = TTS_MAX_TEXT;
  uint8_t data[TTS_MAX_TEXT];
  for (size_t i = 0; i < bytes; i++) {
    int hi = hexVal(hexStr[i * 2]);
    int lo = hexVal(hexStr[i * 2 + 1]);
    if (hi < 0 || lo < 0) {
      Serial.println("[TTS] 十六进制串非法，取消播报");
      return;
    }
    data[i] = (uint8_t)((hi << 4) | lo);
  }
  ttsSendTextBytes(data, bytes);
}

void ttsSpeakAscii(const char *text) {
  if (text == NULL) return;
  size_t n = strlen(text);
  if (n > TTS_MAX_TEXT) n = TTS_MAX_TEXT;
  ttsSendTextBytes((const uint8_t *)text, n);
}

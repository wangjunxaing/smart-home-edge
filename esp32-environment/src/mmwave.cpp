/*
 * 24GHz 毫米波人体感知（5.8）— 双协议实现：LD2410C + LD2450
 * ==========================================================
 * 接线（UART1 重映射，见 PLAN §7.3.2）：
 *   ESP32 GPIO18 (TX) → 模块 RX
 *   ESP32 GPIO19 (RX) ← 模块 TX
 *   3V3 / GND 共地（3.3V TTL 直连）
 *
 * 为什么必须重映射：UART1 默认脚 GPIO9/10 属 Flash 引脚；GPIO16/17 被 TTS 占用。
 *
 * ---- 两种模块协议完全不同，本模块自动识别 ----
 *
 * ① LD2410C（**有 OUT 脚**）：只给"距离"，无方位
 *    命令帧：FD FC FB FA | len(2,LE) | 数据 | 04 03 02 01
 *    数据帧：F4 F3 F2 F1 | len(2,LE) | 数据 | F8 F7 F6 F5
 *    数据区 13B 按位：02 AA | 状态 | 运动距(2) | 运动能量 | 静止距(2) | 静止能量 | 探测距(2) | 55 00
 *
 * ② LD2450（**没有 OUT 脚**）：给 x/y 平面坐标 + 速度（**2D，不是 3D**）
 *    数据帧：AA FF 03 00 | len(2,LE) | 数据 | 55 CC
 *    数据区 24B = 3 目标 × 8B：[x int16 mm][y int16 mm][speed int16 cm/s][分辨率 int16 mm]
 *    空目标 8 字节全 0；y 正向为探测方向、x 正向为右；距离 = sqrt(x²+y²)
 *
 * ---- 诊断设计（不接串口也能定位）----
 *   stats: rx_bytes / frames_ok / frames_bad / baud / baud_tries / protocol
 *   raw_hex: 前 32 字节的十六进制，随上报发到服务器 → 一眼看出模块吐的是什么协议
 */
#include "mmwave.h"

#include "app_config.h"

#if MMWAVE_ENABLED

// ===================== 帧格式常量 =====================
static const uint8_t LD2410_CMD_HEAD[4]  = {0xFD, 0xFC, 0xFB, 0xFA};
static const uint8_t LD2410_CMD_TAIL[4]  = {0x04, 0x03, 0x02, 0x01};
static const uint8_t LD2410_DATA_HEAD[4] = {0xF4, 0xF3, 0xF2, 0xF1};
static const uint8_t LD2410_DATA_TAIL[4] = {0xF8, 0xF7, 0xF6, 0xF5};
static const uint8_t LD2450_DATA_HEAD[4] = {0xAA, 0xFF, 0x03, 0x00};
static const uint8_t LD2450_DATA_TAIL[2] = {0x55, 0xCC};

// ===================== 内部状态 =====================
static MmwaveReading s_cur;
static bool s_inited = false;
static uint32_t s_frames_ok = 0;
static uint32_t s_frames_bad = 0;
static uint32_t s_rx_bytes = 0;
static uint32_t s_cmd_frames = 0;
static uint8_t s_protocol = 0;          // 0=未知 1=LD2410C 2=LD2450

// 收帧状态机
static uint8_t s_buf[192];
static uint8_t s_len = 0;
static uint8_t s_kind = 0;              // 1=LD2410命令 2=LD2410数据 3=LD2450数据

// 原始字节抓取（前 64 字节 → hex 上报，够装 2 帧 LD2450）
static uint8_t s_raw[64];
static uint8_t s_raw_n = 0;
static bool s_raw_ready = false;

// 自动波特率
static const uint32_t BAUD_CANDIDATES[] = {256000, 115200, 57600, 9600};
static const uint8_t BAUD_COUNT = sizeof(BAUD_CANDIDATES) / sizeof(BAUD_CANDIDATES[0]);
static uint8_t s_baud_idx = 0;
static uint8_t s_baud_tries = 0;
static uint32_t s_switch_at = 0;

// LD2410 命令
static const uint8_t CMD_EXIT_CONFIG[] = {
  0xFD, 0xFC, 0xFB, 0xFA, 0x02, 0x00, 0xFE, 0x00, 0x04, 0x03, 0x02, 0x01};
static const uint8_t CMD_READ_VERSION[] = {
  0xFD, 0xFC, 0xFB, 0xFA, 0x02, 0x00, 0xA0, 0x00, 0x04, 0x03, 0x02, 0x01};

static inline uint16_t le16(const uint8_t *p) {
  return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static void sendRaw(const uint8_t *p, uint8_t n) {
  Serial1.write(p, n);
  Serial1.flush();
}

// ===================== 解析：LD2410C =====================
static bool parseLD2410(const uint8_t *b, uint8_t n) {
  if (n < 4 || b[0] != 0x02 || b[1] != 0xAA) return false;

  int8_t state;
  int16_t moving = -1, still = -1, detect = -1;
  int8_t m_energy = 0, s_energy = 0;

  if (n >= 17 && b[2] == 0x03 && b[3] == 0x0D) {
    state = (int8_t)b[4];
    for (uint8_t i = 5; i + 1 < n; i++) {
      uint8_t tag = b[i];
      if (tag == 0x04 && i + 2 < n) { moving = (int16_t)le16(&b[i + 1]); i += 2; }
      else if (tag == 0x05 && i + 1 < n) { m_energy = (int8_t)b[i + 1]; i += 1; }
      else if (tag == 0x06 && i + 2 < n) { still = (int16_t)le16(&b[i + 1]); i += 2; }
      else if (tag == 0x07 && i + 1 < n) { s_energy = (int8_t)b[i + 1]; i += 1; }
      else if (tag == 0x08 && i + 2 < n) { detect = (int16_t)le16(&b[i + 1]); i += 2; }
    }
  } else if (n >= 11) {
    state = (int8_t)b[2];
    moving = (int16_t)le16(&b[3]);
    m_energy = (int8_t)b[5];
    still = (int16_t)le16(&b[6]);
    s_energy = (int8_t)b[8];
    detect = (int16_t)le16(&b[9]);
  } else {
    return false;
  }

  if (state < 0 || state > 3) return false;
  if (moving > 1200 || still > 1200 || detect > 1200) return false;

  s_cur.person = (state == 0) ? 0 : 1;
  s_cur.target_state = (uint8_t)state;
  s_cur.moving_dist = moving;
  s_cur.still_dist = still;
  s_cur.detect_dist = detect;
  s_cur.moving_energy = (m_energy < 0) ? 0 : (uint8_t)m_energy;
  s_cur.still_energy = (s_energy < 0) ? 0 : (uint8_t)s_energy;
  s_cur.x_mm = 0;
  s_cur.y_mm = 0;
  s_cur.speed_cms = 0;
  s_cur.protocol = 1;
  s_protocol = 1;
  return true;
}

// ===================== 解析：LD2450（x/y 坐标）=====================
// 权威协议（wscanplus/HLK-LD2450_RESEARCH、官方文档）：
//   帧：AA FF 03 00 | 24 字节数据区 | 55 CC  —— **固定 30 字节，没有长度字段！**
//   数据区 = 3 个目标槽 × 8 字节：[x][y][speed][resolution]，各 int16 小端
//
//   **坐标是特殊的"符号位编码"，不是补码！**（这是最容易踩的坑）
//     raw & 0x8000 != 0 → 正值 = raw - 0x8000
//     raw & 0x8000 == 0 → 负值 = 0 - raw
//   例：raw 0x030E=782 → -782mm；raw 0x86B1 → 0x86B1-0x8000 = 1713mm
static inline int16_t decodeLD2450(uint16_t raw) {
  if (raw & 0x8000) return (int16_t)(raw - 0x8000);
  return (int16_t)(0 - (int16_t)raw);
}

static bool parseLD2450(const uint8_t *b, uint8_t n) {
  if (n < 8) return false;

  bool found = false;
  for (uint8_t t = 0; t < 3 && (uint8_t)(t + 1) * 8 <= n; t++) {
    const uint8_t *p = b + t * 8;
    uint16_t rx = le16(p);
    uint16_t ry = le16(p + 2);
    // 空目标槽：8 字节全 0
    if (rx == 0 && ry == 0) continue;

    int16_t x = decodeLD2450(rx);
    int16_t y = decodeLD2450(ry);
    int16_t sp = decodeLD2450(le16(p + 4));

    // 合理范围：±8m（LD2450 标称 ±6m）
    if (x < -8000 || x > 8000 || y < -8000 || y > 8000) continue;

    s_cur.x_mm = x;
    s_cur.y_mm = y;
    s_cur.speed_cms = sp;
    float d = sqrtf((float)x * (float)x + (float)y * (float)y);
    s_cur.detect_dist = (int16_t)(d / 10.0f);
    s_cur.person = 1;
    s_cur.target_state = (sp != 0) ? 1 : 2;
    s_cur.moving_dist = (sp != 0) ? s_cur.detect_dist : -1;
    s_cur.still_dist = (sp == 0) ? s_cur.detect_dist : -1;
    s_cur.moving_energy = (sp != 0) ? (uint8_t)(abs(sp) > 100 ? 100 : abs(sp)) : 0;
    s_cur.still_energy = 0;
    s_cur.protocol = 2;
    s_protocol = 2;
    found = true;
    break;
  }

  if (!found) {
    s_cur.person = 0;
    s_cur.target_state = 0;
    s_cur.x_mm = 0;
    s_cur.y_mm = 0;
    s_cur.speed_cms = 0;
    s_cur.detect_dist = -1;
    s_cur.moving_dist = -1;
    s_cur.still_dist = -1;
    s_cur.protocol = 2;
    s_protocol = 2;
  }
  return true;
}

// ===================== 收帧状态机 =====================
static void resetFrame() { s_len = 0; s_kind = 0; }

static void markOK() {
  s_cur.valid = true;
  s_cur.last_update_ms = millis();
  s_frames_ok++;
}

static void feedByte(uint8_t b) {
  if (s_len == 0) {
    if (b == LD2410_DATA_HEAD[0]) { s_buf[0] = b; s_len = 1; s_kind = 2; }
    else if (b == LD2410_CMD_HEAD[0]) { s_buf[0] = b; s_len = 1; s_kind = 1; }
    else if (b == LD2450_DATA_HEAD[0]) { s_buf[0] = b; s_len = 1; s_kind = 3; }
    return;
  }

  if (s_len >= sizeof(s_buf)) { resetFrame(); return; }
  s_buf[s_len++] = b;

  const uint8_t *head = (s_kind == 3) ? LD2450_DATA_HEAD
                      : (s_kind == 2) ? LD2410_DATA_HEAD : LD2410_CMD_HEAD;

  if (s_len <= 4) {
    if (s_buf[s_len - 1] != head[s_len - 1]) {
      resetFrame();
      if (b == LD2410_DATA_HEAD[0]) { s_buf[0] = b; s_len = 1; s_kind = 2; }
      else if (b == LD2410_CMD_HEAD[0]) { s_buf[0] = b; s_len = 1; s_kind = 1; }
      else if (b == LD2450_DATA_HEAD[0]) { s_buf[0] = b; s_len = 1; s_kind = 3; }
    }
    return;
  }

  if (s_len < 6) return;
  uint16_t body_len = le16(&s_buf[4]);

  if (s_kind == 3) {
    // LD2450：**固定 30 字节**（AA FF 03 00 | 24B 数据 | 55 CC），**无长度字段**
    const uint16_t total = 30;
    if (s_len < total) return;
    if (memcmp(&s_buf[total - 2], LD2450_DATA_TAIL, 2) != 0) {
      s_frames_bad++;
      // 帧尾不符：可能 0xAA 出现在数据区导致误同步 → 丢 1 字节重新找帧头
      memmove(s_buf, s_buf + 1, s_len - 1);
      s_len--;
      return;
    }
    if (parseLD2450(&s_buf[4], 24)) markOK();   // 数据区从第 4 字节开始
    resetFrame();
    return;
  }

  uint16_t total = 4 + 2 + body_len + 4;          // LD2410：尾 4 字节
  if (total > sizeof(s_buf) || body_len == 0) { s_frames_bad++; resetFrame(); return; }
  if (s_len < total) return;
  const uint8_t *tail = (s_kind == 2) ? LD2410_DATA_TAIL : LD2410_CMD_TAIL;
  if (memcmp(&s_buf[total - 4], tail, 4) != 0) { s_frames_bad++; resetFrame(); return; }

  if (s_kind == 2) {
    if (parseLD2410(&s_buf[6], (uint8_t)body_len)) markOK();
  } else {
    s_cmd_frames++;         // 命令响应：双向通信正常
  }
  resetFrame();
}

// ===================== 波特率 =====================
static void applyBaud(uint8_t idx) {
  s_baud_idx = idx % BAUD_COUNT;
  Serial1.end();
  delay(20);
  Serial1.setRxBufferSize(2048);
  Serial1.begin(BAUD_CANDIDATES[s_baud_idx], SERIAL_8N1, MMWAVE_RX_PIN, MMWAVE_TX_PIN);
  s_rx_bytes = 0;
  s_len = 0;
  s_kind = 0;
  s_frames_bad = 0;
  s_cmd_frames = 0;
  s_raw_n = 0;
  s_raw_ready = false;

  // LD2410 认这两条命令；LD2450 不响应但无害
  sendRaw(CMD_EXIT_CONFIG, sizeof(CMD_EXIT_CONFIG));
  delay(30);
  sendRaw(CMD_READ_VERSION, sizeof(CMD_READ_VERSION));

  s_switch_at = millis() + 8000;
  Serial.printf("[毫米波] 尝试波特率 %u（第 %u 次）\n",
                (unsigned)BAUD_CANDIDATES[s_baud_idx], s_baud_tries + 1);
  s_baud_tries++;
}

// ===================== 对外接口 =====================

void mmwaveInit() {
  if (s_inited) return;
  s_inited = true;

  memset(&s_cur, 0, sizeof(s_cur));
  s_cur.person = -1;
  s_cur.moving_dist = -1;
  s_cur.still_dist = -1;
  s_cur.detect_dist = -1;
  s_cur.valid = false;

  Serial1.setRxBufferSize(2048);
  Serial1.begin(MMWAVE_BAUD, SERIAL_8N1, MMWAVE_RX_PIN, MMWAVE_TX_PIN);

  if (MMWAVE_OUT_PIN >= 0) pinMode(MMWAVE_OUT_PIN, INPUT);
  s_cur.out_pin = (MMWAVE_OUT_PIN >= 0) ? (digitalRead(MMWAVE_OUT_PIN) == HIGH) : false;

  s_switch_at = millis() + 8000;
  s_baud_idx = 0;
  s_baud_tries = 1;

  sendRaw(CMD_EXIT_CONFIG, sizeof(CMD_EXIT_CONFIG));
  delay(30);
  sendRaw(CMD_READ_VERSION, sizeof(CMD_READ_VERSION));

  Serial.printf("[毫米波] UART1 就绪（%d baud, TX=%d RX=%d，%s），OUT=%d"
                "（LD2410C / LD2450 双协议自动识别）\n",
                MMWAVE_BAUD, MMWAVE_TX_PIN, MMWAVE_RX_PIN,
                MMWAVE_OUT_PIN >= 0 ? "OUT 已接" : "OUT 未接",
                s_cur.out_pin ? 1 : 0);
}

void mmwavePoll() {
  if (!s_inited) return;

  int avail = Serial1.available();
  if (avail > 0) {
    int budget = 512;
    while (avail-- > 0 && budget-- > 0) {
      int b = Serial1.read();
      if (b < 0) break;
      s_rx_bytes++;
      if (s_raw_n < sizeof(s_raw)) {
        s_raw[s_raw_n++] = (uint8_t)b;
        if (s_raw_n == sizeof(s_raw)) s_raw_ready = true;
      }
      feedByte((uint8_t)b);
    }
  }

  if (MMWAVE_OUT_PIN >= 0) s_cur.out_pin = (digitalRead(MMWAVE_OUT_PIN) == HIGH);

  // 自动波特率：有字节但一直无有效帧 → 换候选（rx>8 加快探测）
  if (!s_cur.valid && s_rx_bytes > 8 && millis() > s_switch_at && s_baud_tries < BAUD_COUNT) {
    applyBaud(s_baud_idx + 1);
  }
}

MmwaveReading mmwaveGet() {
  return s_cur;
}

bool mmwaveReady() {
  return s_cur.valid;
}

MmwaveStats mmwaveStats() {
  MmwaveStats s;
  s.rx_bytes = s_rx_bytes;
  s.frames_ok = s_frames_ok;
  s.frames_bad = s_frames_bad;
  s.cmd_frames = s_cmd_frames;
  s.baud = BAUD_CANDIDATES[s_baud_idx];
  s.baud_tries = s_baud_tries;
  s.protocol = s_protocol;
  s.inited = s_inited;
  return s;
}

size_t mmwaveGetRawHex(char *out, size_t cap) {
  if (!out || cap < 3) return 0;
  uint8_t n = s_raw_ready ? (uint8_t)sizeof(s_raw) : s_raw_n;
  if (n == 0) return 0;
  size_t pos = 0;
  static const char HEXCH[] = "0123456789abcdef";
  for (uint8_t i = 0; i < n && pos + 2 < cap; i++) {
    out[pos++] = HEXCH[(s_raw[i] >> 4) & 0x0F];
    out[pos++] = HEXCH[s_raw[i] & 0x0F];
  }
  out[pos] = '\0';
  return pos;
}

#else   // MMWAVE_ENABLED == 0

void mmwaveInit() {}
void mmwavePoll() {}
MmwaveReading mmwaveGet() {
  MmwaveReading r;
  memset(&r, 0, sizeof(r));
  r.person = -1; r.moving_dist = -1; r.still_dist = -1; r.detect_dist = -1;
  return r;
}
bool mmwaveReady() { return false; }
MmwaveStats mmwaveStats() {
  MmwaveStats s;
  memset(&s, 0, sizeof(s));
  return s;
}
size_t mmwaveGetRawHex(char *out, size_t cap) { (void)out; (void)cap; return 0; }

#endif  // MMWAVE_ENABLED

/*
 * ESP-NOW 无线内网 — 共享协议定义（5.4）
 * =====================================
 * 本头文件被「节点固件/网关固件」共同包含（header-only，static inline，无需编译成库）。
 * 与服务器侧 Python 实现（server/deploy_scripts/espnow_proto.py）严格一致，改协议要同步两边。
 *
 * 一、空口帧（ESP-NOW payload，≤250B）
 *   magic[2]  0xAA 0x55
 *   ver u8    0x01
 *   type u8   0x01 DATA(节点→网关) / 0x02 ACK(网关→节点) / 0x03 CMD(网关→节点) / 0x04 HELLO(节点→网关)
 *   dev[6]    源/目的节点 MAC（6B）
 *   seq u16   应用层序号（小端）
 *   flags u8  预留（bit0=需 ACK）
 *   len u16   载荷长度（小端，≤ 200）
 *   payload[len]
 *   crc16 u16 CRC16-CCITT-FALSE，覆盖 ver..payload（即跳过 magic 后到 crc 前的全部字节），小端存放
 *
 *   ACK.payload : seq_acked u16 | rssi i8 | status u8   （status: 0=OK 1=发送失败）
 *   CMD.payload : 服务器下发的 JSON 文本（节点自行解释，测试时为回显/点灯）
 *   HELLO.payload: 节点自述 JSON（id/固件版本等）
 *
 * 二、网关 ↔ Pi USB(CDC) 分帧
 *   magic[2]  0xAB 0xCD
 *   type u8   0x10 DATA_EVENT / 0x11 ACK_EVENT / 0x12 STATUS / 0x13 HELLO_EVENT / 0x20 CMD_TO_NODE
 *   len u16   载荷长度（小端）
 *   payload   type=0x10/0x11/0x13 时空口帧原文；0x12 时网关状态 JSON；0x20 时空口 CMD 帧原文
 *   crc16 u16 覆盖 type..payload，小端
 */
#ifndef ESPNOW_PROTO_H
#define ESPNOW_PROTO_H

#include <stdint.h>
#include <string.h>

#define EW_AIR_MAGIC0      0xAA
#define EW_AIR_MAGIC1      0x55
#define EW_AIR_VER         0x01

#define EW_T_DATA          0x01
#define EW_T_ACK           0x02
#define EW_T_CMD           0x03
#define EW_T_HELLO        0x04

#define EW_USB_MAGIC0      0xAB
#define EW_USB_MAGIC1      0xCD
#define EW_U_DATA_EVENT    0x10
#define EW_U_ACK_EVENT     0x11
#define EW_U_STATUS        0x12
#define EW_U_HELLO_EVENT   0x13
#define EW_U_CMD_TO_NODE   0x20

#define EW_AIR_MAX_PAYLOAD 200
#define EW_AIR_MAX_FRAME   (1 + 1 + 1 + 6 + 2 + 1 + 2 + EW_AIR_MAX_PAYLOAD + 2 + 2)  // ~220B
#define EW_USB_MAX_PAYLOAD 512
#define EW_USB_MAX_FRAME   (2 + 1 + 2 + EW_USB_MAX_PAYLOAD + 2)

// CRC16-CCITT-FALSE: poly=0x1021, init=0xFFFF, 无反射；"123456789" => 0x29B1
static inline uint16_t ew_crc16(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; b++) {
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
  }
  return crc;
}

static inline void ew_put_u16(uint8_t *p, uint16_t v) { p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF; }
static inline uint16_t ew_get_u16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }

// 组空口帧；返回帧长，0=失败（载荷过大）
static inline size_t ew_air_build(uint8_t *out, uint8_t type, const uint8_t dev[6],
                                  uint16_t seq, uint8_t flags,
                                  const uint8_t *payload, uint16_t len) {
  if (len > EW_AIR_MAX_PAYLOAD) return 0;
  size_t pos = 0;
  out[pos++] = EW_AIR_MAGIC0;
  out[pos++] = EW_AIR_MAGIC1;
  out[pos++] = EW_AIR_VER;
  out[pos++] = type;
  memcpy(out + pos, dev, 6); pos += 6;
  ew_put_u16(out + pos, seq); pos += 2;
  out[pos++] = flags;
  ew_put_u16(out + pos, len); pos += 2;
  if (len && payload) { memcpy(out + pos, payload, len); pos += len; }
  uint16_t crc = ew_crc16(out + 2, pos - 2);   // 覆盖 ver..payload
  ew_put_u16(out + pos, crc); pos += 2;
  return pos;
}

// 解析空口帧（就地不修改）；成功返回 true 并填充字段
struct EwAirFrame {
  uint8_t type;
  uint8_t dev[6];
  uint16_t seq;
  uint8_t flags;
  uint16_t len;
  const uint8_t *payload;
};

static inline bool ew_air_parse(const uint8_t *buf, size_t n, EwAirFrame *f) {
  if (n < 17) return false;
  if (buf[0] != EW_AIR_MAGIC0 || buf[1] != EW_AIR_MAGIC1) return false;
  if (buf[2] != EW_AIR_VER) return false;
  // 帧布局：0-1 magic | 2 ver | 3 type | 4-9 dev | 10-11 seq | 12 flags | 13-14 len | 15.. payload | crc
  uint16_t len = ew_get_u16(buf + 13);
  if (len > EW_AIR_MAX_PAYLOAD) return false;
  if (n != (size_t)(15 + len + 2)) return false;
  uint16_t crc_calc = ew_crc16(buf + 2, n - 4);
  if (crc_calc != ew_get_u16(buf + n - 2)) return false;
  f->type = buf[3];
  memcpy(f->dev, buf + 4, 6);
  f->seq = ew_get_u16(buf + 10);
  f->flags = buf[12];
  f->len = len;
  f->payload = buf + 15;
  return true;
}

// 组装 USB 帧；返回帧长，0=失败
static inline size_t ew_usb_build(uint8_t *out, uint8_t type, const uint8_t *payload, uint16_t len) {
  if (len > EW_USB_MAX_PAYLOAD) return 0;
  size_t pos = 0;
  out[pos++] = EW_USB_MAGIC0;
  out[pos++] = EW_USB_MAGIC1;
  out[pos++] = type;
  ew_put_u16(out + pos, len); pos += 2;
  if (len && payload) { memcpy(out + pos, payload, len); pos += len; }
  uint16_t crc = ew_crc16(out + 2, pos - 2);   // 覆盖 type..payload
  ew_put_u16(out + pos, crc); pos += 2;
  return pos;
}

// MAC → 12 位大写十六进制 ID（与服务器 ESP32_ID 格式一致）
static inline void ew_mac_to_id(const uint8_t mac[6], char out[13]) {
  static const char *H = "0123456789ABCDEF";
  for (int i = 0; i < 6; i++) {
    out[i * 2] = H[(mac[i] >> 4) & 0x0F];
    out[i * 2 + 1] = H[mac[i] & 0x0F];
  }
  out[12] = '\0';
}

#endif // ESPNOW_PROTO_H

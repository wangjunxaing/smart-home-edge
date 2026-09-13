/*
 * TTS 语音播报模块（5.3，WT3000T-MO1）— 头文件
 * ============================================
 * 依据官方库 WTSerial_V1.05（WT3000T.cpp / WTSerial.cpp）实现，协议与官方一致：
 *   帧 = 7E | 长度(2B 大端 = 数据区字节数 + 3) | 数据区 | 校验和 | EF
 *   校验和 = 长度高字节 + 长度低字节 + 数据区所有字节（取低 8 位）
 *   数据区 = 模块包头 {0x01,0x00,0x03} + 命令信息
 *   TTS 文本包：命令 {0x03,0xE8} + 1B 数据长度 + 文本字节（默认 UTF-8，每包 ≤255B）
 *   设置编码：{0x03,0xEF,0x01,coder}（0x01=ASCII 0x02=GBK 0x06=UTF-8）
 *   设置音量：{0x00,0xCC,0x01,vol}（0~30）
 * 文本支持控制标签：[m3] 艾佳女声 / [m51] 艾诚男声 / [s0..s10] 语速 / [t0..t10] 音调
 *
 * 接线：ESP32 GPIO17(TX2)→模块 Pin4 RX；GPIO16(RX2)←模块 Pin3 TX；3V3、GND 共地；SPK±接喇叭
 */
#ifndef TTS_VOICE_H
#define TTS_VOICE_H

#include <Arduino.h>

// 初始化 UART2（9600 8N1），并下发"UTF-8 编码 + 音量 25"
void ttsInit();

// 播报文本（hexStr = 文本字节的十六进制串；服务器按 UTF-8 转码下发）
void ttsSpeakHex(const char *hexStr);

// 便捷：直接播报 ASCII/UTF-8 短句（调试用，按原样字节发送）
void ttsSpeakAscii(const char *text);

// 单独设置音量（0~30）
void ttsSetVolume(uint8_t vol);

#endif // TTS_VOICE_H

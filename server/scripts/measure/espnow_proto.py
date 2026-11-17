#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP-NOW 协议库（5.4）— 与 shared/espnow_proto.h 严格一致
==========================================================
空口帧：AA 55 | ver | type | dev[6] | seq u16 | flags | len u16 | payload | crc16
USB 帧 ：AB CD | type | len u16 | payload | crc16
CRC16-CCITT-FALSE(poly=0x1021, init=0xFFFF)；"123456789" -> 0x29B1
"""
import struct

AIR_MAGIC = b'\xAA\x55'
AIR_VER = 0x01
T_DATA, T_ACK, T_CMD, T_HELLO = 0x01, 0x02, 0x03, 0x04

USB_MAGIC = b'\xAB\xCD'
U_DATA_EVENT, U_ACK_EVENT, U_STATUS, U_HELLO_EVENT, U_CMD_TO_NODE = 0x10, 0x11, 0x12, 0x13, 0x20

AIR_MAX_PAYLOAD = 200
USB_MAX_PAYLOAD = 512


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def air_build(ftype: int, dev_hex: str, seq: int, payload: bytes = b'', flags: int = 0) -> bytes:
    dev = bytes.fromhex(dev_hex)
    assert len(dev) == 6, 'dev 必须是 12 位十六进制 MAC'
    assert len(payload) <= AIR_MAX_PAYLOAD
    body = struct.pack('<BB', AIR_VER, ftype) + dev + struct.pack('<HBH', seq & 0xFFFF, flags, len(payload)) + payload
    return AIR_MAGIC + body + struct.pack('<H', crc16(body))


def air_parse(buf: bytes):
    """返回 dict 或 None（magic/crc/长度不符）"""
    if len(buf) < 18 or buf[:2] != AIR_MAGIC or buf[2] != AIR_VER:
        return None
    ftype = buf[3]
    dev = buf[4:10]
    seq, flags, plen = struct.unpack('<HBH', buf[10:15])
    if len(buf) != 15 + plen + 2 or plen > AIR_MAX_PAYLOAD:
        return None
    body = buf[2:-2]
    if crc16(body) != struct.unpack('<H', buf[-2:])[0]:
        return None
    return {'type': ftype, 'dev': dev.hex().upper(), 'seq': seq, 'flags': flags,
            'payload': buf[15:15 + plen]}


def usb_build(utype: int, payload: bytes = b'') -> bytes:
    assert len(payload) <= USB_MAX_PAYLOAD
    body = struct.pack('<BH', utype, len(payload)) + payload
    return USB_MAGIC + body + struct.pack('<H', crc16(body))


def usb_parse_iter(buf: bytes):
    """从字节流中逐个切出合法 USB 帧；返回 (帧列表, 剩余未消费字节)"""
    frames = []
    i = 0
    n = len(buf)
    while True:
        j = buf.find(USB_MAGIC, i)
        if j < 0 or j + 5 > n:
            return frames, buf[max(i, n - 1):] if j < 0 else buf[j:]
        utype = buf[j + 2]
        plen = struct.unpack('<H', buf[j + 3:j + 5])[0]
        if plen > USB_MAX_PAYLOAD:
            i = j + 1
            continue
        end = j + 5 + plen + 2
        if end > n:
            return frames, buf[j:]
        body = buf[j + 2:end - 2]
        if crc16(body) == struct.unpack('<H', buf[end - 2:end])[0]:
            frames.append({'type': utype, 'payload': buf[j + 5:j + 5 + plen]})
            i = end
        else:
            i = j + 1


def ack_build(dev_hex: str, seq: int, rssi: int, status: int = 0, out_seq: int = 0) -> bytes:
    """网关→节点 ACK 载荷：seq_acked u16 | rssi i8 | status u8"""
    return air_build(T_ACK, dev_hex, out_seq, struct.pack('<HbB', seq & 0xFFFF, rssi, status))


def ack_parse(payload: bytes):
    if len(payload) < 4:
        return None
    seq, rssi, status = struct.unpack('<HbB', payload[:4])
    return {'seq_acked': seq, 'rssi': rssi, 'status': status}


def cmd_build(dev_hex: str, seq: int, text: str) -> bytes:
    return air_build(T_CMD, dev_hex, seq, text.encode('utf-8'))


def selftest() -> bool:
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(('  [OK]   ' if cond else '  [FAIL] ') + name)
        ok = ok and cond

    chk('CRC16("123456789")==0x29B1', crc16(b'123456789') == 0x29B1)

    dev = 'A1B2C3D4E5F6'
    f = air_build(T_DATA, dev, 7, '{"lux":12}'.encode())
    p = air_parse(f)
    chk('空口帧往返解析', p is not None and p['dev'] == dev and p['seq'] == 7
        and p['payload'] == b'{"lux":12}' and p['type'] == T_DATA)

    bad = bytearray(f); bad[16] ^= 0xFF
    chk('CRC 篡改应被拒', air_parse(bytes(bad)) is None)

    g = ack_build(dev, 7, -55)
    ap = air_parse(g)
    chk('ACK 帧解析', ap is not None and ack_parse(ap['payload']) ==
        {'seq_acked': 7, 'rssi': -55, 'status': 0})

    cb = cmd_build(dev, 9, 'hello 节点')
    chk('CMD UTF-8 往返', air_parse(cb)['payload'].decode('utf-8') == 'hello 节点')

    stream = b'\x00\x01' + usb_build(U_DATA_EVENT, f) + usb_build(U_STATUS, b'{"rx":1}') + b'\xAB'
    frames, rest = usb_parse_iter(stream)
    chk('USB 流解析（含前导垃圾/尾部半帧）', len(frames) == 2
        and frames[0]['type'] == U_DATA_EVENT and frames[1]['payload'] == b'{"rx":1}' and rest == b'\xAB')

    corrupt = bytearray(usb_build(U_DATA_EVENT, f)); corrupt[6] ^= 0x01
    frames, _ = usb_parse_iter(bytes(corrupt))
    chk('USB 帧 CRC 错误应跳过', len(frames) == 0)

    return ok


if __name__ == '__main__':
    print('== espnow_proto 自检 ==')
    raise SystemExit(0 if selftest() else 1)

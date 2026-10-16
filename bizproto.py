#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
业务服(CSPKG)协议工具: 帧解析 + XXTEA(麦炳耀, include_length) + 极简 protobuf。

帧: 4 字节大端头 P = (compress&7) | ((encrypt&7)<<4) | (payloadLen<<8)
    payload = protobuf(CSPKG_nf) 的序列, 先 XXTEA 加密(encrypt=1), 若 >256 再 LZ4(compress=1)

CSPKG_nf: field1 = Head(CSHead), field2 = Body(bytes)
CSHead  : 1=CmdId(uint) 2=SeqId(uint) 3=RetCode(uint) 4=StrCmdId(str) 6=SecretKey(str)

XXTEA key(默认) = b"AA9zg1CZMnYLpA5H"
"""
import struct
import sys

DELTA = 0x9E3779B9
MASK = 0xFFFFFFFF
KEY = b"AA9zg1CZMnYLpA5H"


# ---------------- XXTEA (Ma Bingyao, include_length=1) ----------------
def _mx(s, y, z, p, e, k):
    return (((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4)) ^ ((s ^ y) + (k[(p & 3) ^ e] ^ z))) & MASK


def _to_uints(data, include_length):
    """include_length=True: 前置长度字(加密用)。False: 纯词数组(解密用)。"""
    n = len(data) >> 2
    if len(data) & 3:
        n += 1
    if include_length:
        n += 1
    v = [0] * n
    off = 1 if include_length else 0
    for i in range(len(data)):
        v[(i >> 2) + off] |= data[i] << ((i & 3) << 3)
    if include_length:
        v[0] = len(data) & MASK
    return v


def _xxtea_decrypt_ip(v, k):
    n = len(v)
    if n < 2:
        return v
    rounds = 6 + 52 // n
    total = (rounds * DELTA) & MASK
    y = v[0]
    while rounds > 0:
        e = (total >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = v[p - 1]
            v[p] = (v[p] - _mx(total, y, z, p, e, k)) & MASK
            y = v[p]
        z = v[n - 1]
        v[0] = (v[0] - _mx(total, y, z, 0, e, k)) & MASK
        y = v[0]
        total = (total - DELTA) & MASK
        rounds -= 1
    return v


def _xxtea_encrypt_ip(v, k):
    n = len(v)
    if n < 2:
        return v
    rounds = 6 + 52 // n
    total = 0
    z = v[n - 1]
    while rounds > 0:
        total = (total + DELTA) & MASK
        e = (total >> 2) & 3
        for p in range(0, n - 1):
            y = v[p + 1]
            v[p] = (v[p] + _mx(total, y, z, p, e, k)) & MASK
            z = v[p]
        y = v[0]
        v[n - 1] = (v[n - 1] + _mx(total, y, z, n - 1, e, k)) & MASK
        z = v[n - 1]
        rounds -= 1
    return v


def _words_to_bytes(v, n):
    out = bytearray(n * 4)
    for i in range(n):
        out[i * 4] = v[i] & 0xFF
        out[i * 4 + 1] = (v[i] >> 8) & 0xFF
        out[i * 4 + 2] = (v[i] >> 16) & 0xFF
        out[i * 4 + 3] = (v[i] >> 24) & 0xFF
    return bytes(out)


def xxtea_decrypt(data, key=KEY):
    """密文 -> 词数组(小端) -> XXTEA解密 -> 最后一个词是明文长度, 其余是明文(小端字节)。
    实测(真实抓包 CSLoginReq): 长度字在**末尾**, 词按**小端**。"""
    k = _to_uints(key, False)
    v = _to_uints(data, False)
    _xxtea_decrypt_ip(v, k)
    L = v[-1]
    body = v[:-1]
    out = bytearray(L)
    for i in range(L):
        out[i] = (body[i >> 2] >> ((i & 3) << 3)) & 0xFF
    return bytes(out)


def xxtea_encrypt(data, key=KEY):
    k = _to_uints(key, False)
    n = (len(data) + 3) >> 2
    v = [0] * (n + 1)
    for i, b in enumerate(data):
        v[i >> 2] |= b << ((i & 3) << 3)
    v[n] = len(data) & MASK             # 长度字放末尾
    _xxtea_encrypt_ip(v, k)
    return _words_to_bytes(v, len(v))


# ---------------- 帧 ----------------
def unpack_frame(buf):
    P = struct.unpack(">I", buf[:4])[0]
    compress = P & 7
    encrypt = (P >> 4) & 7
    ln = P >> 8
    return {"P": P, "compress": compress, "encrypt": encrypt, "len": ln, "payload": buf[4:4 + ln]}


def pack_frame(payload, encrypt=1, compress=0):
    P = (compress & 7) | ((encrypt & 7) << 4) | (len(payload) << 8)
    return struct.pack(">I", P) + payload


# ---------------- 极简 protobuf 解析 ----------------
def read_varint(b, i):
    r = 0
    s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if not (c & 0x80):
            return r, i
        s += 7


def pb_fields(b):
    """返回 [(field_no, wire, value)]，value 为 int 或 bytes。"""
    out = []
    i = 0
    n = len(b)
    while i < n:
        key, i = read_varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = read_varint(b, i)
        elif w == 2:
            ln, i = read_varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif w == 5:
            v = b[i:i + 4]
            i += 4
        elif w == 1:
            v = b[i:i + 8]
            i += 8
        else:
            break
        out.append((f, w, v))
    return out


def parse_cspkg(payload):
    head = None
    body = None
    for f, w, v in pb_fields(payload):
        if f == 1:
            head = parse_head(v)
        elif f == 2:
            body = v
    return head, body


def parse_head(b):
    h = {}
    for f, w, v in pb_fields(b):
        h[f] = v
    return h


HEAD_NAMES = {1: "CmdId", 2: "SeqId", 3: "RetCode", 4: "StrCmdId", 6: "SecretKey"}


def dump_frame(raw, key=KEY):
    fr = unpack_frame(raw)
    print("帧: P=0x%08X compress=%d encrypt=%d len=%d (实际 %d)"
          % (fr["P"], fr["compress"], fr["encrypt"], fr["len"], len(fr["payload"])))
    pl = fr["payload"]
    if fr["encrypt"]:
        pl = xxtea_decrypt(pl, key)
        print("XXTEA 解密后 %d 字节: %s" % (len(pl), pl.hex(" ")))
    head, body = parse_cspkg(pl)
    if head:
        print("Head: " + ", ".join("%s=%r" % (HEAD_NAMES.get(k, k), v) for k, v in head.items()))
    else:
        print("Head: (未解析出)")
    if body is not None:
        print("Body(%d): %s" % (len(body), body.hex(" ")))
    return pl, head, body


# ---------------- LZ4 (block 格式, 无帧头) 解压 ----------------
def lz4_decompress(src):
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        mlen = token & 0xF
        if mlen == 15:
            while True:
                b = src[i]; i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        for k in range(mlen):
            out.append(out[start + k])
    return bytes(out)


def decode_payload(payload, compress, encrypt, key=KEY):
    """接收侧: 先 LZ4 解压, 再 XXTEA 解密(顺序与发送相反)。"""
    if compress == 1:
        payload = lz4_decompress(payload)
    if encrypt == 1:
        payload = xxtea_decrypt(payload, key)
    return payload


# ---------------- 极简 protobuf 编码 ----------------
def write_varint(n):
    """★★ 必须能吃下负数 —— 否则死循环。

    原来的写法是 `while True: b = n & 0x7F; n >>= 7; if n: ... else: return`。
    Python 的 `>>` 对负数是**算术移位**: `-1 >> 7` 恒等于 `-1`,
    于是 `if n:` 永真 ⇒ **死循环**, 而且 `out` 字节数组无限增长。

    2026-10-15 实测事故: `1050 使用物品` 的处理器往 1052 推送里塞了
    `-1` (消耗数量), ⇒ 服务端满核卡死 + 内存涨到 5.9 GB,
    表现是「登录转圈」「网络超时」, 而**第一次使用其实已经成功了**
    (1051 回包在卡住之前就发出去了, 所以玩家看得到"获得物品",
     但收不到 1052 ⇒ 背包不刷新、数量不减)。
    py-spy 的栈里就是 `write_varint` 在空转。

    按 protobuf 惯例把负数当 **64 位补码**处理, 保证一定终止。
    (但还是别主动传负数 —— `CSGetItem.Count` 是 uint32, 见 build_item_notify。)
    """
    n = int(n)
    if n < 0:
        n &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_v(field, val):
    """varint 字段"""
    return write_varint((field << 3) | 0) + write_varint(int(val))


def pb_b(field, data):
    """length-delimited 字段"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return write_varint((field << 3) | 2) + write_varint(len(data)) + data


def build_head(cmd_id, seq_id, ret_code=0, str_cmd_id=b"", secret_key=b""):
    b = pb_v(1, cmd_id) + pb_v(2, seq_id) + pb_v(3, ret_code)
    if str_cmd_id:
        b += pb_b(4, str_cmd_id)
    if secret_key:
        b += pb_b(6, secret_key)
    return b


def build_cspkg(cmd_id, seq_id, body=b"", ret_code=0):
    head = build_head(cmd_id, seq_id, ret_code)
    return pb_b(1, head) + pb_b(2, body)


def build_reply(req_cmd, req_seq, body=b"", ret_code=0, res_cmd=None):
    """按请求构造回复帧: res_cmd 默认 = req_cmd+1, seqId 回填。返回线上字节。"""
    if res_cmd is None:
        res_cmd = req_cmd + 1
    pkg = build_cspkg(res_cmd, req_seq, body, ret_code)
    return pack_frame(xxtea_encrypt(pkg), encrypt=1, compress=0)


if __name__ == "__main__":
    # 从命令行读 hex 或文件
    data = sys.argv[1]
    if data.endswith(".hex") or data.endswith(".txt"):
        raw = bytes.fromhex(open(data).read().replace("\n", " ").replace("#", " "))
    else:
        raw = bytes.fromhex(data.replace(" ", ""))
    dump_frame(raw)

"""AES-128-CBC decryption in pure Python (for HLS #EXT-X-KEY METHOD=AES-128).

Table-driven (T-tables), so a few MiB/s in CPython: enough for music segments.
Verified against FIPS-197 / NIST SP 800-38A vectors in tests.
"""
import struct

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")
_INV = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _INV[_v] = _i
_INV = bytes(_INV)


def _xt(a):
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


# Decryption T-tables: InvMixColumns(InvSubBytes) combined, one per byte position.
_TD = [[0] * 256 for _ in range(4)]
for _x in range(256):
    _s = _INV[_x]
    _w = (_mul(_s, 14) << 24) | (_mul(_s, 9) << 16) | (_mul(_s, 13) << 8) | _mul(_s, 11)
    for _k in range(4):
        _TD[_k][_x] = ((_w >> (8 * _k)) | (_w << (32 - 8 * _k))) & 0xFFFFFFFF
_TD0, _TD1, _TD2, _TD3 = _TD
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _expand(key):
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes")
    w = list(struct.unpack(">4I", key))
    for i in range(4, 44):
        t = w[i - 1]
        if i % 4 == 0:
            t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
            t = (_SBOX[t >> 24] << 24) | (_SBOX[(t >> 16) & 0xFF] << 16) | \
                (_SBOX[(t >> 8) & 0xFF] << 8) | _SBOX[t & 0xFF]
            t ^= _RCON[i // 4 - 1] << 24
        w.append(w[i - 4] ^ t)
    rounds = [w[4 * r:4 * r + 4] for r in range(11)]
    # Equivalent inverse cipher: middle round keys go through InvMixColumns.
    dec = [rounds[10]]
    for r in range(9, 0, -1):
        dec.append([_TD0[_SBOX[k >> 24]] ^ _TD1[_SBOX[(k >> 16) & 0xFF]] ^
                    _TD2[_SBOX[(k >> 8) & 0xFF]] ^ _TD3[_SBOX[k & 0xFF]] for k in rounds[r]])
    dec.append(rounds[0])
    return dec


class AES128:
    def __init__(self, key):
        self._rk = _expand(key)

    def decrypt_block(self, block):
        rk = self._rk
        k = rk[0]
        s0, s1, s2, s3 = struct.unpack(">4I", block)
        s0 ^= k[0]; s1 ^= k[1]; s2 ^= k[2]; s3 ^= k[3]
        td0, td1, td2, td3 = _TD0, _TD1, _TD2, _TD3
        for r in range(1, 10):
            k = rk[r]
            t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 255] ^ td2[(s2 >> 8) & 255] ^ td3[s1 & 255] ^ k[0]
            t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 255] ^ td2[(s3 >> 8) & 255] ^ td3[s2 & 255] ^ k[1]
            t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 255] ^ td2[(s0 >> 8) & 255] ^ td3[s3 & 255] ^ k[2]
            t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 255] ^ td2[(s1 >> 8) & 255] ^ td3[s0 & 255] ^ k[3]
            s0, s1, s2, s3 = t0, t1, t2, t3
        k = rk[10]
        inv = _INV
        return bytes((
            inv[s0 >> 24] ^ (k[0] >> 24), inv[(s3 >> 16) & 255] ^ ((k[0] >> 16) & 255),
            inv[(s2 >> 8) & 255] ^ ((k[0] >> 8) & 255), inv[s1 & 255] ^ (k[0] & 255),
            inv[s1 >> 24] ^ (k[1] >> 24), inv[(s0 >> 16) & 255] ^ ((k[1] >> 16) & 255),
            inv[(s3 >> 8) & 255] ^ ((k[1] >> 8) & 255), inv[s2 & 255] ^ (k[1] & 255),
            inv[s2 >> 24] ^ (k[2] >> 24), inv[(s1 >> 16) & 255] ^ ((k[2] >> 16) & 255),
            inv[(s0 >> 8) & 255] ^ ((k[2] >> 8) & 255), inv[s3 & 255] ^ (k[2] & 255),
            inv[s3 >> 24] ^ (k[3] >> 24), inv[(s2 >> 16) & 255] ^ ((k[3] >> 16) & 255),
            inv[(s1 >> 8) & 255] ^ ((k[3] >> 8) & 255), inv[s0 & 255] ^ (k[3] & 255),
        ))

    def decrypt_cbc(self, data, iv, unpad=True):
        if len(data) % 16:
            raise ValueError("ciphertext is not a whole number of blocks")
        n = len(data) // 4
        words = struct.unpack(f">{n}I", data)
        out = [0] * n
        p0, p1, p2, p3 = struct.unpack(">4I", iv)
        rk = self._rk
        k0, k10 = rk[0], rk[10]
        mid = rk[1:10]
        td0, td1, td2, td3, inv = _TD0, _TD1, _TD2, _TD3, _INV
        a0, a1, a2, a3 = k10
        for i in range(0, n, 4):
            c0, c1, c2, c3 = words[i], words[i + 1], words[i + 2], words[i + 3]
            s0 = c0 ^ k0[0]; s1 = c1 ^ k0[1]; s2 = c2 ^ k0[2]; s3 = c3 ^ k0[3]
            for k in mid:
                s0, s1, s2, s3 = (
                    td0[s0 >> 24] ^ td1[(s3 >> 16) & 255] ^ td2[(s2 >> 8) & 255] ^ td3[s1 & 255] ^ k[0],
                    td0[s1 >> 24] ^ td1[(s0 >> 16) & 255] ^ td2[(s3 >> 8) & 255] ^ td3[s2 & 255] ^ k[1],
                    td0[s2 >> 24] ^ td1[(s1 >> 16) & 255] ^ td2[(s0 >> 8) & 255] ^ td3[s3 & 255] ^ k[2],
                    td0[s3 >> 24] ^ td1[(s2 >> 16) & 255] ^ td2[(s1 >> 8) & 255] ^ td3[s0 & 255] ^ k[3])
            out[i] = ((inv[s0 >> 24] << 24) | (inv[(s3 >> 16) & 255] << 16) |
                      (inv[(s2 >> 8) & 255] << 8) | inv[s1 & 255]) ^ a0 ^ p0
            out[i + 1] = ((inv[s1 >> 24] << 24) | (inv[(s0 >> 16) & 255] << 16) |
                          (inv[(s3 >> 8) & 255] << 8) | inv[s2 & 255]) ^ a1 ^ p1
            out[i + 2] = ((inv[s2 >> 24] << 24) | (inv[(s1 >> 16) & 255] << 16) |
                          (inv[(s0 >> 8) & 255] << 8) | inv[s3 & 255]) ^ a2 ^ p2
            out[i + 3] = ((inv[s3 >> 24] << 24) | (inv[(s2 >> 16) & 255] << 16) |
                          (inv[(s1 >> 8) & 255] << 8) | inv[s0 & 255]) ^ a3 ^ p3
            p0, p1, p2, p3 = c0, c1, c2, c3
        out = bytearray(struct.pack(f">{n}I", *out))
        if unpad and out:
            pad = out[-1]
            if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
                del out[-pad:]
        return bytes(out)

"""QUIC draft-18 (0xff000012) Initial crypto for TP-Link IPC P2P.

Matches libNetworkUtil.so (quicly + ptls_cyassl_aes256cbc):
- Initial salt: draft-18
- Suite: AES-256-CBC ``trunk`` AEAD (full blocks CBC; remainder = bitwise NOT)
- tag_size = 0 (ciphertext length == plaintext length; AAD ignored)
- Header protection: AES-256-ECB(sample) mask (CBC cipher init(sample)+encrypt zeros)
- CID nibble encoding; length is QUIC varint; SEND_PN_SIZE = 2
"""

from __future__ import annotations

import os
import struct
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

INITIAL_SALT = bytes.fromhex("ef4fb0abb47470c41befcf8031334fae485e09a0")
QUIC_VERSION = 0xFF000012
SEND_PN_SIZE = 2
MAX_PN_SIZE = 4


def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    h = hmac.HMAC(salt, hashes.SHA256(), backend=default_backend())
    h.update(ikm)
    return h.finalize()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out = b""
    t = b""
    i = 1
    while len(out) < length:
        h = hmac.HMAC(prk, hashes.SHA256(), backend=default_backend())
        h.update(t + info + bytes([i]))
        t = h.finalize()
        out += t
        i += 1
    return out[:length]


def hkdf_expand_label(
    secret: bytes,
    label: bytes,
    context: bytes,
    length: int,
    prefix: bytes = b"tls13 ",
) -> bytes:
    full = prefix + label
    info = (
        struct.pack("!H", length)
        + bytes([len(full)])
        + full
        + bytes([len(context)])
        + context
    )
    return hkdf_expand(secret, info, length)


def derive_initial_secrets(dcid: bytes) -> tuple[bytes, bytes]:
    """Return (client_secret, server_secret) for Initial keys."""
    initial = hkdf_extract(INITIAL_SALT, dcid)
    client = hkdf_expand_label(initial, b"client in", b"", 32)
    server = hkdf_expand_label(initial, b"server in", b"", 32)
    return client, server


def derive_traffic_keys(secret: bytes) -> tuple[bytes, bytes, bytes]:
    """Return (aead_key_32, iv_16, hp_key_32)."""
    key = hkdf_expand_label(secret, b"key", b"", 32, prefix=b"tls13 quic ")
    iv = hkdf_expand_label(secret, b"iv", b"", 16, prefix=b"tls13 quic ")
    hp = hkdf_expand_label(secret, b"quic hp", b"", 32, prefix=b"tls13 ")
    return key, iv, hp


def build_iv(static_iv: bytes, packet_number: int) -> bytes:
    iv = bytearray(static_iv)
    for i in range(8):
        iv[8 + i] ^= (packet_number >> (56 - 8 * i)) & 0xFF
    return bytes(iv)


def aes256_trunk_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-256-CBC for full 16-byte blocks; remainder = bitwise NOT."""
    out = bytearray()
    prev = bytes(iv)
    nblocks = len(data) // 16
    for i in range(nblocks):
        block = bytearray(data[i * 16 : (i + 1) * 16])
        for j in range(16):
            block[j] ^= prev[j]
        enc = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).encryptor()
        ct = enc.update(bytes(block)) + enc.finalize()
        out.extend(ct)
        prev = ct
    rem = data[nblocks * 16 :]
    if rem:
        out.extend(bytes((~b) & 0xFF for b in rem))
    return bytes(out)


def aes256_trunk_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    out = bytearray()
    prev = bytes(iv)
    nblocks = len(data) // 16
    for i in range(nblocks):
        block = data[i * 16 : (i + 1) * 16]
        dec = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).decryptor()
        plain = bytearray(dec.update(block) + dec.finalize())
        for j in range(16):
            plain[j] ^= prev[j]
        out.extend(plain)
        prev = block
    rem = data[nblocks * 16 :]
    if rem:
        out.extend(bytes((~b) & 0xFF for b in rem))
    return bytes(out)


def hp_mask(hp_key: bytes, sample: bytes) -> bytes:
    """AES-256-ECB(sample) — equivalent to CBC init(sample) + encrypt(zeros)."""
    enc = Cipher(algorithms.AES(hp_key), modes.ECB(), backend=default_backend()).encryptor()
    return enc.update(sample[:16]) + enc.finalize()


def cid_len_from_nibble(nibble: int) -> int:
    return 0 if nibble == 0 else nibble + 3


def nibble_from_cid_len(length: int) -> int:
    if length == 0:
        return 0
    if length < 3 or length > 18:
        raise ValueError(f"unsupported CID length {length}")
    return length - 3


def read_varint(buf: bytes, off: int) -> tuple[int, int]:
    b = buf[off]
    length = 1 << (b >> 6)
    raw = int.from_bytes(buf[off : off + length], "big")
    mask = (1 << (length * 8 - 2)) - 1
    return raw & mask, off + length


def write_varint(value: int) -> bytes:
    if value < 0x40:
        return bytes([value])
    if value < 0x4000:
        return struct.pack("!H", 0x4000 | value)
    if value < 0x40000000:
        return struct.pack("!I", 0x80000000 | value)
    return struct.pack("!Q", 0xC000000000000000 | value)


def parse_long_header(pkt: bytes) -> dict[str, Any]:
    if len(pkt) < 7 or (pkt[0] >> 7) == 0:
        raise ValueError("not a long header")
    ver = struct.unpack_from("!I", pkt, 1)[0]
    dcil = cid_len_from_nibble(pkt[5] >> 4)
    scil = cid_len_from_nibble(pkt[5] & 0xF)
    i = 6
    dcid = pkt[i : i + dcil]
    i += dcil
    scid = pkt[i : i + scil]
    i += scil
    ptype = (pkt[0] >> 4) & 3
    token = b""
    if ptype == 0:
        token_len, i = read_varint(pkt, i)
        token = pkt[i : i + token_len]
        i += token_len
    length, i = read_varint(pkt, i)
    return {
        "first": pkt[0],
        "version": ver,
        "type": ptype,
        "dcid": dcid,
        "scid": scid,
        "token": token,
        "enc_off": i,
        "length": length,
        "packet": pkt[: i + length],
    }


def decrypt_initial(
    pkt: bytes,
    *,
    is_client: bool,
    secrets_dcid: bytes | None = None,
    pn_length: int = SEND_PN_SIZE,
) -> dict[str, Any]:
    """Decrypt an Initial packet. ``secrets_dcid`` defaults to packet DCID
    (client Initial) or must be the client's chosen DCID for server Initial
    with empty DCID.
    """
    info = parse_long_header(pkt)
    if info["type"] != 0:
        raise ValueError("not an Initial packet")
    dcid = secrets_dcid if secrets_dcid is not None else info["dcid"]
    client_sec, server_sec = derive_initial_secrets(dcid)
    secret = client_sec if is_client else server_sec
    key, iv, hp = derive_traffic_keys(secret)

    raw = info["packet"]
    enc_off = info["enc_off"]
    length = info["length"]
    sample = raw[enc_off + MAX_PN_SIZE : enc_off + MAX_PN_SIZE + 16]
    mask = hp_mask(hp, sample)

    first = raw[0] ^ (mask[0] & 0x0F)
    pn_bytes = bytearray(raw[enc_off : enc_off + pn_length])
    for i in range(pn_length):
        pn_bytes[i] ^= mask[1 + i]
    pn = int.from_bytes(pn_bytes, "big")

    aead = raw[enc_off + pn_length : enc_off + length]
    plain = aes256_trunk_decrypt(key, build_iv(iv, pn), aead)
    return {
        **info,
        "first_unprotected": first,
        "packet_number": pn,
        "plaintext": plain,
        "key": key,
        "iv": iv,
        "hp": hp,
        "secrets_dcid": dcid,
    }


def build_client_initial(
    dcid: bytes,
    plaintext: bytes,
    *,
    packet_number: int = 0,
    scid: bytes = b"",
    first_byte: int = 0xCD,
) -> bytes:
    """Build a protected client Initial (empty token).

    ``first_byte`` before HP: long header with PN-length bits = 1 → 2-byte PN
    (``0xCD``). App capture uses this; ``0xCF`` claims 4-byte PN and is rejected.
    """
    if len(dcid) == 0:
        raise ValueError("client Initial needs a non-empty DCID")
    client_sec, _ = derive_initial_secrets(dcid)
    key, iv, hp = derive_traffic_keys(client_sec)

    dcil_n = nibble_from_cid_len(len(dcid))
    scil_n = nibble_from_cid_len(len(scid))
    hdr = bytearray()
    hdr.append(first_byte)
    hdr += struct.pack("!I", QUIC_VERSION)
    hdr.append((dcil_n << 4) | scil_n)
    hdr += dcid
    hdr += scid
    hdr.append(0)  # empty token

    length = SEND_PN_SIZE + len(plaintext)
    hdr += write_varint(length)
    enc_off = len(hdr)

    body = bytearray(SEND_PN_SIZE + len(plaintext))
    body[:SEND_PN_SIZE] = struct.pack("!H", packet_number & 0xFFFF)
    body[SEND_PN_SIZE:] = aes256_trunk_encrypt(
        key, build_iv(iv, packet_number), plaintext
    )
    packet = bytearray(hdr) + body

    sample = bytes(packet[enc_off + MAX_PN_SIZE : enc_off + MAX_PN_SIZE + 16])
    mask = hp_mask(hp, sample)
    packet[0] ^= mask[0] & 0x0F
    for i in range(SEND_PN_SIZE):
        packet[enc_off + i] ^= mask[1 + i]
    return bytes(packet)


def crypto_frame(offset: int, data: bytes) -> bytes:
    """Build a QUIC CRYPTO frame (type 0x06)."""
    return (
        bytes([0x06])
        + write_varint(offset)
        + write_varint(len(data))
        + data
    )


def pad_initial_payload(frames: bytes, min_payload: int = 1162) -> bytes:
    """Pad with 0x00 so UDP Initial is typically ≥1200 bytes."""
    if len(frames) >= min_payload:
        return frames
    return frames + bytes(min_payload - len(frames))


def extract_crypto_tls(plaintext: bytes) -> bytes | None:
    """Return TLS message bytes from the first CRYPTO frame, if present.

    Skips PADDING (0x00) and ACK (0x02) frames that often precede CRYPTO
    on server Initials.
    """
    i = 0
    while i < len(plaintext):
        if plaintext[i] == 0x00:
            i += 1
            continue
        ftype = plaintext[i]
        i += 1
        if ftype == 0x06:  # CRYPTO
            _off, i = read_varint(plaintext, i)
            length, i = read_varint(plaintext, i)
            return plaintext[i : i + length]
        if ftype == 0x02:  # ACK
            for _ in range(4):
                _, i = read_varint(plaintext, i)
            continue
        return None
    return None


def is_valid_server_hello(tls: bytes | None) -> bool:
    """True if ``tls`` looks like a TLS ServerHello (type 2, legacy 0x0303)."""
    if not tls or len(tls) < 6 or tls[0] != 0x02:
        return False
    return tls[4:6] == b"\x03\x03"


def freshen_client_hello(ch: bytes, *, also_key_share: bool = True) -> bytes:
    """Return a same-sized ClientHello with new Random / SessionID (/ key_share).

    TP-Link App CH uses cipher ``0x003d`` and a stub secp256r1 key_share
    (9 bytes). We keep framing/extensions and only refresh entropy fields so
    a new DCID is not tied to a replayed CH random.
    """
    if len(ch) < 39 or ch[0] != 0x01:
        raise ValueError("not a ClientHello")
    body_len = (ch[1] << 16) | (ch[2] << 8) | ch[3]
    if body_len != len(ch) - 4:
        raise ValueError("ClientHello length mismatch")
    body = bytearray(ch[4:])
    if body[0:2] != b"\x03\x03":
        raise ValueError("unexpected legacy_version")
    # Random at body[2:34]
    body[2:34] = os.urandom(32)
    i = 34
    sid_len = body[i]
    i += 1
    if sid_len:
        body[i : i + sid_len] = os.urandom(sid_len)
        i += sid_len
    cs_len = (body[i] << 8) | body[i + 1]
    i += 2 + cs_len
    comp_len = body[i]
    i += 1 + comp_len
    ext_len = (body[i] << 8) | body[i + 1]
    i += 2
    exts = bytearray(body[i : i + ext_len])
    if also_key_share:
        j = 0
        while j + 4 <= len(exts):
            et = (exts[j] << 8) | exts[j + 1]
            el = (exts[j + 2] << 8) | exts[j + 3]
            j += 4
            if et == 0x0033 and el >= 8:  # key_share
                # Keep group/lengths; refresh opaque key_exchange bytes.
                # Layout: list_len(2) group(2) ke_len(2) ke_bytes
                ke_len = (exts[j + 4] << 8) | exts[j + 5]
                if 6 + ke_len <= el and ke_len > 0:
                    exts[j + 6 : j + 6 + ke_len] = os.urandom(ke_len)
                j += el
                break
            j += el
    body[i : i + ext_len] = exts
    return bytes([0x01, ch[1], ch[2], ch[3]]) + bytes(body)


def build_initial_payload_from_ch(ch: bytes, min_payload: int = 1245) -> bytes:
    """CRYPTO(offset=0) + PADDING to ``min_payload`` (App Initial size)."""
    return pad_initial_payload(crypto_frame(0, ch), min_payload=min_payload)


def new_dcid(length: int = 8) -> bytes:
    return os.urandom(length)


# --- X9.62 stub key exchange (ptls_cyassl_secp256r1 in libNetworkUtil) ---
# Not real ECDH: 9-byte "public keys", modulus 0x224b. App always uses the
# same wire key_shares; shared uint is constant 1685 for those pubs.
X962_MODULUS = 0x224B
X962_CLIENT_PUB = 0x0BD5
X962_SERVER_PUB = 0x0533
X962_CLIENT_PRIV = 5
X962_SHARED_UINT = 1685  # pow(0x533,5)%MOD == pow(0x0BD5,3)%MOD


def x962_shared_secret_bytes(*, encoding: str = "le9") -> bytes:
    """Encode the stub DH shared secret (default: 8-byte LE + trailing NUL)."""
    n = X962_SHARED_UINT
    if encoding == "le9":
        return n.to_bytes(8, "little") + b"\x00"
    if encoding == "le8":
        return n.to_bytes(8, "little")
    if encoding == "le32":
        return n.to_bytes(32, "little")
    if encoding == "be32":
        return n.to_bytes(32, "big")
    raise ValueError(f"unknown encoding {encoding}")


def split_coalesced_long_headers(datagram: bytes) -> list[bytes]:
    """Split a UDP datagram into draft-18 long-header packets."""
    out: list[bytes] = []
    off = 0
    while off < len(datagram) and (datagram[off] >> 7) == 1:
        info = parse_long_header(datagram[off:])
        total = info["enc_off"] + info["length"]
        out.append(datagram[off : off + total])
        off += total
    if off < len(datagram):
        out.append(datagram[off:])
    return out

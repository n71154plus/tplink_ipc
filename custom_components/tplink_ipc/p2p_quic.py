"""Guestroom / IPC44GW P2P media: QUIC after punch (StunVer2).

Wire format RE (2026-07-20) from PCAPdroid + libNetworkUtil.so (quicly):

1. Cloud ``p2p.request_p2p`` with ``version=2.0``, ``app_imp_version=4``
2. Poll ``is_p2p_ready`` until ``pri_port``
3. ASCII punch token (MAC+0x10)
4. QUIC version ``0xff000012`` (IETF draft era; quicly in libNetworkUtil)
5. CID lengths encoded in one byte as nibbles: ``len = 0 if n==0 else n+3``
6. Packet types: ``0xCE`` Initial, ``0xEE`` Handshake, ``0x5E`` 1-RTT short
7. Camera often coalesces Initial+Handshake in one UDP datagram
8. Application data on 1-RTT is high-entropy (TLS1.3/quicly); not TPPipe 0x80

Livingroom (IPC59AE, request_p2p v1.1) uses TPPipe ``0x80`` instead — see
``p2p_stream.py``.

aioquic only speaks QUICv1/v2 — cannot talk ``0xff000012``. Initial AEAD is
TP-Link ``AES256-CBC`` trunk (see ``p2p_quic_crypto.py``).

**Callable replay API** (keys not derived yet — payloads stay opaque):

- :func:`load_quic_replay_bundle` — load canned session packets
- :class:`TPLinkQuicReplaySession` — punch → Initial → HS → 1-RTT → collect
- :func:`collect_quic_replay_media` — one-shot convenience
"""

from __future__ import annotations

import logging
import random
import socket
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .p2p_quic_crypto import (
    aes256_trunk_decrypt,
    build_iv,
    decrypt_initial,
    derive_initial_secrets,
    derive_traffic_keys,
    extract_crypto_tls,
    is_valid_server_hello,
    parse_long_header,
    split_coalesced_long_headers,
)
from .stream_session import TPLinkMediaStreamError

_LOGGER = logging.getLogger(__name__)

QUIC_VERSION = 0xFF000012
DEFAULT_REPLAY_BUNDLE = "session12"
_REPLAY_ROOT = Path(__file__).resolve().parent / "quic_replay"


class QuicReplayError(TPLinkMediaStreamError):
    """QUIC exact-replay session failed."""


def server_handshake_token(client_token: str) -> str:
    """App observes response MAC = first byte of device MAC + 0x10."""
    mac, rest = client_token.split("-", 1)
    first = (int(mac[:2], 16) + 0x10) & 0xFF
    return f"{first:02X}{mac[2:]}-{rest}"


def cid_len_from_nibble(nibble: int) -> int:
    """quicly draft CID length: 0 → empty; else nibble+3 (max 18)."""
    return 0 if nibble == 0 else nibble + 3


def _read_varint(buf: bytes, off: int) -> tuple[int, int]:
    b = buf[off]
    length = 1 << (b >> 6)
    raw = int.from_bytes(buf[off : off + length], "big")
    mask = (1 << (length * 8 - 2)) - 1
    return raw & mask, off + length


def classify_quic_datagram(pkt: bytes) -> list[dict[str, Any]]:
    """Split a UDP datagram into quicly long/short header packets."""
    out: list[dict[str, Any]] = []
    off = 0
    while off < len(pkt):
        p = pkt[off:]
        if len(p) < 2:
            break
        b0 = p[0]
        if ((b0 >> 7) & 1) == 0:
            out.append(
                {
                    "kind": "1RTT",
                    "b0": b0,
                    "offset": off,
                    "length": len(p),
                }
            )
            break
        if len(p) < 6:
            break
        ver = struct.unpack_from("!I", p, 1)[0]
        cidbyte = p[5]
        dcil = cid_len_from_nibble(cidbyte >> 4)
        scil = cid_len_from_nibble(cidbyte & 0xF)
        i = 6 + dcil + scil
        ptype = (b0 >> 4) & 3
        names = {0: "Initial", 1: "0RTT", 2: "Handshake", 3: "Retry"}
        kind = names.get(ptype, f"long-{ptype}")
        try:
            if ptype == 0:
                token_len, i = _read_varint(p, i)
                i += token_len
            length, i = _read_varint(p, i)
        except (IndexError, struct.error):
            out.append({"kind": "parse-error", "offset": off})
            break
        total = i + length
        if total > len(p):
            out.append(
                {
                    "kind": kind,
                    "version": ver,
                    "offset": off,
                    "error": "truncated",
                    "hdr": i,
                    "length": length,
                }
            )
            break
        out.append(
            {
                "kind": kind,
                "version": ver,
                "dcil": dcil,
                "scil": scil,
                "dcid": p[6 : 6 + dcil].hex(),
                "scid": p[6 + dcil : 6 + dcil + scil].hex(),
                "offset": off,
                "hdr": i,
                "length": length,
                "total": total,
            }
        )
        off += total
    return out


def datagram_has_server_hello(data: bytes, dcid: bytes) -> bool:
    """True if ``data`` (possibly coalesced) decrypts to a valid ServerHello."""
    try:
        parts = split_coalesced_long_headers(data)
    except Exception:
        parts = [data]
    for part in parts:
        if len(part) < 20 or (part[0] >> 4) & 3 != 0:
            continue
        try:
            info = parse_long_header(part)
            _, ssec = derive_initial_secrets(dcid)
            key, iv, _ = derive_traffic_keys(ssec)
            aead = part[info["enc_off"] + 2 : info["enc_off"] + info["length"]]
            plain = aes256_trunk_decrypt(key, build_iv(iv, 24), aead)
            if is_valid_server_hello(extract_crypto_tls(plain)):
                return True
        except Exception:
            continue
        try:
            sdec = decrypt_initial(part, is_client=False, secrets_dcid=dcid)
            if is_valid_server_hello(extract_crypto_tls(sdec["plaintext"])):
                return True
        except Exception:
            continue
    return False


def is_quic_1rtt(data: bytes) -> bool:
    """Short-header 1-RTT used by this stack (App/camera: ``0x5e``)."""
    return bool(data) and (data[0] & 0x80) == 0 and data[0] in (0x5E, 0x5C, 0x5F)


@dataclass
class QuicReplayBundle:
    """Canned client packets for one App session (must stay matched)."""

    name: str
    dcid: bytes
    client_initial: bytes
    client_handshake: bytes
    client_1rtt: tuple[bytes, ...] = ()
    client_initial_2: bytes | None = None
    path: Path | None = None

    @property
    def dcid_hex(self) -> str:
        return self.dcid.hex()


def load_quic_replay_bundle(
    name_or_dir: str | Path = DEFAULT_REPLAY_BUNDLE,
) -> QuicReplayBundle:
    """Load a replay bundle from ``quic_replay/<name>/`` or an absolute dir.

    Required files: ``client_initial.bin``, ``client_handshake.bin``.
    Optional: ``client_initial_2.bin``, ``client_1rtt_*.bin``.
    """
    root = Path(name_or_dir)
    if not root.is_absolute() and not root.exists():
        root = _REPLAY_ROOT / str(name_or_dir)
    if not root.is_dir():
        raise QuicReplayError(f"replay bundle not found: {root}")

    initial = root / "client_initial.bin"
    hs = root / "client_handshake.bin"
    if not initial.is_file() or not hs.is_file():
        raise QuicReplayError(
            f"bundle {root} needs client_initial.bin + client_handshake.bin"
        )

    init_bytes = initial.read_bytes()
    hs_bytes = hs.read_bytes()
    init2 = root / "client_initial_2.bin"
    rtt_files = sorted(root.glob("client_1rtt_*.bin"))
    try:
        dcid = decrypt_initial(init_bytes, is_client=True)["dcid"]
    except Exception as err:
        raise QuicReplayError(f"cannot decrypt client_initial: {err}") from err

    return QuicReplayBundle(
        name=root.name,
        dcid=dcid,
        client_initial=init_bytes,
        client_handshake=hs_bytes,
        client_1rtt=tuple(p.read_bytes() for p in rtt_files),
        client_initial_2=init2.read_bytes() if init2.is_file() else None,
        path=root,
    )


def list_quic_replay_bundles() -> list[str]:
    """Names of bundled replay sessions under ``quic_replay/``."""
    if not _REPLAY_ROOT.is_dir():
        return []
    return sorted(
        p.name
        for p in _REPLAY_ROOT.iterdir()
        if p.is_dir() and (p / "client_initial.bin").is_file()
    )


@dataclass
class QuicReplayResult:
    """Outcome of a replay media collect."""

    punched: bool = False
    server_hello: bool = False
    handshake_ack: bool = False
    peer: tuple[str, int] | None = None
    dcid_hex: str = ""
    server_initial: bytes | None = None
    hs_replies: list[bytes] = field(default_factory=list)
    media: list[bytes] = field(default_factory=list)
    other: list[bytes] = field(default_factory=list)

    @property
    def media_bytes(self) -> int:
        return sum(len(p) for p in self.media)

    def summary(self) -> dict[str, Any]:
        return {
            "punched": self.punched,
            "server_hello": self.server_hello,
            "handshake_ack": self.handshake_ack,
            "peer": list(self.peer) if self.peer else None,
            "dcid": self.dcid_hex,
            "hs_replies": [len(x) for x in self.hs_replies],
            "media_packets": len(self.media),
            "media_bytes": self.media_bytes,
            "other_packets": len(self.other),
            "media_sizes": [len(x) for x in self.media[:12]],
        }


class TPLinkQuicReplaySession:
    """Punch + exact-replay QUIC session (guestroom / StunVer2).

    Does **not** decrypt Handshake/1-RTT yet. After :meth:`start_1rtt` the
    camera typically emits fresh ``0x5e`` datagrams (often 1280B media).
    """

    def __init__(
        self,
        host: str,
        peer_port: int,
        punch_token: str,
        bundle: QuicReplayBundle | str | Path = DEFAULT_REPLAY_BUNDLE,
        *,
        bind_port: int = 0,
    ) -> None:
        self.host = host
        self.peer_port = int(peer_port)
        self.punch_token = punch_token.strip()
        self.bind_port = bind_port
        if isinstance(bundle, QuicReplayBundle):
            self.bundle = bundle
        else:
            self.bundle = load_quic_replay_bundle(bundle)

        self._sock: socket.socket | None = None
        self._peer: tuple[str, int] | None = None
        self.server_initial: bytes | None = None
        self.hs_replies: list[bytes] = []

    @property
    def peer(self) -> tuple[str, int] | None:
        return self._peer

    @property
    def dcid(self) -> bytes:
        return self.bundle.dcid

    def connect(self, timeout: float = 8.0) -> None:
        """UDP punch only; leaves socket open for QUIC."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.bind_port))
        sock.settimeout(0.35)
        peer = (self.host, self.peer_port)
        expect = server_handshake_token(self.punch_token).encode()
        token = self.punch_token.encode()

        deadline = time.monotonic() + timeout
        got = False
        while time.monotonic() < deadline:
            sock.sendto(token, peer)
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            if data.startswith(expect[:20]) or expect[:24] in data:
                peer = addr
                got = True
                break
        if not got:
            sock.close()
            raise QuicReplayError(
                f"P2P punch timeout {self.host}:{self.peer_port} "
                f"token={self.punch_token[:20]}…"
            )
        for _ in range(3):
            sock.sendto(expect[:40], peer)
            time.sleep(0.02)

        self._sock = sock
        self._peer = peer
        _LOGGER.debug(
            "QUIC punch ok peer=%s dcid=%s bundle=%s",
            peer,
            self.bundle.dcid_hex,
            self.bundle.name,
        )

    def _require_sock(self) -> tuple[socket.socket, tuple[str, int]]:
        if self._sock is None or self._peer is None:
            raise QuicReplayError("not connected; call connect() first")
        return self._sock, self._peer

    def _send(self, data: bytes) -> None:
        sock, peer = self._require_sock()
        sock.sendto(data, peer)

    def _recv(self) -> bytes | None:
        sock, peer = self._require_sock()
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            return None
        self._peer = addr
        return data

    def handshake_initial(self, timeout: float = 5.0) -> bytes:
        """Send canned Initial(s); return server datagram containing ServerHello."""
        sock, _peer = self._require_sock()
        init = self.bundle.client_initial
        init2 = self.bundle.client_initial_2 or init
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._send(init)
            self._send(init2)
            data = self._recv()
            if data and len(data) >= 100 and datagram_has_server_hello(data, self.dcid):
                self.server_initial = data
                _LOGGER.debug(
                    "QUIC ServerHello ok len=%s parts=%s",
                    len(data),
                    [len(p) for p in split_coalesced_long_headers(data)],
                )
                return data
        raise QuicReplayError("QUIC Initial timeout (no ServerHello)")

    def handshake_finished(self, timeout: float = 3.0, sends: int = 6) -> list[bytes]:
        """Send canned Handshake Finished; collect short Handshake ACKs."""
        self._require_sock()
        if self.server_initial is None:
            raise QuicReplayError("call handshake_initial() before handshake_finished()")
        hs = self.bundle.client_handshake
        replies: list[bytes] = []
        deadline = time.monotonic() + timeout
        sent = 0
        while time.monotonic() < deadline and sent < sends:
            self._send(hs)
            sent += 1
            data = self._recv()
            if not data:
                continue
            # Prefer Handshake long-header ACKs (~15B); ignore leftover Initial
            if (data[0] >> 4) & 3 == 2 or (len(data) <= 32 and data[0] == 0xEE):
                replies.append(data)
            elif data[0] == 0xCE and len(data) <= 32:
                # small Initial probe; keep going
                continue
            else:
                replies.append(data)
            if any(len(r) <= 32 and (r[0] >> 4) & 3 == 2 for r in replies):
                break
        self.hs_replies = replies
        if not any(len(r) <= 32 and (r[0] >> 4) & 3 == 2 for r in replies):
            raise QuicReplayError(
                f"QUIC Handshake ACK timeout (got {[len(r) for r in replies]})"
            )
        return replies

    def start_1rtt(self, packets: Iterable[bytes] | None = None) -> None:
        """Send canned (or custom) client 1-RTT datagrams once each."""
        self._require_sock()
        pkts = list(packets) if packets is not None else list(self.bundle.client_1rtt)
        if not pkts:
            raise QuicReplayError("no client 1-RTT packets in bundle")
        for pkt in pkts:
            for _ in range(2):
                self._send(pkt)
            time.sleep(0.02)

    def recv_datagrams(
        self,
        *,
        timeout: float = 4.0,
        max_packets: int = 64,
        predicate: Callable[[bytes], bool] | None = None,
    ) -> list[bytes]:
        """Receive UDP datagrams until timeout or ``max_packets``."""
        self._require_sock()
        pred = predicate or (lambda _d: True)
        out: list[bytes] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(out) < max_packets:
            data = self._recv()
            if data is None:
                continue
            # skip ASCII punch echoes
            if data[:1] in (b"7", b"8") and all(32 <= b < 127 for b in data[:16]):
                continue
            if pred(data):
                out.append(data)
        return out

    def collect_media(
        self,
        *,
        timeout: float = 4.0,
        max_packets: int = 48,
        min_size: int = 64,
    ) -> list[bytes]:
        """Collect 1-RTT datagrams that look like media (``0x5e``, size filter)."""

        def _media(d: bytes) -> bool:
            return is_quic_1rtt(d) and len(d) >= min_size

        return self.recv_datagrams(
            timeout=timeout, max_packets=max_packets, predicate=_media
        )

    def run(
        self,
        *,
        punch_timeout: float = 8.0,
        initial_timeout: float = 5.0,
        hs_timeout: float = 3.0,
        media_timeout: float = 4.0,
        max_media: int = 48,
    ) -> QuicReplayResult:
        """Full path: punch → Initial → HS → 1-RTT → collect media."""
        result = QuicReplayResult(dcid_hex=self.bundle.dcid_hex)
        try:
            self.connect(timeout=punch_timeout)
            result.punched = True
            result.peer = self._peer
            result.server_initial = self.handshake_initial(timeout=initial_timeout)
            result.server_hello = True
            result.hs_replies = self.handshake_finished(timeout=hs_timeout)
            result.handshake_ack = True
            self.start_1rtt()
            got = self.recv_datagrams(timeout=media_timeout, max_packets=max_media + 16)
            for d in got:
                if is_quic_1rtt(d) and len(d) >= 64:
                    result.media.append(d)
                elif (d[0] >> 4) & 3 == 2:
                    result.hs_replies.append(d)
                else:
                    result.other.append(d)
            return result
        finally:
            # leave open only if caller wants; run() closes
            self.close()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> TPLinkQuicReplaySession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def collect_quic_replay_media(
    host: str,
    peer_port: int,
    punch_token: str,
    *,
    bundle: QuicReplayBundle | str | Path = DEFAULT_REPLAY_BUNDLE,
    bind_port: int = 0,
    media_timeout: float = 4.0,
    max_media: int = 48,
) -> QuicReplayResult:
    """Convenience: punch + exact replay + collect opaque 1-RTT media bytes."""
    if bind_port == 0:
        bind_port = 51000 + random.randint(0, 999)
    session = TPLinkQuicReplaySession(
        host,
        peer_port,
        punch_token,
        bundle=bundle,
        bind_port=bind_port,
    )
    return session.run(media_timeout=media_timeout, max_media=max_media)


def punch_and_listen_quic(
    host: str,
    peer_port: int,
    punch_token: str,
    *,
    bind_port: int = 0,
    listen_s: float = 8.0,
) -> dict[str, Any]:
    """Punch then listen for QUIC datagrams (no crypto / no Initial send)."""
    expect = server_handshake_token(punch_token).encode()
    token = punch_token.encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", bind_port))
    sock.settimeout(0.4)
    peer = (host, int(peer_port))
    info: dict[str, Any] = {
        "bind": list(sock.getsockname()),
        "peer": list(peer),
        "punched": False,
        "datagrams": [],
    }

    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        sock.sendto(token, peer)
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        if data.startswith(expect[:20]) or expect[:24] in data:
            peer = addr
            info["punched"] = True
            info["peer"] = list(addr)
            break
    if not info["punched"]:
        sock.close()
        return info

    for _ in range(3):
        sock.sendto(expect[: len(token)], peer)
        time.sleep(0.02)

    deadline = time.monotonic() + listen_s
    while time.monotonic() < deadline and len(info["datagrams"]) < 30:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        if addr[0] != peer[0]:
            continue
        if data.startswith(expect[:2]) or data[:1] in (b"7", b"8"):
            continue
        info["datagrams"].append(
            {
                "len": len(data),
                "hex": data[:24].hex(),
                "packets": classify_quic_datagram(data),
            }
        )
    sock.close()
    kinds: dict[str, int] = {}
    for dg in info["datagrams"]:
        for pkt in dg.get("packets") or []:
            k = str(pkt.get("kind"))
            kinds[k] = kinds.get(k, 0) + 1
    info["kinds"] = kinds
    info["note"] = (
        "Use TPLinkQuicReplaySession / collect_quic_replay_media to send "
        "canned Initial after punch."
    )
    return info

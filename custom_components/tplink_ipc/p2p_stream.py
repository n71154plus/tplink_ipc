"""LAN P2P UDP tunnel (TPPipe 0x80) carrying Streamd multipart.

After cloud ``p2p.request_p2p`` returns ``sid`` + ``pri_port``:

1. ASCII punch token exchange (MAC first byte +0x10 on reply)
2. 0x8000 session handshake (peer IPv4 in LE form at offset 48)
3. 16-byte data frames wrapping POST /stream + multipart

Verified 2026-07-20 on TL-IPC59AE (livingroom): HTTP 200 Streamd + video/mp2t.
"""

from __future__ import annotations

import json
import logging
import random
import re
import socket
import struct
import time
import uuid
from typing import Any

from .stream_session import (
    CLIENT_BOUNDARY,
    TPLinkMediaStreamError,
)

_LOGGER = logging.getLogger(__name__)


def server_handshake_token(client_token: str) -> str:
    """App observes response MAC = first byte of device MAC + 0x10."""
    mac, rest = client_token.split("-", 1)
    if len(mac) < 2:
        raise ValueError(f"bad punch token: {client_token!r}")
    first = (int(mac[:2], 16) + 0x10) & 0xFF
    return f"{first:02X}{mac[2:]}-{rest}"


def _ip_to_le_u32(ip: str) -> int:
    a, b, c, d = (int(x) for x in ip.split("."))
    return a | (b << 8) | (c << 16) | (d << 24)


def _build_8000(
    *,
    local_magic: int,
    local_chan: int,
    peer_ip: str,
    peer_chan: int = 0,
    confirmed: bool = False,
    mtu: int = 1420,
    cap: int = 0x2F,
) -> bytes:
    buf = bytearray(64)
    struct.pack_into("!H", buf, 0, 0x8000)
    if peer_chan:
        struct.pack_into("!I", buf, 12, peer_chan & 0xFFFFFFFF)
    struct.pack_into("!I", buf, 16, 4)
    struct.pack_into("!I", buf, 20, 1)
    struct.pack_into("!I", buf, 24, local_magic & 0xFFFFFFFF)
    struct.pack_into("!I", buf, 28, mtu)
    struct.pack_into("!I", buf, 32, cap)
    if confirmed:
        struct.pack_into("!I", buf, 36, 0xFFFFFFFF)
    struct.pack_into("!I", buf, 40, local_chan & 0xFFFFFFFF)
    struct.pack_into("!I", buf, 48, _ip_to_le_u32(peer_ip))
    return bytes(buf)


def _parse_8000(data: bytes) -> dict[str, int]:
    if len(data) < 64 or data[0:2] != b"\x80\x00":
        raise TPLinkMediaStreamError("not a 0x8000 packet")
    return {
        "peer_ref": struct.unpack("!I", data[12:16])[0],
        "magic": struct.unpack("!I", data[24:28])[0],
        "mtu": struct.unpack("!I", data[28:32])[0],
        "cap": struct.unpack("!I", data[32:36])[0],
        "chan": struct.unpack("!I", data[40:44])[0],
    }


def _build_data_frame(
    local_magic: int,
    seq: int,
    peer_chan: int,
    payload: bytes,
    tag: int,
    flags: int = 0xC0,
) -> bytes:
    hdr = bytearray(16)
    struct.pack_into("!I", hdr, 0, local_magic & 0xFFFFFFFF)
    hdr[4] = flags & 0xFF
    hdr[5] = (seq >> 16) & 0xFF
    hdr[6] = (seq >> 8) & 0xFF
    hdr[7] = seq & 0xFF
    struct.pack_into("!I", hdr, 8, tag & 0xFFFFFFFF)
    struct.pack_into("!I", hdr, 12, peer_chan & 0xFFFFFFFF)
    return bytes(hdr) + payload


def _build_8006(seq: int, local_chan: int) -> bytes:
    buf = bytearray(20)
    struct.pack_into("!H", buf, 0, 0x8006)
    struct.pack_into("!I", buf, 4, seq & 0xFFFFFFFF)
    struct.pack_into("!I", buf, 12, local_chan & 0xFFFFFFFF)
    return bytes(buf)


def _build_8002(seq: int, local_chan: int, peer_magic: int) -> bytes:
    buf = bytearray(40)
    struct.pack_into("!H", buf, 0, 0x8002)
    struct.pack_into("!I", buf, 4, seq & 0xFFFFFFFF)
    struct.pack_into("!I", buf, 12, local_chan & 0xFFFFFFFF)
    struct.pack_into("!I", buf, 16, peer_magic & 0xFFFFFFFF)
    struct.pack_into("!I", buf, 20, 100000)
    struct.pack_into("!I", buf, 24, 50000)
    struct.pack_into("!I", buf, 28, 30)
    struct.pack_into("!I", buf, 32, 1)
    struct.pack_into("!I", buf, 36, 1000)
    return bytes(buf)


class TPLinkP2PStreamSession:
    """UDP TPPipe session that carries Streamd HTTP multipart after punch."""

    def __init__(
        self,
        host: str,
        peer_port: int,
        punch_token: str,
        *,
        client_uuid: str | None = None,
        bind_port: int = 0,
    ) -> None:
        self.host = host
        self.peer_port = int(peer_port)
        self.punch_token = punch_token.strip()
        self.client_uuid = client_uuid or str(uuid.uuid4())
        self.bind_port = bind_port
        self._sock: socket.socket | None = None
        self._peer: tuple[str, int] | None = None
        self._buf = b""
        self.session_id: str | None = None
        self._local_magic = 0
        self._local_chan = 0
        self._peer_chan = 0
        self._peer_magic = 0
        self._tx_seq = 0
        self._tick = 0
        self._rx_seqs: set[int] = set()

    def connect(self, timeout: float = 10.0) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.bind_port))
        sock.settimeout(0.4)
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
            raise TPLinkMediaStreamError(
                f"P2P punch timeout {self.host}:{self.peer_port} "
                f"token={self.punch_token[:20]}…"
            )
        for _ in range(3):
            sock.sendto(expect[: len(token)], peer)
            time.sleep(0.02)

        local_magic = random.randint(0x10000000, 0x7FFFFFFF)
        local_chan = random.randint(0x10000000, 0x7FFFFFFF)
        sock.sendto(
            _build_8000(
                local_magic=local_magic,
                local_chan=local_chan,
                peer_ip=self.host,
                confirmed=False,
            ),
            peer,
        )

        cam: dict[str, int] | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.sendto(expect[: len(token)], peer)
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            if data.startswith(b"44") or data.startswith(expect[:2]):
                continue
            if len(data) >= 64 and data[0:2] == b"\x80\x00":
                cam = _parse_8000(data)
                peer = addr
                break
        if not cam:
            sock.close()
            raise TPLinkMediaStreamError("P2P 0x8000 handshake timeout (no cam INIT)")

        sock.sendto(
            _build_8000(
                local_magic=local_magic,
                local_chan=local_chan,
                peer_ip=self.host,
                peer_chan=cam["chan"],
                confirmed=True,
            ),
            peer,
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            if len(data) >= 64 and data[0:2] == b"\x80\x00":
                break

        self._sock = sock
        self._peer = peer
        self._local_magic = local_magic
        self._local_chan = local_chan
        self._peer_chan = cam["chan"]
        self._peer_magic = cam["magic"]
        self._tx_seq = 0
        self._tick = random.randint(0x20000, 0x30000)
        self._rx_seqs = set()
        self._buf = b""

        headers = (
            "POST /stream HTTP/1.1\r\n"
            "Content-Length: -1\r\n"
            f"X-Client-UUID: {self.client_uuid}\r\n"
            "X-Client-Model: Android\r\n"
            "Connection: keep-alive\r\n"
            "Host: 127.0.0.1\r\n"
            f"Content-Type: multipart/mixed;boundary={CLIENT_BOUNDARY}\r\n"
            "\r\n"
        ).encode()
        self._send_frame(headers)

        # Wait Streamd HTTP 200 inside data frames
        http_buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._recv_payload(timeout=0.5)
            if not chunk:
                continue
            http_buf += chunk
            if b"\r\n\r\n" in http_buf and b"200" in http_buf.split(b"\r\n", 1)[0]:
                _, _, rest = http_buf.partition(b"\r\n\r\n")
                self._buf = rest
                _LOGGER.info(
                    "P2P TPPipe Streamd open via UDP %s:%s",
                    peer[0],
                    peer[1],
                )
                return
        sock.close()
        self._sock = None
        raise TPLinkMediaStreamError(
            f"P2P Streamd HTTP open failed; got {http_buf[:200]!r}"
        )

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> TPLinkP2PStreamSession:
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _send_frame(self, payload: bytes) -> None:
        if not self._sock or not self._peer:
            raise TPLinkMediaStreamError("Not connected")
        self._tx_seq += 1
        pkt = _build_data_frame(
            self._local_magic,
            self._tx_seq,
            self._peer_chan,
            payload,
            tag=self._tick,
        )
        self._local_magic = (self._local_magic + 1) & 0xFFFFFFFF
        self._tick = (self._tick + 0x2F000 + random.randint(0, 0x1000)) & 0xFFFFFFFF
        # MTU-safe: App keeps control frames small; split payload if needed
        mtu = 1200
        if len(pkt) <= mtu + 16:
            self._sock.sendto(pkt, self._peer)
            return
        # large multipart: send as-is first (LAN); fragment later if needed
        self._sock.sendto(pkt, self._peer)

    def _recv_payload(self, timeout: float = 0.5) -> bytes | None:
        if not self._sock or not self._peer:
            return None
        self._sock.settimeout(timeout)
        try:
            data, addr = self._sock.recvfrom(65535)
        except socket.timeout:
            return None
        if addr[0] != self._peer[0]:
            return None
        if not data:
            return None
        if data[0] == 0x80 and len(data) >= 2:
            mtype = struct.unpack("!H", data[0:2])[0]
            if mtype == 0x8002 and len(data) >= 8:
                rseq = struct.unpack("!I", data[4:8])[0]
                self._sock.sendto(_build_8006(rseq, self._local_chan), self._peer)
            return None
        if len(data) <= 16:
            return None
        payload = data[16:]
        rseq = (data[5] << 16) | (data[6] << 8) | data[7]
        if rseq not in self._rx_seqs:
            self._rx_seqs.add(rseq)
            self._peer_magic = (self._peer_magic + 1) & 0xFFFFFFFF
            self._sock.sendto(
                _build_8002(rseq, self._local_chan, self._peer_magic), self._peer
            )
        return payload

    def send_json(self, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode()
        part = (
            b"----client-stream-boundary--\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
            + b"\r\n"
        )
        self._send_frame(part)

    def request_playback(
        self,
        *,
        start_time: int | str,
        channels: list[int] | None = None,
        scale: str = "1/1",
        client_id: int = 2,
        event_type: list[int] | None = None,
        seq: int = 0,
    ) -> None:
        pb: dict[str, Any] = {
            "channels": channels or [0],
            "client_id": client_id,
            "scale": scale,
            "start_time": str(start_time),
            "event_type": event_type or [1],
        }
        self.send_json(
            {"type": "request", "seq": seq, "params": {"method": "get", "playback": pb}}
        )

    def request_preview(
        self,
        *,
        channels: list[int] | None = None,
        resolutions: list[str] | None = None,
        seq: int = 0,
    ) -> None:
        self.send_json(
            {
                "type": "request",
                "seq": seq,
                "params": {
                    "method": "get",
                    "preview": {
                        "channels": channels or [0],
                        "resolutions": resolutions or ["HD"],
                    },
                },
            }
        )

    def read_parts(
        self, max_bytes: int = 8_000_000, timeout: float = 15.0
    ) -> list[tuple[str, bytes]]:
        """Read multipart parts until max_bytes of video or timeout."""
        if not self._sock:
            raise TPLinkMediaStreamError("Not connected")
        deadline = time.monotonic() + timeout
        parts: list[tuple[str, bytes]] = []
        video_total = 0
        while time.monotonic() < deadline and video_total < max_bytes:
            chunk = self._recv_payload(timeout=0.5)
            if chunk:
                self._buf += chunk
            while True:
                m = re.search(br"----device-stream-boundary--\r\n", self._buf)
                if not m:
                    break
                start = m.end()
                nxt = re.search(
                    br"\r\n----device-stream-boundary--", self._buf[start:]
                )
                if not nxt:
                    break
                chunk_part = self._buf[start : start + nxt.start()]
                self._buf = self._buf[start + nxt.start() :]
                head, _, body = chunk_part.partition(b"\r\n\r\n")
                ctype = "application/octet-stream"
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-type:"):
                        ctype = line.split(b":", 1)[1].strip().decode()
                parts.append((ctype, body))
                if "mp2t" in ctype or "octet" in ctype:
                    video_total += len(body)
                if ctype.startswith("application/json"):
                    try:
                        js = json.loads(body.decode())
                        params = (js.get("params") or {}) if isinstance(js, dict) else {}
                        if params.get("session_id"):
                            self.session_id = str(params["session_id"])
                        err = params.get("error_code")
                        if err not in (0, None, "0"):
                            raise TPLinkMediaStreamError(
                                f"P2P stream error_code={err}: {js}"
                            )
                    except TPLinkMediaStreamError:
                        raise
                    except Exception:  # noqa: BLE001
                        pass
        return parts


def download_playback_mpegts_p2p(
    host: str,
    peer_port: int,
    punch_token: str,
    start_time: int | str,
    *,
    bind_port: int = 0,
    max_bytes: int = 4_000_000,
    timeout: float = 20.0,
) -> tuple[bytes, list[dict[str, Any]]]:
    """Convenience: punch + TPPipe + playback + collect MPEG-TS bytes."""
    jsons: list[dict[str, Any]] = []
    with TPLinkP2PStreamSession(
        host, peer_port, punch_token, bind_port=bind_port
    ) as sess:
        sess.request_playback(start_time=start_time)
        parts = sess.read_parts(max_bytes=max_bytes, timeout=timeout)
    video = b"".join(b for c, b in parts if "mp2t" in c or c.endswith("octet-stream"))
    for c, b in parts:
        if c.startswith("application/json"):
            try:
                jsons.append(json.loads(b.decode()))
            except Exception:  # noqa: BLE001
                pass
    if not video:
        raise TPLinkMediaStreamError(f"P2P playback produced no video; json={jsons[:3]}")
    return video, jsons

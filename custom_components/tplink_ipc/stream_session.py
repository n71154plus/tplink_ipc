"""TP-Link NVRHttpStream / Streamd multipart media session (port 8000).

Protocol (aligned with go2rtc vigi:// and pytapo media_stream):
1. POST /stok={stok}/stream on stream_port with Digest auth
   (password = TP-Link securityEncode scramble of device password)
2. Content-Type: multipart/mixed; boundary=--client-stream-boundary--
3. After HTTP 200, write JSON commands as multipart parts on the same TCP socket
4. Read multipart parts (--device-stream-boundary--) with video/mp2t + JSON
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import time
import uuid
from typing import Any

from .const import TP_ENCRYPT_A, TP_ENCRYPT_C

_LOGGER = logging.getLogger(__name__)

CLIENT_BOUNDARY = "--client-stream-boundary--"
DEVICE_BOUNDARY = "--device-stream-boundary--"


def tp_encrypt(password: str) -> str:
    a, c, b = TP_ENCRYPT_A, TP_ENCRYPT_C, password
    e = ""
    g, h, k = len(a), len(b), len(c)
    f = max(g, h)
    for p in range(f):
        if p >= g:
            n, l = ord(b[p]), 187
        elif p >= h:
            n, l = 187, ord(a[p])
        else:
            n, l = ord(b[p]), ord(a[p])
        e += c[(l ^ n) % k]
    return e


def _parse_www_auth(header: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for m in re.finditer(r'(\w+)\s*=\s*("([^"]*)"|([^,\s]*))', header):
        params[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(4)
    return params


def _digest_header(
    method: str,
    uri: str,
    username: str,
    password: str,
    challenge: dict[str, str],
    nc: str = "00000001",
) -> str:
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop", "auth")
    opaque = challenge.get("opaque")
    cnonce = uuid.uuid4().hex[:16]
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    resp = hashlib.md5(
        f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
    ).hexdigest()
    parts = [
        f'Digest username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{resp}"',
        f"qop={qop}",
        f"nc={nc}",
        f'cnonce="{cnonce}"',
        "algorithm=MD5",
    ]
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return ", ".join(parts)


def _read_http_response(sock: socket.socket, timeout: float = 10.0) -> tuple[int, dict[str, str], bytes]:
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode().lower()] = v.strip().decode()
    return status, headers, rest


class TPLinkMediaStreamError(Exception):
    """Stream session failure."""


# WebUI / APK-aligned stream & playback error codes
STREAM_ERROR_HINTS: dict[int, str] = {
    -52400: "資料傳輸錯誤 (DATATRANSCOMMONERR)",
    -52407: "本機 Streamd 無法讀取錄影（IPC 上常見於 client_id=-1；App 使用 client_id=2）",
    -52409: "儲存媒體錯誤 (DATATRANSSDCARDERR)",
    -52420: "儲存空間滿 (DATATRANSFULLERR)",
    -52421: "無錄影資料 (DATATRANSEMPTYERR)",
    -71103: "client_id 無效 (EIDINVALID) — 需已連線的回放 client",
    -71104: "當日無事件 (ENOEVENTS)",
    -71111: "client 尚未建立 socket (ECLINOTCONNECTED)",
    -71112: "參數非法 (EARGSILLEGAL)",
    -40209: "錯誤的服務埠（串流請用 8000/8800，不是 :80）",
}


def explain_stream_error(code: int | None) -> str | None:
    if code is None:
        return None
    try:
        code_i = int(code)
    except (TypeError, ValueError):
        return None
    return STREAM_ERROR_HINTS.get(code_i)


class TPLinkMediaStreamSession:
    """Synchronous NVRHttpStream session (run in executor)."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        stok: str,
        *,
        stream_port: int = 8000,
        use_stok_path: bool = True,
        connect_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.stok = stok
        self.stream_port = stream_port
        self.use_stok_path = use_stok_path
        self.connect_timeout = float(connect_timeout)
        self._sock: socket.socket | None = None
        self._buf = b""
        self.session_id: str | None = None

    @property
    def path(self) -> str:
        if self.use_stok_path and self.stok:
            return f"/stok={self.stok}/stream"
        return "/stream"

    def connect(self, timeout: float | None = None) -> None:
        timeout = self.connect_timeout if timeout is None else timeout
        path = self.path
        sock = socket.create_connection((self.host, self.stream_port), timeout)

        def write_req(auth: str | None = None) -> None:
            lines = [
                f"POST {path} HTTP/1.1",
                f"Host: {self.host}:{self.stream_port}",
                f"Content-Type: multipart/mixed; boundary={CLIENT_BOUNDARY}",
                "Content-Length: 0",
                "Connection: keep-alive",
                f"X-Client-UUID: {uuid.uuid4()}",
                "X-Client-Model: HomeAssistant",
            ]
            if auth:
                lines.append(f"Authorization: {auth}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        # Digest uses TP scramble password (vigi:// style)
        digest_pwd = tp_encrypt(self.password)
        write_req()
        status, headers, rest = _read_http_response(sock, timeout)
        if status != 401 or "www-authenticate" not in headers:
            sock.close()
            raise TPLinkMediaStreamError(
                f"Expected Digest challenge, got {status}: {headers}"
            )
        challenge = _parse_www_auth(headers["www-authenticate"])
        auth = _digest_header("POST", path, self.username, digest_pwd, challenge)
        write_req(auth)
        status, headers, rest = _read_http_response(sock, timeout)
        if status != 200:
            sock.close()
            raise TPLinkMediaStreamError(
                f"Stream open failed {status}: {headers}"
            )
        self._sock = sock
        self._buf = rest
        _LOGGER.debug(
            "Opened media stream %s:%s%s ctype=%s",
            self.host,
            self.stream_port,
            path,
            headers.get("content-type"),
        )

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> TPLinkMediaStreamSession:
        self.connect(self.connect_timeout)
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def send_json(self, obj: dict[str, Any]) -> None:
        if not self._sock:
            raise TPLinkMediaStreamError("Not connected")
        body = json.dumps(obj, separators=(",", ":")).encode()
        # Content-Type boundary=--client-stream-boundary--
        # => on-wire delimiter ----client-stream-boundary--
        part = (
            b"----client-stream-boundary--\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
            + b"\r\n"
        )
        self._sock.sendall(part)

    def _recv_more(self, timeout: float = 1.0) -> bool:
        assert self._sock
        self._sock.settimeout(timeout)
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout:
            return True
        if not chunk:
            return False
        self._buf += chunk
        return True

    def iter_parts(self, *, idle_timeout: float = 8.0):
        """Yield (content_type, headers_dict, body_bytes)."""
        deadline = time.time() + idle_timeout
        while time.time() < deadline:
            while True:
                m = re.search(br"----device-stream-boundary--\r\n", self._buf)
                if not m:
                    break
                start = m.end()
                hm = re.match(br"(.*?)\r\n\r\n", self._buf[start:], re.S)
                if not hm:
                    break
                hdr_text = hm.group(1).decode("utf-8", "replace")
                cl_m = re.search(r"Content-Length:\s*(\d+)", hdr_text, re.I)
                if not cl_m:
                    break
                cl = int(cl_m.group(1))
                body_start = start + hm.end()
                if len(self._buf) < body_start + cl:
                    break
                body = self._buf[body_start : body_start + cl]
                self._buf = self._buf[body_start + cl :]
                if self._buf.startswith(b"\r\n"):
                    self._buf = self._buf[2:]
                ctype_m = re.search(r"Content-Type:\s*(\S+)", hdr_text, re.I)
                ctype = ctype_m.group(1) if ctype_m else "application/octet-stream"
                headers = {
                    k.lower(): v.strip()
                    for k, v in (
                        line.split(":", 1)
                        for line in hdr_text.split("\r\n")
                        if ":" in line
                    )
                }
                deadline = time.time() + idle_timeout
                yield ctype, headers, body
            if not self._recv_more(0.5):
                break

    def request_preview(
        self, channel: int = 0, resolution: str = "HD"
    ) -> dict[str, Any]:
        self.send_json(
            {
                "type": "request",
                "seq": 1,
                "params": {
                    "preview": {
                        "audio": ["default"],
                        "channels": [int(channel)],
                        "resolutions": [resolution],
                    },
                    "method": "get",
                },
            }
        )
        return self._wait_json_response()

    def request_playback(
        self,
        start_time: int | str,
        end_time: int | str,
        *,
        channel: int = 0,
        client_id: int = 2,
        scale: str = "1/1",
        event_type: list[int] | None = None,
    ) -> dict[str, Any]:
        # App / PCAPdroid use client_id=2. client_id=-1 → Streamd -52407 on IPC59AE.
        pb: dict[str, Any] = {
            "client_id": int(client_id),
            "channels": [int(channel)],
            "scale": scale,
            "start_time": str(start_time),
            "end_time": str(end_time),
            "event_type": event_type or [1, 2],
        }
        self.send_json(
            {
                "type": "request",
                "seq": 1,
                "params": {"playback": pb, "method": "get"},
            }
        )
        return self._wait_json_response()

    def _wait_json_response(self, timeout: float = 5.0) -> dict[str, Any]:
        for ctype, _headers, body in self.iter_parts(idle_timeout=timeout):
            if "json" not in ctype:
                continue
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "response":
                params = data.get("params") or {}
                self.session_id = str(params.get("session_id") or "") or None
                return data
        raise TPLinkMediaStreamError("No JSON response from media stream")

    def collect_mpegts(
        self,
        *,
        max_bytes: int = 8_000_000,
        max_seconds: float = 30.0,
        idle_timeout: float = 3.0,
    ) -> bytes:
        """Collect video/mp2t payloads until size/time/idle limits."""
        out = bytearray()
        start = time.time()
        last_data = start
        for ctype, _headers, body in self.iter_parts(idle_timeout=idle_timeout):
            if "mp2t" in ctype:
                out.extend(body)
                last_data = time.time()
                if len(out) >= max_bytes:
                    break
            if time.time() - start >= max_seconds:
                break
            if time.time() - last_data > idle_timeout and out:
                break
        return bytes(out)


def playback_has_mpegts(
    host: str,
    username: str,
    password: str,
    stok: str,
    start_time: int | str,
    end_time: int | str,
    *,
    channel: int = 0,
    stream_port: int = 8800,
    use_stok_path: bool = True,
    min_bytes: int = 4096,
    max_seconds: float = 4.0,
) -> tuple[bool, int, dict[str, Any]]:
    """Quick probe: True if Streamd yields any MPEG-TS for the window.

    IPC Streamd often returns error_code=0 with 0 bytes for empty hours
    (no motion / not yet flushed). Callers should skip those slots.
    """
    ports = [stream_port]
    # Prefer a single fast attempt; only fall back if connect fails.
    last_err: str | None = None
    for port in ports:
        try:
            with TPLinkMediaStreamSession(
                host,
                username,
                password,
                stok,
                stream_port=port,
                use_stok_path=use_stok_path,
                connect_timeout=3.0,
            ) as session:
                resp = session.request_playback(
                    start_time, end_time, channel=channel, client_id=2
                )
                err = (resp.get("params") or {}).get("error_code", 0)
                if err not in (0, None):
                    last_err = f"error_code={err}"
                else:
                    data = session.collect_mpegts(
                        max_bytes=max(min_bytes * 4, 32_000),
                        max_seconds=max_seconds,
                        idle_timeout=0.6,
                    )
                    n = len(data)
                    return n >= min_bytes, n, {
                        "response": resp,
                        "probe_bytes": n,
                        "stream_port": port,
                        "use_stok_path": use_stok_path,
                    }
        except (TPLinkMediaStreamError, OSError, TimeoutError) as err:
            last_err = str(err)
    # One fallback port if primary could not connect.
    for p in (8800, 8000):
        if p == stream_port:
            continue
        try:
            with TPLinkMediaStreamSession(
                host,
                username,
                password,
                stok,
                stream_port=p,
                use_stok_path=True,
                connect_timeout=3.0,
            ) as session:
                resp = session.request_playback(
                    start_time, end_time, channel=channel, client_id=2
                )
                err = (resp.get("params") or {}).get("error_code", 0)
                if err not in (0, None):
                    last_err = f"error_code={err}"
                    continue
                data = session.collect_mpegts(
                    max_bytes=max(min_bytes * 4, 32_000),
                    max_seconds=max_seconds,
                    idle_timeout=0.6,
                )
                n = len(data)
                return n >= min_bytes, n, {
                    "response": resp,
                    "probe_bytes": n,
                    "stream_port": p,
                    "use_stok_path": True,
                }
        except (TPLinkMediaStreamError, OSError, TimeoutError) as err:
            last_err = str(err)
            continue
    return False, 0, {"error": last_err or "probe failed"}


def download_playback_mpegts(
    host: str,
    username: str,
    password: str,
    stok: str,
    start_time: int | str,
    end_time: int | str,
    channel: int = 0,
    stream_port: int = 8000,
    max_bytes: int = 12_000_000,
    max_seconds: float = 45.0,
    use_stok_path: bool = True,
) -> tuple[bytes, dict[str, Any]]:
    """Open stream, request playback, return (mpegts_bytes, response_json)."""
    with TPLinkMediaStreamSession(
        host,
        username,
        password,
        stok,
        stream_port=stream_port,
        use_stok_path=use_stok_path,
    ) as session:
        resp = session.request_playback(
            start_time, end_time, channel=channel, client_id=2
        )
        err = (resp.get("params") or {}).get("error_code", 0)
        if err not in (0, None):
            hint = explain_stream_error(err) or ""
            suffix = f" — {hint}" if hint else ""
            raise TPLinkMediaStreamError(
                f"playback request failed ({err}){suffix}: {resp}"
            )
        data = session.collect_mpegts(
            max_bytes=max_bytes, max_seconds=max_seconds
        )
        meta = {
            "response": resp,
            "session_id": session.session_id,
            "bytes": len(data),
            "channel": channel,
            "start_time": str(start_time),
            "end_time": str(end_time),
            "stream_port": stream_port,
            "use_stok_path": use_stok_path,
        }
        return data, meta

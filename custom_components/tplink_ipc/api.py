"""Local HTTP client for TP-Link China IPC (tpSurveillance APK protocol)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import ssl
from typing import Any
from urllib.parse import quote, unquote

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import TP_ENCRYPT_A, TP_ENCRYPT_C
from .features import POLL_REQUESTS

_LOGGER = logging.getLogger(__name__)


class TPLinkIPCError(Exception):
    """Base error."""


class TPLinkIPCAuthError(TPLinkIPCError):
    """Authentication failed."""


class TPLinkIPCLockedError(TPLinkIPCAuthError):
    """Device temporarily locked after too many login attempts."""

    def __init__(self, sec_left: int | None = None, raw: Any = None) -> None:
        self.sec_left = sec_left
        msg = "Device login locked"
        if sec_left is not None:
            msg = f"Device login locked, retry in {sec_left}s"
        super().__init__(f"{msg}: {raw}")


def tp_encrypt(password: str) -> str:
    """TP-Link proprietary password scramble used by China IPC web UI."""
    a = TP_ENCRYPT_A
    c = TP_ENCRYPT_C
    b = password
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


def _load_rsa_public_key(pubkey_b64: str):
    """Load TP-Link device RSA public key (DER SPKI or raw modulus blob)."""
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    raw = unquote(str(pubkey_b64)).strip()
    if "BEGIN PUBLIC KEY" in raw:
        return load_pem_public_key(raw.encode("utf-8"), backend=default_backend())

    der = base64.b64decode(raw)
    try:
        return load_der_public_key(der, backend=default_backend())
    except ValueError:
        pass

    # Mercury / older web UI: fixed-offset parse of PKCS#1-ish blob (1024-bit)
    if len(der) >= 162:
        hex_str = der.hex()
        modulus = int(hex_str[29 * 2 : 29 * 2 + 128 * 2], 16)
        exponent = int(hex_str[159 * 2 : 159 * 2 + 3 * 2], 16)
        return RSAPublicNumbers(exponent, modulus).public_key(default_backend())

    raise ValueError(f"Unsupported RSA public key (len={len(der)})")


def _rsa_encrypt_b64(plaintext: str, pubkey_b64: str) -> str:
    key = _load_rsa_public_key(pubkey_b64)
    cipher = key.encrypt(plaintext.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(cipher).decode("ascii")


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _looks_like_rsa_key(key: Any) -> bool:
    if not key or not isinstance(key, str):
        return False
    raw = unquote(key).strip()
    if len(raw) < 32:
        return False
    if "BEGIN PUBLIC KEY" in raw:
        return True
    try:
        base64.b64decode(raw)
        return True
    except Exception:
        return False


class TPLinkIPCClient:
    """Async local client for one camera."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        use_https: bool = False,
        verify_ssl: bool = False,
        rtsp_port: int = 554,
        stream_path: str = "stream1",
    ) -> None:
        self.hass = hass
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_https = use_https
        self.verify_ssl = verify_ssl
        self.rtsp_port = rtsp_port
        self.stream_path = stream_path.lstrip("/")
        # NVRHttpStream often 8000; many IPCs use Streamd on 8800
        # TL-IPC59AE / many China IPCs expose Streamd on 8800; NVR often 8000.
        self.stream_port = 8800
        self._stream_ports_try = (8800, 8000)

        self._stok: str | None = None
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {}
        self._ssl_context: ssl.SSLContext | None = None

        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}:{port}"

    @property
    def state(self) -> dict[str, Any]:
        return self._state

    @property
    def module_spec(self) -> dict[str, Any]:
        return (
            self._state.get("function", {}).get("module_spec", {})
            or {}
        )

    def has_module(self, key: str | None) -> bool:
        if key is None:
            return True
        val = self.module_spec.get(key)
        return val not in (None, "0", 0, False, "")

    @property
    def rtsp_url(self) -> str:
        user = quote(self.username, safe="")
        pwd = quote(self.password, safe="")
        return f"rtsp://{user}:{pwd}@{self.host}:{self.rtsp_port}/{self.stream_path}"

    @property
    def rtsp_url_sub(self) -> str:
        user = quote(self.username, safe="")
        pwd = quote(self.password, safe="")
        return f"rtsp://{user}:{pwd}@{self.host}:{self.rtsp_port}/stream2"

    @property
    def device_name(self) -> str:
        info = self._state.get("device_info", {}).get("basic_info", {})
        raw = (
            info.get("device_alias")
            or info.get("dev_name")
            or info.get("device_model")
            or self.host
        )
        return unquote(str(raw))

    @property
    def model(self) -> str | None:
        info = self._state.get("device_info", {}).get("basic_info", {})
        raw = info.get("device_model") or info.get("dev_model")
        return unquote(str(raw)) if raw else None

    @property
    def sw_version(self) -> str | None:
        info = self._state.get("device_info", {}).get("basic_info", {})
        raw = info.get("sw_version") or info.get("firmware_version")
        return unquote(str(raw)) if raw else None

    @property
    def mac(self) -> str | None:
        info = self._state.get("device_info", {}).get("basic_info", {})
        return info.get("mac") or info.get("macaddr")

    def _session_ssl(self) -> ssl.SSLContext | bool | None:
        if not self.use_https:
            return None
        if self.verify_ssl:
            return True
        if self._ssl_context is None:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            except ssl.SSLError:
                pass
            self._ssl_context = ctx
        return self._ssl_context

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = async_get_clientsession(self.hass, verify_ssl=self.verify_ssl)
        url = f"{self.base_url}{path}"
        try:
            async with session.post(
                url,
                json=payload,
                ssl=self._session_ssl(),
                timeout=15,
                headers={
                    "Content-Type": "application/json; charset=UTF-8",
                    "Accept": "application/json",
                    "Referer": self.base_url,
                    "requestByApp": "true",
                },
            ) as resp:
                text = await resp.text()
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    _LOGGER.debug("Non-JSON from %s: %s", url, text[:200])
                    raise TPLinkIPCError(f"Invalid JSON from {url}") from None
        except TPLinkIPCError:
            raise
        except Exception as err:
            raise TPLinkIPCError(f"Request failed: {err}") from err

    async def login(self) -> None:
        async with self._lock:
            await self._login_unlocked()

    async def _login_unlocked(self) -> None:
        enc = await self._post(
            "/",
            {"method": "do", "user_management": {"get_encrypt_info": None}},
        )
        key = nonce = None
        data = enc.get("data") or enc
        if isinstance(data, dict):
            key, nonce = data.get("key"), data.get("nonce")
            # Hard lockout after failed attempts (sec_left countdown)
            if data.get("code") in (-40404, -40408) or enc.get("error_code") in (
                -40404,
                -40408,
            ):
                raise TPLinkIPCLockedError(data.get("sec_left"), enc)

        if not _looks_like_rsa_key(key) or nonce is None:
            probe = await self._post("/", {"method": "do", "login": {}})
            pdata = probe.get("data") or probe
            if isinstance(pdata, dict):
                if pdata.get("code") in (-40404, -40408) or probe.get(
                    "error_code"
                ) in (-40404, -40408):
                    raise TPLinkIPCLockedError(pdata.get("sec_left"), probe)
                key, nonce = pdata.get("key"), pdata.get("nonce")
                # Soft -40401 with key+nonce is normal challenge; keep going

        # Path A: RSA session key available (TL-IPC44GW / Mercury style)
        if _looks_like_rsa_key(key) and nonce is not None:
            key_s = unquote(str(key))
            nonce_s = str(nonce)
            # type 2: TP scramble + RSA
            try:
                tp_pwd = tp_encrypt(self.password)
                rsa_pwd = await self.hass.async_add_executor_job(
                    _rsa_encrypt_b64, f"{tp_pwd}:{nonce_s}", key_s
                )
                result = await self._post(
                    "/",
                    {
                        "method": "do",
                        "login": {
                            "username": self.username,
                            "encrypt_type": "2",
                            "password": rsa_pwd,
                        },
                    },
                )
                if result.get("stok"):
                    self._stok = result["stok"]
                    return
                _LOGGER.debug("type2 login rejected: %s", result)
            except Exception as err:
                _LOGGER.debug("type2 login error: %s", err)

            # Refresh nonce then type 1: MD5 + RSA
            try:
                probe = await self._post("/", {"method": "do", "login": {}})
                pdata = probe.get("data") or probe
                if isinstance(pdata, dict) and _looks_like_rsa_key(
                    pdata.get("key")
                ) and pdata.get("nonce") is not None:
                    key_s = unquote(str(pdata["key"]))
                    nonce_s = str(pdata["nonce"])
                pwd_md5 = _md5_hex(self.password)
                rsa_pwd = await self.hass.async_add_executor_job(
                    _rsa_encrypt_b64, f"{pwd_md5}:{nonce_s}", key_s
                )
                result = await self._post(
                    "/",
                    {
                        "method": "do",
                        "login": {
                            "username": self.username,
                            "password": quote(rsa_pwd, safe=""),
                            "passwdType": "md5",
                            "encrypt_type": "1",
                        },
                    },
                )
                if result.get("stok"):
                    self._stok = result["stok"]
                    return
                _LOGGER.debug("type1 login rejected: %s", result)
            except Exception as err:
                _LOGGER.debug("type1 login error: %s", err)

        # Path B: older web UI (TL-IPC59AE) — TP scramble / plaintext, no RSA
        tp_pwd = tp_encrypt(self.password)
        result = None
        for candidate in (tp_pwd, self.password):
            result = await self._post(
                "/",
                {
                    "method": "do",
                    "login": {
                        "username": self.username,
                        "password": candidate,
                    },
                },
            )
            if result.get("stok"):
                self._stok = result["stok"]
                return

        raise TPLinkIPCAuthError(f"Login failed: {result}")

    async def request(
        self, payload: dict[str, Any], *, retry: bool = True, raise_error: bool = True
    ) -> dict[str, Any]:
        if not self._stok:
            await self.login()
        assert self._stok
        result = await self._post(f"/stok={self._stok}/ds", payload)
        err = result.get("error_code", 0)
        if err in (-40401, -40101, -40413) and retry:
            self._stok = None
            await self.login()
            return await self.request(payload, retry=False, raise_error=raise_error)
        if raise_error and err not in (0, None):
            raise TPLinkIPCError(f"Device error {err}: {result}")
        return result

    async def async_fetch_state(self) -> dict[str, Any]:
        """Poll all APK-aligned modules; merge successful responses."""
        # Battery doorbells sleep — App does wakeup before local gets.
        try:
            await self.request(
                {"method": "do", "doorbell_ring": {"wakeup": None}},
                raise_error=False,
            )
        except TPLinkIPCError:
            pass

        merged: dict[str, Any] = {}
        for payload in POLL_REQUESTS:
            try:
                result = await self.request(payload, raise_error=False)
            except TPLinkIPCError as err:
                _LOGGER.debug("poll failed %s: %s", payload, err)
                continue
            if result.get("error_code", 0) not in (0, None):
                continue
            for key, value in result.items():
                if key == "error_code":
                    continue
                merged[key] = value
        self._state = merged
        return merged

    async def async_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request(payload)

    async def async_test_connection(self) -> dict[str, Any]:
        await self.login()
        return await self.async_fetch_state()

    async def doorbell_wakeup(self) -> dict[str, Any]:
        """Wake a sleeping battery doorbell (APK doorbell_ring.wakeup)."""
        return await self.request(
            {"method": "do", "doorbell_ring": {"wakeup": None}},
            raise_error=False,
        )

    async def format_disk(self, disk_name: str = "1") -> dict[str, Any]:
        """Format HDD (APK harddisk_manage.format_hd). Destructive."""
        return await self.request(
            {
                "method": "do",
                "harddisk_manage": {"format_hd": str(disk_name)},
            },
            raise_error=False,
        )

    async def get_format_status(self, disk_name: str = "1") -> dict[str, Any]:
        """Poll HDD format progress (APK harddisk_manage.get_format_status)."""
        return await self.request(
            {
                "method": "do",
                "harddisk_manage": {
                    "get_format_status": {"disk_name": str(disk_name)}
                },
            },
            raise_error=False,
        )

    async def format_sd(self, sd_name: str = "1") -> dict[str, Any]:
        """Format SD card (APK sd_manage.format_sd). Destructive."""
        return await self.request(
            {"method": "do", "sd_manage": {"format_sd": str(sd_name)}},
            raise_error=False,
        )

    async def set_timing_reboot(
        self,
        *,
        enabled: bool | None = None,
        day: int | None = None,
        time_hm: str | None = None,
    ) -> dict[str, Any]:
        """Set scheduled reboot (APK timing_reboot.reboot)."""
        cur = ((self._state.get("timing_reboot") or {}).get("reboot") or {}).copy()
        if not isinstance(cur, dict):
            cur = {}
        if enabled is not None:
            cur["enabled"] = "on" if enabled else "off"
        if day is not None:
            cur["day"] = int(day)
        if time_hm is not None:
            cur["time"] = str(time_hm)
        cur.setdefault("enabled", "off")
        cur.setdefault("day", 127)
        cur.setdefault("time", "03:00")
        return await self.request(
            {"method": "set", "timing_reboot": {"reboot": cur}}
        )

    async def trigger_alarm(self, *, seconds: float = 5.0) -> None:
        """Pulse siren (msg_alarm enabled) then restore previous state."""
        import asyncio

        info = ((self._state.get("msg_alarm") or {}).get("chn1_msg_alarm_info") or {})
        prev = str(info.get("enabled") or "off")
        await self.request(
            {
                "method": "set",
                "msg_alarm": {"chn1_msg_alarm_info": {"enabled": "on"}},
            }
        )
        try:
            await asyncio.sleep(max(1.0, float(seconds)))
        finally:
            await self.request(
                {
                    "method": "set",
                    "msg_alarm": {"chn1_msg_alarm_info": {"enabled": prev}},
                }
            )

    # ---- PTZ (TL-IPC44GW uses ptz.*, not motor.*) ----

    async def ptz_move(self, x: int | str, y: int | str) -> None:
        try:
            xf, yf = float(x), float(y)
        except (TypeError, ValueError):
            xf, yf = 0.0, 0.0
        if abs(xf) > 1.0 or abs(yf) > 1.0:
            xf = max(-1.0, min(1.0, xf / 180.0))
            yf = max(-1.0, min(1.0, yf / 90.0))
        await self.request(
            {
                "method": "do",
                "ptz": {
                    "absolute_move": {
                        "position_pan": f"{xf:.6f}",
                        "position_tilt": f"{yf:.6f}",
                        "position_zoom": "0",
                    }
                },
            }
        )

    async def ptz_step(self, direction: str) -> None:
        speed = 0.35
        mapping = {
            "0": (speed, 0.0),
            "90": (0.0, speed),
            "180": (-speed, 0.0),
            "270": (0.0, -speed),
            "RIGHT": (speed, 0.0),
            "UP": (0.0, speed),
            "LEFT": (-speed, 0.0),
            "DOWN": (0.0, -speed),
        }
        pan, tilt = mapping.get(str(direction), (0.0, 0.0))
        await self.request(
            {
                "method": "do",
                "ptz": {
                    "continuous_move": {
                        "velocity_pan": f"{pan:.6f}",
                        "velocity_tilt": f"{tilt:.6f}",
                        "velocity_zoom": "0",
                    }
                },
            }
        )
        await asyncio.sleep(0.4)
        await self.ptz_stop()

    async def ptz_zoom(self, zoom: float) -> None:
        """Absolute zoom 0..1 (capability position_zoom_range)."""
        z = max(0.0, min(1.0, float(zoom)))
        await self.request(
            {
                "method": "do",
                "ptz": {
                    "absolute_move": {
                        "position_pan": "0",
                        "position_tilt": "0",
                        "position_zoom": f"{z:.6f}",
                    }
                },
            }
        )

    async def ptz_continuous(
        self, pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0
    ) -> None:
        await self.request(
            {
                "method": "do",
                "ptz": {
                    "continuous_move": {
                        "velocity_pan": f"{pan:.6f}",
                        "velocity_tilt": f"{tilt:.6f}",
                        "velocity_zoom": f"{zoom:.6f}",
                    }
                },
            }
        )

    async def ptz_stop(self) -> None:
        await self.request({"method": "do", "ptz": {"stop": "null"}})

    async def ptz_calibrate(self) -> None:
        await self.request({"method": "do", "ptz": {"manual_cali": "null"}})

    async def ptz_goto_preset(self, preset_id: str | int) -> None:
        await self.request(
            {"method": "do", "preset": {"goto_preset": {"id": str(preset_id)}}}
        )

    async def ptz_set_preset(self, name: str) -> None:
        await self.request(
            {
                "method": "do",
                "preset": {"set_preset": {"name": name, "save_ptz": "1"}},
            }
        )

    async def reboot(self) -> None:
        await self.request({"method": "do", "system": {"reboot": None}})

    @property
    def device_type(self) -> str | None:
        info = self._state.get("device_info", {}).get("basic_info", {})
        raw = info.get("device_type")
        return unquote(str(raw)) if raw else None

    @property
    def is_nvr(self) -> bool:
        dt = (self.device_type or "").upper()
        return "NVR" in dt or "NETWORKVIDEORECORDER" in dt

    @property
    def max_channels(self) -> int:
        try:
            return int(self.module_spec.get("max_channels") or 1)
        except (TypeError, ValueError):
            return 1

    def list_channels(self) -> list[dict[str, Any]]:
        """NVR channel list from chm.chn_info (id is 1-based hardware channel)."""
        rows = self._unwrap_table(
            (self._state.get("chm") or {}).get("chn_info") or []
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            cid = row.get("id")
            if cid is None:
                continue
            try:
                channel_id = int(cid)
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "id": channel_id,
                    # APK playback channel index is 0-based (hardware id - 1)
                    "playback_channel": channel_id - 1,
                    "name": unquote(str(row.get("name") or f"CH{channel_id}")),
                    "alias": unquote(str(row.get("alias") or "")),
                    "ip": row.get("ip"),
                    "online": str(row.get("online")) in ("1", "on", "true"),
                    "main_url": unquote(str(row.get("main_url") or "")),
                    "sub_url": unquote(str(row.get("sub_url") or "")),
                    "model": unquote(str(row.get("device_mode") or "")),
                    "mac": row.get("mac"),
                    "record": row.get("record"),
                }
            )
        out.sort(key=lambda c: c["id"])
        return out

    def channel_rtsp_url(self, channel_id: int, *, sub: bool = False) -> str | None:
        for ch in self.list_channels():
            if ch["id"] == channel_id:
                url = ch["sub_url"] if sub else ch["main_url"]
                return url or None
        return None

    # ---- Playback / recording history (APK-aligned) ----

    @property
    def playback_version(self) -> str:
        """Device module_spec.playback_version, e.g. '1.0' or '1.1'."""
        return str(self.module_spec.get("playback_version") or "1.0")

    @staticmethod
    def _ymd(value: str | int) -> str:
        """Normalize date to YYYYMMDD (device rejects dashed dates)."""
        s = str(value).strip().replace("-", "").replace("/", "")
        if len(s) >= 8 and s[:8].isdigit():
            return s[:8]
        raise TPLinkIPCError(f"Invalid date (want YYYYMMDD): {value}")

    @staticmethod
    def _unwrap_table(items: Any) -> list[dict[str, Any]]:
        """Flatten TP-Link table rows like [{k: {...}}, ...] -> [{...}, ...]."""
        out: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return out
        for row in items:
            if not isinstance(row, dict):
                continue
            for val in row.values():
                if isinstance(val, dict):
                    out.append(val)
        return out

    async def search_recording_days(
        self,
        start_date: str,
        end_date: str,
        *,
        channel: int = 0,
    ) -> list[str]:
        """Return YYYYMMDD list of days that have local recordings (APK calendar)."""
        start = self._ymd(start_date)
        end = self._ymd(end_date)
        result = await self.request(
            {
                "method": "do",
                "playback": {
                    "search_year_utility": {
                        "start_date": start,
                        "end_date": end,
                        "channel": int(channel),
                    }
                },
            },
            raise_error=False,
        )
        err = result.get("error_code", 0)
        if err not in (0, None):
            raise TPLinkIPCError(f"search_year_utility failed ({err}): {result}")
        rows = self._unwrap_table(
            (result.get("playback") or {}).get("search_results") or []
        )
        days = [str(r["date"]) for r in rows if r.get("date")]
        days.sort()
        return days

    async def search_video_segments(
        self,
        date: str,
        *,
        channel: int = 0,
        start_index: int = 0,
        end_index: int = 99,
        segment_id: int = -1,
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        """Search video clips for one day (works on NVR; many IPCs need stream client)."""
        day = self._ymd(date)
        collected: list[dict[str, Any]] = []
        idx = int(start_index)
        hard_end = int(end_index)
        while idx <= hard_end:
            page_end = min(idx + page_size - 1, hard_end)
            body: dict[str, Any] = {
                "date": day,
                "id": int(segment_id),
                "start_index": idx,
                "end_index": page_end,
                "channel": int(channel),
            }
            result = await self.request(
                {"method": "do", "playback": {"search_video_utility": body}},
                raise_error=False,
            )
            err = result.get("error_code", 0)
            if err not in (0, None) and "channel" in body:
                body.pop("channel", None)
                result = await self.request(
                    {"method": "do", "playback": {"search_video_utility": body}},
                    raise_error=False,
                )
                err = result.get("error_code", 0)
            if err not in (0, None):
                if not collected:
                    raise TPLinkIPCError(
                        f"search_video_utility failed ({err}): {result}"
                    )
                break
            pb = result.get("playback") or {}
            rows = self._unwrap_table(pb.get("search_video_results") or [])
            if not rows:
                rows = self._unwrap_table(pb.get("search_results") or [])
            if not rows:
                break
            collected.extend(rows)
            if len(rows) < (page_end - idx + 1):
                break
            idx = page_end + 1
        return collected

    @staticmethod
    def _day_bounds_cst(day: str) -> tuple[int, int]:
        """Return (start, end) unix seconds for YYYYMMDD in CST (UTC+8)."""
        import calendar as _cal

        y, m, d = int(day[0:4]), int(day[4:6]), int(day[6:8])
        start = int(_cal.timegm((y, m, d, 0, 0, 0, 0, 0, 0))) - 8 * 3600
        return start, start + 86400 - 1

    def synthetic_day_segments(
        self,
        date: str,
        *,
        slot_seconds: int = 3600,
        newest_first: bool = True,
    ) -> list[dict[str, Any]]:
        """Build seek windows when device has no segment-list API (IPC Streamd)."""
        import time as _time

        day = self._ymd(date)
        start, day_end = self._day_bounds_cst(day)
        end = min(day_end, int(_time.time()))
        if end <= start or slot_seconds < 60:
            return []
        segments: list[dict[str, Any]] = []
        t = start
        while t <= end:
            te = min(t + slot_seconds - 1, end)
            if te < t:
                break
            # Skip the in-progress hour: Streamd often returns error_code=0
            # with 0 MPEG-TS bytes until the hour is closed / flushed.
            if te < t + slot_seconds - 1:
                break
            segments.append(
                {
                    "start_time": t,
                    "end_time": te,
                    "startTime": t,
                    "endTime": te,
                    "synthetic": True,
                    # t is unix UTC; +8h via gmtime → CST wall clock label
                    "label": _time.strftime("%H:%M", _time.gmtime(t + 8 * 3600)),
                }
            )
            t += slot_seconds
        if newest_first:
            segments.reverse()
        return segments

    @staticmethod
    def _segments_newest_first(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """NVR search_video returns oldest-first; UI index 0 must be newest."""

        def _start(seg: dict[str, Any]) -> int:
            raw = seg.get("startTime") or seg.get("start_time") or 0
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0

        return sorted(segments, key=_start, reverse=True)

    async def search_day_timeline(
        self, date: str, *, channel: int = 0
    ) -> dict[str, Any]:
        """Best-effort day timeline: segments → events → synthetic hourly slots."""
        day = self._ymd(date)
        try:
            segments = self._segments_newest_first(
                await self.search_video_segments(day, channel=channel)
            )
            return {
                "ok": True,
                "source": "search_video_utility",
                "date": day,
                "channel": channel,
                "segments": segments,
                "count": len(segments),
            }
        except TPLinkIPCError as seg_err:
            start, end = self._day_bounds_cst(day)
            try:
                events = self._segments_newest_first(
                    await self.search_events(
                        start, end, channel=channel, max_num=200
                    )
                )
                return {
                    "ok": True,
                    "source": "get_event_list",
                    "date": day,
                    "channel": channel,
                    "segments": events,
                    "count": len(events),
                    "warning": str(seg_err),
                }
            except TPLinkIPCError as ev_err:
                # TL-IPC59AE: calendar OK, but no search_video / get_event_list.
                # Streamd still accepts arbitrary unix start/end for playback.
                synthetic = self.synthetic_day_segments(day)
                if synthetic:
                    return {
                        "ok": True,
                        "source": "synthetic_slots",
                        "date": day,
                        "channel": channel,
                        "segments": synthetic,
                        "count": len(synthetic),
                        "warning": f"{seg_err}; {ev_err}",
                        "note": (
                            "裝置無片段列表；已依 CST 每小時產生可跳轉時段"
                            "（較新在前）。點「播放」會取該時段開頭。"
                        ),
                    }
                return {
                    "ok": False,
                    "date": day,
                    "channel": channel,
                    "segments": [],
                    "count": 0,
                    "error": f"{seg_err}; {ev_err}",
                }

    async def search_events(
        self,
        start_time: str | int,
        end_time: str | int,
        *,
        channel: int = 0,
        start_index: int = 0,
        max_num: int = 100,
        client_id: int = -1,
        event_type: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Search event timeline for playback_version 1.1 (get_event_list)."""
        body: dict[str, Any] = {
            "channel": int(channel),
            "client_id": int(client_id),
            "start_index": int(start_index),
            "max_num": int(max_num),
            "start_time": start_time
            if isinstance(start_time, int)
            else str(start_time),
            "end_time": end_time if isinstance(end_time, int) else str(end_time),
        }
        if event_type is not None:
            body["event_type"] = event_type
        result = await self.request(
            {"method": "do", "playback": {"get_event_list": body}},
            raise_error=False,
        )
        err = result.get("error_code", 0)
        if err not in (0, None):
            raise TPLinkIPCError(f"get_event_list failed ({err}): {result}")
        pb = result.get("playback") or {}
        rows = self._unwrap_table(pb.get("event_list") or [])
        if not rows and isinstance(pb.get("event_list"), list):
            rows = [x for x in pb["event_list"] if isinstance(x, dict)]
        return rows

    def playback_stream_url(
        self,
        client_id: int | str,
        start_time: int | str,
        *,
        channels: str = "0",
        scale: str = "1/1",
        event_type: int | str = 0,
    ) -> str:
        """Build APK local playback URL (needs active client_id from media session)."""
        if not self._stok:
            raise TPLinkIPCError("Not logged in")
        from urllib.parse import quote as _q

        q = (
            f"client_id={client_id}"
            f"&start_time={start_time}"
            f"&channels={channels}"
            f"&scale={_q(str(scale), safe='/')}"
            f"&type={event_type}"
        )
        return f"{self.base_url}/stok={self._stok}/stream/playback?{q}"

    async def download_playback_clip(
        self,
        start_time: int | str,
        end_time: int | str,
        *,
        channel: int = 0,
        max_bytes: int = 12_000_000,
        max_seconds: float = 45.0,
    ) -> tuple[bytes, dict[str, Any]]:
        """Download MPEG-TS recording via media stream (try port 8000 then 8800)."""
        if not self._stok:
            await self.login()
        assert self._stok
        from .stream_session import (
            TPLinkMediaStreamError,
            download_playback_mpegts,
        )

        ports = list(self._stream_ports_try)
        # Prefer last known good port first
        if self.stream_port in ports:
            ports.remove(self.stream_port)
            ports.insert(0, self.stream_port)

        last_err: Exception | None = None
        for port in ports:
            for use_stok in (True, False):
                try:
                    data, meta = await self.hass.async_add_executor_job(
                        download_playback_mpegts,
                        self.host,
                        self.username,
                        self.password,
                        self._stok,
                        start_time,
                        end_time,
                        channel,
                        port,
                        max_bytes,
                        max_seconds,
                        use_stok,
                    )
                    self.stream_port = port
                    meta["stream_port"] = port
                    meta["use_stok_path"] = use_stok
                    return data, meta
                except TPLinkMediaStreamError as err:
                    last_err = err
                    _LOGGER.debug(
                        "playback stream %s:%s stok=%s failed: %s",
                        self.host,
                        port,
                        use_stok,
                        err,
                    )
                except OSError as err:
                    last_err = err
                    _LOGGER.debug(
                        "playback connect %s:%s failed: %s", self.host, port, err
                    )
                    break  # port unreachable — skip use_stok=False
        raise TPLinkMediaStreamError(
            f"No media stream on {self.host} ports {ports}: {last_err}"
        )

    def storage_info(self) -> dict[str, Any] | None:
        """Return first harddisk_manage.hd_info entry if present."""
        hd = (self._state.get("harddisk_manage") or {}).get("hd_info")
        if not isinstance(hd, list) or not hd:
            return None
        first = hd[0]
        if isinstance(first, dict):
            for val in first.values():
                if isinstance(val, dict):
                    return val
        return None

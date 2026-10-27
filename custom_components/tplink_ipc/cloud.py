"""TP-Link China Surveillance App (tpSurveillance) cloud API client.

Matches the APK n-wap-ipc JSON protocol:
  POST https://n-wap-ipc.tplinkcloud.com.cn/
  {"method":"login","params":{appType, cloudUserName, cloudPassword, terminalUUID}}
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

DEFAULT_CLOUD_HOST = "https://n-wap-ipc.tplinkcloud.com.cn"
DEFAULT_APP_TYPE = "TP-LINK_IPC_"

# Known n-wap-ipc / Kasa-family account errors (see APK + international messages)
CLOUD_ERROR_HINTS: dict[int, str] = {
    -20600: "帳號不存在",
    -20601: "帳號或密碼錯誤",
    -20615: "密碼解密失敗（請用明文雲端密碼）",
    -20661: "帳號已鎖定，請稍後再試或在 App 解鎖",
    -20677: "需要 MFA 驗證",
    -20103: "方法不存在或不可用",
    -20104: "缺少必要參數",
    -20000: "一般錯誤",
}


class TPLinkCloudError(Exception):
    """Cloud API error."""

    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        raw: Any = None,
    ) -> None:
        self.error_code = error_code
        self.raw = raw
        super().__init__(message)


class TPLinkCloudAuthError(TPLinkCloudError):
    """Login / token failure."""


class TPLinkCloudLockedError(TPLinkCloudAuthError):
    """Account temporarily locked."""


class TPLinkCloudClient:
    """Async client for n-wap-ipc (official APK cloud)."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        username: str,
        password: str,
        terminal_uuid: str | None = None,
        app_type: str = DEFAULT_APP_TYPE,
        host: str = DEFAULT_CLOUD_HOST,
        verify_ssl: bool = False,
    ) -> None:
        self.hass = hass
        self.username = username.strip()
        self.password = password
        self.terminal_uuid = terminal_uuid or str(uuid.uuid4())
        self.app_type = app_type
        self.host = host.rstrip("/")
        self.verify_ssl = verify_ssl
        self.token: str | None = None
        # HA container CA store often fails on tplinkcloud.com.cn chain;
        # default off to match local IPC client and our working probes.
        self._session = async_get_clientsession(hass, verify_ssl=verify_ssl)

    def _url(self, *, with_token: bool = False) -> str:
        base = f"{self.host}/"
        if with_token and self.token:
            return f"{base}?token={self.token}"
        return base

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "TP-LINK_APP",
            "Accept": "application/json",
            "Term-Id": self.terminal_uuid,
        }
        if self.token:
            headers["X-Token"] = self.token
            headers["Account-Token"] = self.token
        return headers

    @staticmethod
    def _raise_for_code(data: dict[str, Any], *, auth: bool = False) -> None:
        code = data.get("error_code")
        if code in (0, None) and "result" in data:
            return
        try:
            code_i = int(code)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise TPLinkCloudError(f"Unexpected cloud response: {data}", raw=data) from None
        if code_i == 0:
            return
        hint = CLOUD_ERROR_HINTS.get(code_i, data.get("msg") or "cloud error")
        msg = f"{hint} ({code_i})"
        if code_i == -20661:
            raise TPLinkCloudLockedError(msg, error_code=code_i, raw=data)
        # Token / session expiry codes seen across Kasa-family APIs
        if auth or code_i in (
            -20600,
            -20601,
            -20615,
            -20651,
            -20675,
            -20677,
        ):
            raise TPLinkCloudAuthError(msg, error_code=code_i, raw=data)
        raise TPLinkCloudError(msg, error_code=code_i, raw=data)

    async def _async_post(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        require_token: bool,
    ) -> dict[str, Any]:
        if require_token and not self.token:
            raise TPLinkCloudAuthError("Not logged in")
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        try:
            async with self._session.post(
                self._url(with_token=require_token and bool(self.token)),
                json=payload,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                try:
                    data = await resp.json(content_type=None)
                except Exception as err:
                    raise TPLinkCloudError(
                        f"Non-JSON cloud response HTTP {resp.status}: {text[:300]}",
                        raw=text,
                    ) from err
        except (
            aiohttp.ClientConnectorError,
            aiohttp.ClientConnectorCertificateError,
            aiohttp.ServerTimeoutError,
            TimeoutError,
            OSError,
        ) as err:
            raise TPLinkCloudError(f"Cannot reach cloud API: {err}") from err
        if not isinstance(data, dict):
            raise TPLinkCloudError(f"Unexpected cloud payload: {data!r}", raw=data)
        return data

    async def async_call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        require_token: bool = True,
        _retried: bool = False,
    ) -> dict[str, Any]:
        """Call a cloud method; auto re-login once on token/auth errors."""
        data = await self._async_post(method, params, require_token=require_token)
        try:
            self._raise_for_code(data, auth=method == "login")
        except TPLinkCloudAuthError:
            if (
                method != "login"
                and require_token
                and not _retried
            ):
                _LOGGER.info("Cloud token expired for %s; re-login", method)
                self.token = None
                await self.async_login()
                return await self.async_call(
                    method, params, require_token=require_token, _retried=True
                )
            raise
        return data

    async def async_login(self) -> str:
        """Login with cloud username/password; returns account token."""
        data = await self.async_call(
            "login",
            {
                "appType": self.app_type,
                "cloudUserName": self.username,
                "cloudPassword": self.password,
                "terminalUUID": self.terminal_uuid,
            },
            require_token=False,
        )
        token = (data.get("result") or {}).get("token")
        if not token:
            raise TPLinkCloudAuthError("Login OK but no token", raw=data)
        self.token = str(token)
        _LOGGER.info(
            "TP-Link cloud login OK for %s (terminal=%s)",
            self.username,
            self.terminal_uuid[:8],
        )
        return self.token

    async def async_ensure_login(self) -> str:
        if self.token:
            return self.token
        return await self.async_login()

    async def async_get_device_list(self) -> list[dict[str, Any]]:
        """Return bound devices (cameras, NVR, routers, …)."""
        await self.async_ensure_login()
        data = await self.async_call("getDeviceList")
        result = data.get("result") or {}
        devices = result.get("deviceList") or []
        if not isinstance(devices, list):
            return []
        return devices

    async def async_passthrough(
        self,
        device_id: str,
        request_data: dict[str, Any],
    ) -> Any:
        """Proxy a local-style JSON request through the cloud to a device.

        ``request_data`` must be a dict (not a JSON string).
        Returns the device ``responseData`` (or full result on odd shapes).
        """
        await self.async_ensure_login()
        if not isinstance(request_data, dict):
            raise TPLinkCloudError("request_data must be a JSON object")
        data = await self.async_call(
            "passthrough",
            {"deviceId": device_id, "requestData": request_data},
        )
        result = data.get("result") or {}
        if "responseData" in result:
            return result["responseData"]
        return result

    def to_persistence(self) -> dict[str, Any]:
        """Fields safe to store in config entry (no password)."""
        return {
            "terminal_uuid": self.terminal_uuid,
            "app_type": self.app_type,
            "host": self.host,
        }

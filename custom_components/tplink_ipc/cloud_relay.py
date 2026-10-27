"""Cloud media relay client (bizToken + cluster + relayservice multipart).

Protocol notes: tmp/tpsurveillance/notes/cloud_relay.json
                 tmp/tpsurveillance/notes/relayws_progress.json

Pipeline:
  1. getBizToken (ipc.media / ipc.appService)
  2. getRelayClusterAddress → relayUrl + relayMasterUrl
  3. getRelayServerKey → cookie + relayServer
  4. passthrough BuildUnderlineRequestRelay → sid
  5. relayservice WSS (rtsp-rtp) → MPEG-TS  (step 5 still RE)

Captured tokens from ``relay_tokens.json`` are loaded when present.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloud import TPLinkCloudClient, TPLinkCloudError

_LOGGER = logging.getLogger(__name__)

DEFAULT_BIZ_HOST = "https://biz-ipc.tplinkcloud.com.cn"
DEFAULT_SERVICE_HOST = "https://n-service-ipc.tplinkcloud.com.cn"
DEFAULT_RELAY_HOSTS = (
    "https://n-relay-ipc-txc-nj-00.tplinkcloud.com.cn",
    "https://n-txc-relay-ipc-nj.tplinkcloud.com.cn",
    "https://txc-relay-ipc-nj.tplinkcloud.com.cn",
    "https://relay-ipc-txc-nj-00.tplinkcloud.com.cn",
)

# From APK libNetworkUtil TPBasicCloudService::GetAuthorization
ACCESS_KEY_PUB = "8c55a4a7-c86e-45c7-bbf6-524363ca394b"
ACCESS_KEY_SEC = "85cd4b2f-8c89-4927-b384-3e48ea55d4d6"

CLIENT_BOUNDARY = "--client-stream-boundary--"


class TPLinkCloudRelayError(TPLinkCloudError):
    """Media relay failure."""


def _md5_b64(text: str) -> str:
    import base64

    return base64.b64encode(hashlib.md5(text.encode()).digest()).decode()


def _sign_authorization(
    path: str,
    body: str,
    account_token: str,
    *,
    timestamp: str | None = None,
    nonce: str | None = None,
    access_key_pub: str = ACCESS_KEY_PUB,
    access_key_sec: str = ACCESS_KEY_SEC,
) -> str:
    """Build X-Authorization per native GetAuthorization."""
    import random

    ts = timestamp or str(int(time.time()))
    n = nonce or str(random.getrandbits(31))
    p = path[1:] if path.startswith("/") else path
    content = f"{p}\n{body}\n{_md5_b64(account_token)}\n{ts}\n{n}"
    dig = hmac.new(access_key_sec.encode(), content.encode(), hashlib.sha1).hexdigest()
    return (
        f"Timestamp={ts},Nonce={n},"
        f"AccessKey={access_key_pub},Signature={dig}"
    )


def _tokens_path(hass: HomeAssistant | None) -> Path | None:
    if hass is None:
        return None
    candidates = [
        Path(hass.config.path("custom_components/tplink_ipc/relay_tokens.json")),
    ]
    for p in candidates:
        if p.is_file():
            return p
    return candidates[0]


def _device_field(device: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if device.get(key) not in (None, ""):
            return device[key]
    return default


class TPLinkCloudRelayClient:
    """Obtain biz/relay tokens and pull MPEG-TS via relayservice."""

    def __init__(
        self,
        hass: HomeAssistant,
        cloud: TPLinkCloudClient,
        *,
        biz_host: str = DEFAULT_BIZ_HOST,
        service_host: str = DEFAULT_SERVICE_HOST,
        relay_hosts: tuple[str, ...] = DEFAULT_RELAY_HOSTS,
        verify_ssl: bool = False,
    ) -> None:
        self.hass = hass
        self.cloud = cloud
        self.biz_host = biz_host.rstrip("/")
        self.service_host = service_host.rstrip("/")
        self.relay_hosts = list(relay_hosts)
        self._session = async_get_clientsession(hass, verify_ssl=verify_ssl)
        self.biz_token: str | None = None
        self.biz_token_media: str | None = None
        self.biz_token_app: str | None = None
        self.relay_token: str | None = None
        self.relay_host: str | None = None
        self.relay_master_url: str | None = None
        self.cluster_info: dict[str, Any] | None = None
        self._load_captured_tokens()

    def _load_captured_tokens(self) -> None:
        path = _tokens_path(self.hass)
        if path is None or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("Cannot read relay_tokens.json: %s", err)
            return
        self.biz_token = data.get("bizToken") or data.get("biz_token") or self.biz_token
        self.biz_token_media = data.get("bizTokenMedia") or self.biz_token_media
        self.biz_token_app = data.get("bizTokenApp") or self.biz_token_app
        self.relay_token = (
            data.get("relayToken")
            or data.get("x_token")
            or data.get("X-Token")
            or self.relay_token
        )
        self.relay_host = data.get("relay_host") or data.get("relayHost") or self.relay_host
        self.relay_master_url = data.get("relayMasterUrl") or self.relay_master_url
        cluster = data.get("cluster") or data.get("clusterInfo")
        if isinstance(cluster, dict):
            self.cluster_info = cluster
            self._apply_cluster(cluster)
        if self.biz_token or self.relay_token:
            _LOGGER.info(
                "Loaded captured relay tokens from %s (biz=%s relay=%s)",
                path,
                bool(self.biz_token),
                bool(self.relay_token),
            )

    def _persist_tokens(self, extra: dict[str, Any] | None = None) -> None:
        path = _tokens_path(self.hass)
        if path is None:
            return
        payload: dict[str, Any] = {}
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
        payload.update(
            {
                "bizToken": self.biz_token,
                "bizTokenMedia": self.biz_token_media,
                "bizTokenApp": self.biz_token_app,
                "relayToken": self.relay_token,
                "relay_host": self.relay_host,
                "relayMasterUrl": self.relay_master_url,
                "cluster": self.cluster_info,
                "updated": int(time.time()),
            }
        )
        if extra:
            payload.update(extra)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as err:
            _LOGGER.debug("Cannot write relay_tokens.json: %s", err)

    def _apply_cluster(self, cluster: dict[str, Any]) -> None:
        self.cluster_info = cluster
        master = cluster.get("relayMasterUrl") or cluster.get("relayMasterDualstackUrl")
        if master:
            self.relay_master_url = str(master).rstrip("/")
        relay_url = cluster.get("relayUrl") or {}
        hosts: list[str] = []
        for key in ("selfcaHttps", "dccaHttps", "selfcaHttp", "dccaHttp"):
            url = relay_url.get(key)
            if not url:
                continue
            hosts.append(str(url).rstrip("/").replace(":443", "").replace(":80", ""))
        for h in hosts:
            if h not in self.relay_hosts:
                self.relay_hosts.insert(0, h)
        if hosts:
            self.relay_host = hosts[0]

    def _media_token(self) -> str:
        return (
            self.relay_token
            or self.biz_token_media
            or self.biz_token
            or self.cloud.token
            or ""
        )

    async def _async_biz_post(
        self,
        host: str,
        api_path: str,
        body_obj: dict[str, Any],
        *,
        biz_token: str | None = None,
        query_account_token: bool = True,
    ) -> dict[str, Any]:
        await self.cloud.async_ensure_login()
        assert self.cloud.token
        body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
        auth = _sign_authorization(api_path, body, self.cloud.token)
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "TP-LINK_APP",
            "Account-Token": self.cloud.token,
            "Term-Id": self.cloud.terminal_uuid,
            "X-Token": self.cloud.token,
            "X-Authorization": auth,
            "Connection": "keep-alive",
        }
        if biz_token:
            headers["Biz-Token"] = biz_token
        url = f"{host.rstrip('/')}{api_path}"
        if query_account_token:
            url = f"{url}?token={self.cloud.token}"
        try:
            async with self._session.post(
                url,
                data=body.encode(),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                data = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001
            raise TPLinkCloudRelayError(f"{api_path} transport: {err}") from err
        if not isinstance(data, dict):
            raise TPLinkCloudRelayError(f"{api_path} bad payload: {data!r}")
        return data

    async def async_get_biz_token(
        self, *, biz_service_id: str | None = None
    ) -> str:
        """POST getBizToken using native GetAuthorization signing."""
        await self.cloud.async_ensure_login()
        assert self.cloud.token
        api_path = "/biz/basicService/v1/getBizToken"
        ids = (
            [biz_service_id]
            if biz_service_id
            else ["ipc.media", "ipc.appService"]
        )
        last: dict[str, Any] | None = None
        for sid in ids:
            if not sid:
                continue
            data = await self._async_biz_post(
                self.biz_host, api_path, {"bizServiceId": sid}
            )
            last = data
            code = data.get("error_code")
            result = data.get("result") or {}
            token = (
                result.get("bizToken")
                or result.get("token")
                or result.get("serviceToken")
            )
            if code in (0, None) and token:
                token_s = str(token)
                if sid == "ipc.media":
                    self.biz_token_media = token_s
                elif sid == "ipc.appService":
                    self.biz_token_app = token_s
                self.biz_token = token_s
                _LOGGER.info("getBizToken OK (%s)", sid)
                self._persist_tokens()
                if biz_service_id:
                    return token_s
                # keep going to fill both media + app when default list
                continue
        if self.biz_token:
            return self.biz_token
        raise TPLinkCloudRelayError(
            f"getBizToken failed: {last}",
            error_code=int((last or {}).get("error_code") or 0) or None,
            raw=last,
        )

    async def async_get_relay_cluster_address(
        self, device: dict[str, Any]
    ) -> dict[str, Any]:
        """POST getRelayClusterAddress (proven working 2026-07-19)."""
        if not self.biz_token_app and not self.biz_token:
            await self.async_get_biz_token()
        biz = self.biz_token_app or self.biz_token
        assert biz
        item = {
            "deviceId": _device_field(device, "deviceId"),
            "deviceType": _device_field(
                device, "deviceType", default="SURVEILLANCECAMERA"
            ),
            "hwType": _device_field(device, "hwId", "hwType"),
            "hwVersion": _device_field(device, "deviceHwVer", "hwVersion"),
            "relationType": _device_field(device, "relationType", default="owner"),
        }
        api_path = "/biz/relayLocation/v1/getRelayClusterAddress"
        data = await self._async_biz_post(
            self.service_host,
            api_path,
            {"deviceList": [item]},
            biz_token=biz,
        )
        if data.get("error_code") not in (0, None):
            raise TPLinkCloudRelayError(
                f"getRelayClusterAddress failed: {data}",
                error_code=int(data.get("error_code") or 0) or None,
                raw=data,
            )
        result = data.get("result") or {}
        clusters = result.get("clusterInfoList") or []
        if not clusters:
            raise TPLinkCloudRelayError(
                "getRelayClusterAddress empty clusterInfoList", raw=data
            )
        cluster = clusters[0]
        self._apply_cluster(cluster)
        self._persist_tokens({"getRelayClusterAddress": data})
        _LOGGER.info(
            "relay cluster %s master=%s relay=%s",
            cluster.get("clusterId"),
            self.relay_master_url,
            self.relay_host,
        )
        return cluster

    async def async_relay_master(
        self,
        device: dict[str, Any],
        *,
        stream_type: str = "download",
        channel_id: int = 0,
        host: str | None = None,
    ) -> dict[str, Any]:
        """POST /biz/relayMaster/v1/getRelayServerKey (static RE 2026-07-19).

        GenRelayMasterUrl builds::

            /biz/relayMaster/v + to_string(1) + / + getRelayServerKey + ? + token=…

        i.e. ``/biz/relayMaster/v1/getRelayServerKey?token=<accountToken>``
        — **not** the adjacent string ``/biz/relayMaster/v2.0``.

        Body is cJSON: strings for ids/type, number for channelId, bools for
        isPrePostStream / isVipPreview. ``type=download|dtspk`` returns
        error_code=0 + cookie + relayServer; ``sdvod|video`` may still -82414.
        """
        await self.cloud.async_ensure_login()
        assert self.cloud.token
        if not self.biz_token_media and not self.biz_token:
            await self.async_get_biz_token()
        biz = self.biz_token_media or self.biz_token
        assert biz

        path = "/biz/relayMaster/v1/getRelayServerKey"
        client_uuid = str(uuid.uuid4())
        # Match BuildGetRelayServerKey field types (cJSON_AddString/Number/Bool)
        body_obj: dict[str, Any] = {
            "type": stream_type,
            "playerId": client_uuid.replace("-", "")[:16],
            "deviceId": _device_field(device, "deviceId"),
            "deviceType": _device_field(
                device, "deviceType", default="SURVEILLANCECAMERA"
            ),
            "hwType": _device_field(device, "hwId", "hwType"),
            "hwVersion": _device_field(device, "deviceHwVer", "hwVersion"),
            "channelId": int(channel_id),
            "clientUUID": client_uuid,
            "clientType": self.cloud.app_type or "TP-LINK_IPC_",
            "isPrePostStream": False,
            "isVipPreview": False,
        }
        if stream_type == "video":
            body_obj["resolution"] = "HD"
        elif stream_type in ("sdvod", "nvr"):
            body_obj["resolution"] = "0"

        body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
        auth = _sign_authorization(path, body, self.cloud.token)
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "TP-LINK_APP",
            "Biz-Token": biz,
            "Account-Token": self.cloud.token,
            "Term-Id": self.cloud.terminal_uuid,
            "X-Token": self.cloud.token,
            "X-Authorization": auth,
        }
        base = (
            host or self.relay_master_url or self.service_host
        ).rstrip("/")
        url = f"{base}{path}?token={self.cloud.token}"
        async with self._session.post(
            url,
            data=body.encode(),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            data = await resp.json(content_type=None)

        if not isinstance(data, dict) or data.get("error_code") != 0:
            raise TPLinkCloudRelayError(
                f"getRelayServerKey failed: {data}",
                error_code=int((data or {}).get("error_code") or 0) or None,
                raw=data if isinstance(data, dict) else None,
            )

        result = data.get("result") or {}
        cookie = result.get("cookie")
        relay_server = result.get("relayServer")
        if relay_server:
            port = result.get("relaysPort") or 443
            self.relay_host = (
                f"https://{relay_server}"
                if int(port) in (443, 0)
                else f"https://{relay_server}:{port}"
            )
        self.relay_token = biz
        self._persist_tokens(
            {
                "relayMaster": data,
                "relayCookie": cookie,
                "getRelayServerKey": True,
            }
        )
        _LOGGER.info(
            "getRelayServerKey OK type=%s relay=%s cookie=%s",
            stream_type,
            relay_server,
            bool(cookie),
        )
        return data

    def _relay_master_body(
        self, device: dict[str, Any], stream_type: str, channel_id: int
    ) -> dict[str, Any]:
        """Deprecated shape — kept for callers; prefer async_relay_master."""
        client_uuid = str(uuid.uuid4())
        return {
            "type": stream_type,
            "playerId": client_uuid.replace("-", "")[:16],
            "deviceId": _device_field(device, "deviceId"),
            "deviceType": _device_field(
                device, "deviceType", default="SURVEILLANCECAMERA"
            ),
            "hwType": _device_field(device, "hwId", "hwType"),
            "hwVersion": _device_field(device, "deviceHwVer", "hwVersion"),
            "channelId": int(channel_id),
            "clientUUID": client_uuid,
            "clientType": self.cloud.app_type or "TP-LINK_IPC_",
            "isPrePostStream": False,
            "isVipPreview": False,
        }

    async def async_prepare_media_session(
        self,
        device: dict[str, Any],
        *,
        stream_type: str = "download",
        channel_id: int = 0,
    ) -> dict[str, Any]:
        """bizToken → cluster → getRelayServerKey (try download/dtspk/sdvod)."""
        media = await self.async_get_biz_token("ipc.media")
        app = await self.async_get_biz_token("ipc.appService")
        cluster = await self.async_get_relay_cluster_address(device)
        master_err: str | None = None
        master: dict[str, Any] | None = None
        types = [stream_type]
        for t in ("download", "dtspk", "sdvod", "video"):
            if t not in types:
                types.append(t)
        for typ in types:
            try:
                master = await self.async_relay_master(
                    device, stream_type=typ, channel_id=channel_id
                )
                stream_type = typ
                master_err = None
                break
            except TPLinkCloudRelayError as err:
                master_err = str(err)
                _LOGGER.debug("getRelayServerKey type=%s: %s", typ, err)
        if master is None:
            _LOGGER.warning("getRelayServerKey still blocked: %s", master_err)
        return {
            "ok": master is not None and master_err is None,
            "bizTokenMedia": media,
            "bizTokenApp": app,
            "cluster": cluster,
            "relay_host": self.relay_host,
            "relayMasterUrl": self.relay_master_url,
            "stream_type": stream_type,
            "relayMaster": master,
            "relayMaster_error": master_err,
        }

    # GenerateRelayRequestUrl jump table: 0=video/nvr, 1=sdvod, 2=dtspk, 3=download
    _STREAM_TYPE_NUM = {
        "video": 0,
        "nvr": 0,
        "sdvod": 1,
        "dtspk": 2,
        "download": 3,
    }

    async def async_request_relay(
        self,
        device_id: str,
        *,
        stream_type: str = "download",
        host: str | None = None,
        version: str = "2.0",
        protocol: int = 1,
        start_time: int | str | None = None,
        end_time: int | str | None = None,
        player_id: str | None = None,
    ) -> dict[str, Any]:
        """Send BuildUnderlineRequestRelay via cloud passthrough.

        Static RE (2026-07-20): ``NotifyPeer`` → ``SendDeviceRequestToGateway``
        → n-wap ``passthrough`` (NOT POST to relayws ``/relaycontrol``).

        Live probe (2026-07-20): passthrough → ``sid``; then raw TLS
        ``GET /relayservice?…`` + Upgrade/rtsp-rtp + ``X-Client-SessionID=sid``
        yields **HTTP 101** after ~30s (device rendezvous). Do **not** send
        huge Content-Length on the Upgrade request (nginx 413). After 101 the
        payload is MULTITRANS (libTPMediaKit); peer may send stop until the
        start/download JSON is correct.
        """
        host = (host or self.relay_host or self.relay_hosts[0]).rstrip("/")
        account = self.cloud.token
        if not account:
            raise TPLinkCloudRelayError("No account token for request_relay")

        path = _tokens_path(self.hass)
        cookie = None
        relay_server = None
        relays_port = 443
        relay_port = 80
        if path and path.is_file():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                cookie = saved.get("relayCookie") or (
                    (saved.get("relayMaster") or {}).get("result") or {}
                ).get("cookie")
                master_res = (saved.get("relayMaster") or {}).get("result") or {}
                relay_server = master_res.get("relayServer")
                relays_port = int(master_res.get("relaysPort") or 443)
                relay_port = int(master_res.get("relayPort") or 80)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        if not relay_server:
            from urllib.parse import urlparse

            relay_server = urlparse(host).hostname or host
        if not cookie:
            raise TPLinkCloudRelayError(
                "No relay cookie — run getRelayServerKey first"
            )

        st_num = self._STREAM_TYPE_NUM.get(stream_type, 3)
        player = player_id or uuid.uuid4().hex[:16]
        now = int(time.time())
        start = int(start_time) if start_time is not None else now - 3600
        end = int(end_time) if end_time is not None else start + 100

        if st_num == 3:
            relay_req = (
                f"/relayservice?deviceid={device_id}"
                f"&type=download&bidirectionid={player}&channel=0"
            )
            local_req = (
                f"/stream/download?client_id={player}"
                f"&media_type=1&start_time={start}&end_time={end}"
                f"&channels=0&type=0"
            )
        elif st_num == 1:
            relay_req = (
                f"/relayservice?deviceid={device_id}"
                f"&type=sdvod&playerid={player}&channel=0"
            )
            local_req = f"/stream/playback?client_id={player}&start_time={start}"
        elif st_num == 2:
            relay_req = (
                f"/relayservice?deviceid={device_id}"
                f"&type=dtspk&playerid={player}&channel=0"
            )
            local_req = "/stream/audio?mode=vad"
        else:
            relay_req = (
                f"/relayservice?deviceid={device_id}"
                f"&type=video&resolution=HD&channel=0"
            )
            local_req = "/stream/video/h264?resolution=hd&channel=0"

        body_obj = {
            "method": "do",
            "relay": {
                "request_relay": {
                    "version": version,
                    "token": account,
                    "stream_type": st_num,
                    "protocol": int(protocol),
                    "relay_req_url": relay_req,
                    "local_req_url": local_req,
                    "relay_server": relay_server,
                    "relays_port": relays_port,
                    "relay_ip": relay_server,
                    "relay_port": relay_port,
                    "cookie": cookie,
                    "extra_data": {},
                    "share_info": {},
                }
            },
        }
        try:
            resp_data = await self.cloud.async_passthrough(device_id, body_obj)
        except TPLinkCloudError as err:
            raise TPLinkCloudRelayError(
                f"request_relay passthrough failed: {err}",
                error_code=getattr(err, "error_code", None),
                raw=getattr(err, "raw", None),
            ) from err

        data: dict[str, Any]
        if isinstance(resp_data, dict):
            data = resp_data
        else:
            data = {"responseData": resp_data}

        result = data.get("result") if isinstance(data.get("result"), dict) else data
        if isinstance(result, dict):
            code = result.get("error_code", data.get("error_code"))
            if code not in (0, None, "0"):
                raise TPLinkCloudRelayError(
                    f"request_relay device error_code={code}: {result}",
                    error_code=int(code) if str(code).lstrip("-").isdigit() else None,
                    raw=data,
                )
            if result.get("sid"):
                self.relay_token = str(result["sid"])
            nested = result.get("result") if isinstance(result.get("result"), dict) else None
            if nested and nested.get("sid"):
                self.relay_token = str(nested["sid"])
        if self.relay_host is None:
            self.relay_host = host
        # Prefer the relayServer from getRelayServerKey for later WSS
        if relay_server and not str(relay_server).startswith("http"):
            self.relay_host = f"https://{relay_server}"
        elif relay_server:
            self.relay_host = str(relay_server).rstrip("/")
        self._persist_tokens(
            {
                "request_relay": data,
                "relaySid": self.relay_token,
                "relayCookie": cookie,
                "playerId": player,
            }
        )
        return data

    async def async_request_p2p(
        self,
        device_id: str,
        *,
        pub_ip: str,
        pub_port: int,
        pri_ip: str | None = None,
        pri_port: int | None = None,
        stun_url: str = "175.24.184.201:3478",
        nat_type: int = 1,
        version: str | None = None,
        app_imp_version: int | str = 4,
        ready_timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Negotiate LAN/WAN P2P via ``p2p.request_p2p`` passthrough.

        Static RE (``BuildUnderlineRequestStun``):
          ``{"method":"do","p2p":{"request_p2p":{version, app_imp_version:4,
            stun_url, pub_ip/port, pri_ip/port, nat_type}}}``
          ``app_imp_version`` is a **number** (literal 4.0 in SO).

        Live (2026-07-20):
          - Living (IPC59AE): ``version=1.1`` often returns ports immediately.
          - Guest/study (IPC44GW): ``version=1.1`` → ``-52001``; ``version=2.0``
            returns ``sid`` with ``is_ready=0`` → poll ``is_p2p_ready`` until
            ``is_ready=1`` + ``pri_port``. Punch OK; media is QUIC
            ``0xff000012`` (``p2p_quic.py``), not TPPipe ``0x80``.
        """
        import asyncio

        pri_ip = pri_ip or pub_ip
        pri_port = int(pri_port if pri_port is not None else pub_port)
        versions = [version] if version else ["1.1", "2.0"]
        last_err: TPLinkCloudRelayError | None = None
        result: dict[str, Any] | None = None
        resp_data: dict[str, Any] | None = None

        for ver in versions:
            if ver is None:
                continue
            body_obj: dict[str, Any] = {
                "method": "do",
                "p2p": {
                    "request_p2p": {
                        "version": ver,
                        "app_imp_version": app_imp_version,
                        "stun_url": stun_url,
                        "pub_ip": pub_ip,
                        "pub_port": int(pub_port),
                        "pri_ip": pri_ip,
                        "pri_port": pri_port,
                        "nat_type": int(nat_type),
                    }
                },
            }
            try:
                raw = await self.cloud.async_passthrough(device_id, body_obj)
            except TPLinkCloudError as err:
                last_err = TPLinkCloudRelayError(
                    f"request_p2p passthrough failed: {err}",
                    error_code=getattr(err, "error_code", None),
                    raw=getattr(err, "raw", None),
                )
                continue
            if not isinstance(raw, dict):
                last_err = TPLinkCloudRelayError(
                    f"request_p2p unexpected response: {raw!r}",
                    raw=raw,
                )
                continue
            code = raw.get("error_code")
            res = raw.get("result") if isinstance(raw.get("result"), dict) else {}
            if code not in (0, None, "0"):
                last_err = TPLinkCloudRelayError(
                    f"request_p2p device error_code={code} ver={ver}",
                    error_code=int(code) if str(code).lstrip("-").isdigit() else None,
                    raw=raw,
                )
                # -52001: try next version (IPC44GW needs 2.0)
                if code == -52001:
                    continue
                raise last_err
            if not res.get("sid"):
                last_err = TPLinkCloudRelayError(
                    f"request_p2p missing sid: {raw}",
                    raw=raw,
                )
                continue
            result = res
            resp_data = raw
            break

        if result is None or resp_data is None:
            raise last_err or TPLinkCloudRelayError("request_p2p failed")

        # Poll is_p2p_ready when ports not yet present (IPC44GW)
        if result.get("pri_port") is None or result.get("is_ready") in (0, "0", False):
            sid = str(result["sid"])
            deadline = asyncio.get_running_loop().time() + ready_timeout
            while asyncio.get_running_loop().time() < deadline:
                ready_body = {
                    "method": "do",
                    "p2p": {"is_p2p_ready": {"sid": sid}},
                }
                try:
                    ready_raw = await self.cloud.async_passthrough(
                        device_id, ready_body
                    )
                except TPLinkCloudError as err:
                    _LOGGER.debug("is_p2p_ready: %s", err)
                    await asyncio.sleep(0.4)
                    continue
                if not isinstance(ready_raw, dict):
                    await asyncio.sleep(0.4)
                    continue
                ready_res = (
                    ready_raw.get("result")
                    if isinstance(ready_raw.get("result"), dict)
                    else {}
                )
                if ready_raw.get("error_code") not in (0, None, "0"):
                    await asyncio.sleep(0.4)
                    continue
                if ready_res.get("is_ready") in (1, "1", True) and ready_res.get(
                    "pri_port"
                ) is not None:
                    result = ready_res
                    resp_data = ready_raw
                    break
                await asyncio.sleep(0.4)
            else:
                if result.get("pri_port") is None:
                    raise TPLinkCloudRelayError(
                        f"is_p2p_ready timeout sid={result.get('sid')}",
                        raw=resp_data,
                    )

        if not result.get("sid") or result.get("pri_port") is None:
            raise TPLinkCloudRelayError(
                f"request_p2p missing sid/pri_port: {resp_data}",
                raw=resp_data,
            )
        self._persist_tokens({"request_p2p": resp_data, "p2pSid": result.get("sid")})
        return result

    async def async_download_playback(
        self,
        device_id: str,
        start_time: int | str,
        end_time: int | str,
        *,
        channel: int = 0,
        max_bytes: int = 12_000_000,
        max_seconds: float = 40.0,
        stream_type: str = "sdvod",
        device: dict[str, Any] | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        """Open relayservice multipart and collect video/mp2t bytes."""
        self._load_captured_tokens()
        if device:
            try:
                await self.async_prepare_media_session(
                    device, stream_type=stream_type, channel_id=channel
                )
            except TPLinkCloudRelayError as err:
                _LOGGER.debug("prepare_media_session: %s", err)
        elif not self.biz_token and not self.relay_token:
            try:
                await self.async_get_biz_token()
            except TPLinkCloudRelayError as err:
                _LOGGER.debug("getBizToken skipped: %s", err)

        hosts: list[str] = []
        if self.relay_host:
            hosts.append(self.relay_host)
        hosts.extend(h for h in self.relay_hosts if h not in hosts)

        last_err: Exception | None = None
        for host in hosts:
            try:
                await self.async_request_relay(
                    device_id, stream_type=stream_type, host=host
                )
            except TPLinkCloudRelayError as err:
                _LOGGER.debug("request_relay on %s: %s", host, err)
            try:
                return await self._async_pull_stream(
                    host,
                    device_id,
                    start_time,
                    end_time,
                    channel=channel,
                    max_bytes=max_bytes,
                    max_seconds=max_seconds,
                    stream_type=stream_type,
                )
            except TPLinkCloudRelayError as err:
                last_err = err
                _LOGGER.debug("relayservice on %s failed: %s", host, err)
        raise TPLinkCloudRelayError(
            f"cloud relay playback failed: {last_err}"
        )

    async def _async_pull_stream(
        self,
        host: str,
        device_id: str,
        start_time: int | str,
        end_time: int | str,
        *,
        channel: int,
        max_bytes: int,
        max_seconds: float,
        stream_type: str,
    ) -> tuple[bytes, dict[str, Any]]:
        token = self._media_token()
        url = (
            f"{host.rstrip('/')}/relayservice"
            f"?deviceid={device_id}&type={stream_type}&resolution=0"
        )
        headers = {
            "Content-Type": f"multipart/mixed; boundary={CLIENT_BOUNDARY}",
            "User-Agent": "TP-LINK_APP",
            "X-Token": token,
            "X-Client-SessionID": uuid.uuid4().hex,
            "X-Service-Type": "ipc",
            "X-Client-Id": self.cloud.terminal_uuid,
            "X-PrePostStream-FirstPreview": "0",
            "Keep-Relay": "1",
            "Connection": "keep-alive",
            "Account-Token": self.cloud.token or "",
            "Term-Id": self.cloud.terminal_uuid,
            "Biz-Token": self.biz_token_media or self.biz_token or "",
        }
        playback_cmd = {
            "type": "request",
            "seq": 1,
            "params": {
                "method": "get",
                "playback": {
                    "client_id": 2,
                    "start_time": str(start_time),
                    "end_time": str(end_time),
                    "scale": "1/1",
                    "channels": [int(channel)],
                    "event_type": [1, 2],
                },
            },
        }
        body = (
            f"--{CLIENT_BOUNDARY}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: 0\r\n\r\n"
            f"--{CLIENT_BOUNDARY}\r\n"
            f"Content-Type: application/json\r\n\r\n"
            f"{json.dumps(playback_cmd)}\r\n"
            f"--{CLIENT_BOUNDARY}--\r\n"
        ).encode()

        meta: dict[str, Any] = {
            "source": "cloud_relay",
            "host": host,
            "device_id": device_id,
            "stream_type": stream_type,
            "http_status": None,
        }
        chunks: list[bytes] = []
        total = 0
        t0 = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=max_seconds + 15, sock_read=max_seconds)
        async with self._session.post(
            url, data=body, headers=headers, timeout=timeout
        ) as resp:
            meta["http_status"] = resp.status
            if resp.status != 200:
                text = await resp.text()
                raise TPLinkCloudRelayError(
                    f"relayservice HTTP {resp.status}: {text[:200]}",
                    raw=meta,
                )
            ctype = resp.headers.get("Content-Type", "")
            meta["content_type"] = ctype
            async for chunk in resp.content.iter_chunked(65536):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes or (time.monotonic() - t0) > max_seconds:
                    break

        raw = b"".join(chunks)
        ts = _extract_mpegts(raw)
        meta["raw_bytes"] = len(raw)
        meta["ts_bytes"] = len(ts)
        if len(ts) < 1024:
            raise TPLinkCloudRelayError(
                f"empty cloud relay stream ({len(ts)} ts bytes / {len(raw)} raw)",
                raw=meta,
            )
        return ts, meta


def _extract_mpegts(data: bytes) -> bytes:
    """Pull MPEG-TS sync bytes (0x47) from multipart or raw body."""
    if not data:
        return b""
    if data[:1] == b"\x47" and len(data) >= 188:
        return data
    idx = data.find(b"\x47")
    if idx < 0:
        return b""
    out = bytearray()
    i = idx
    while i + 188 <= len(data):
        if data[i] != 0x47:
            nxt = data.find(b"\x47", i + 1)
            if nxt < 0:
                break
            i = nxt
            continue
        out.extend(data[i : i + 188])
        i += 188
    return bytes(out) if out else data[idx:]

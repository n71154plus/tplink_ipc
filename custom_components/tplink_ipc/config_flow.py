"""Config flow for TP-Link China IPC (local device or App cloud account)."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.device_registry import format_mac

from .api import TPLinkIPCAuthError, TPLinkIPCClient, TPLinkIPCError, TPLinkIPCLockedError
from .cloud import (
    DEFAULT_APP_TYPE,
    DEFAULT_CLOUD_HOST,
    TPLinkCloudAuthError,
    TPLinkCloudClient,
    TPLinkCloudError,
    TPLinkCloudLockedError,
)
from .const import (
    CONF_APP_TYPE,
    CONF_CLOUD_HOST,
    CONF_CONNECTION_TYPE,
    CONF_RTSP_PORT,
    CONF_STREAM_PATH,
    CONF_TERMINAL_UUID,
    CONF_USE_HTTPS,
    CONF_VERIFY_SSL,
    CONN_CLOUD,
    CONN_LOCAL,
    DEFAULT_PORT,
    DEFAULT_RTSP_PORT,
    DEFAULT_STREAM_PATH,
    DEFAULT_USERNAME,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_LOCAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_USE_HTTPS, default=False): bool,
        vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
        vol.Optional(CONF_RTSP_PORT, default=DEFAULT_RTSP_PORT): int,
        vol.Optional(CONF_STREAM_PATH, default=DEFAULT_STREAM_PATH): str,
    }
)

STEP_CLOUD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_local(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    client = TPLinkIPCClient(
        hass,
        host=data[CONF_HOST],
        port=data[CONF_PORT],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        use_https=data.get(CONF_USE_HTTPS, False),
        verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        rtsp_port=data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
        stream_path=data.get(CONF_STREAM_PATH, DEFAULT_STREAM_PATH),
    )
    info = await client.async_test_connection()
    return {
        "title": client.device_name,
        "mac": client.mac,
        "model": client.model,
        "raw": info,
    }


async def _validate_cloud(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    client = TPLinkCloudClient(
        hass,
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        app_type=data.get(CONF_APP_TYPE, DEFAULT_APP_TYPE),
        host=data.get(CONF_CLOUD_HOST, DEFAULT_CLOUD_HOST),
        terminal_uuid=data.get(CONF_TERMINAL_UUID),
    )
    await client.async_login()
    devices = await client.async_get_device_list()
    return {
        "title": f"TP-Link 雲端 ({data[CONF_USERNAME]})",
        "terminal_uuid": client.terminal_uuid,
        "device_count": len(devices),
    }


class TPLinkIPCConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="user",
            menu_options=["local", "cloud"],
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                result = await _validate_local(self.hass, user_input)
            except TPLinkIPCLockedError as err:
                errors["base"] = "device_locked"
                _LOGGER.warning(
                    "Camera %s is login-locked (sec_left=%s)",
                    user_input.get(CONF_HOST),
                    err.sec_left,
                )
            except TPLinkIPCAuthError:
                errors["base"] = "invalid_auth"
            except TPLinkIPCError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                unique = result.get("mac") or user_input[CONF_HOST]
                if result.get("mac"):
                    unique = format_mac(result["mac"])
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                data = {**user_input, CONF_CONNECTION_TYPE: CONN_LOCAL}
                return self.async_create_entry(title=result["title"], data=data)

        return self.async_show_form(
            step_id="local",
            data_schema=STEP_LOCAL_SCHEMA,
            errors=errors,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                result = await _validate_cloud(self.hass, user_input)
            except TPLinkCloudLockedError:
                errors["base"] = "cloud_locked"
            except TPLinkCloudAuthError:
                errors["base"] = "invalid_auth"
            except TPLinkCloudError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected cloud login exception")
                errors["base"] = "unknown"
            else:
                unique = f"cloud:{user_input[CONF_USERNAME].strip().lower()}"
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_CONNECTION_TYPE: CONN_CLOUD,
                    CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_TERMINAL_UUID: result["terminal_uuid"],
                    CONF_APP_TYPE: DEFAULT_APP_TYPE,
                    CONF_CLOUD_HOST: DEFAULT_CLOUD_HOST,
                }
                return self.async_create_entry(title=result["title"], data=data)

        return self.async_show_form(
            step_id="cloud",
            data_schema=STEP_CLOUD_SCHEMA,
            errors=errors,
        )

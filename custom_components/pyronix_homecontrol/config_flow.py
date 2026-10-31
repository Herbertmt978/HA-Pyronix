"""UI-managed connection details and credential rotation; no YAML configuration."""

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from aiopyronix import ProtocolError, validate_auth
import probatio

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
CREDENTIALS = probatio.Schema(
    {
        probatio.Required("app_password"): PASSWORD,
        probatio.Required("user_code"): PASSWORD,
    }
)


class PyronixConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a panel from the connection details the setup helper prepares."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept the prepared connection details for one panel."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                auth = validate_auth(json.loads(user_input["connection_details"]))
            except ValueError, ProtocolError:
                errors["base"] = "invalid_connection"
            else:
                await self.async_set_unique_id(
                    hashlib.sha256(auth["PanelId"].encode()).hexdigest()[:24]
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=auth["SystemName"], data=auth)
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {probatio.Required("connection_details"): PASSWORD}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the app password or user code."""
        return self._update_credentials(
            "reconfigure", self._get_reconfigure_entry(), user_input
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Ask for credentials after the panel rejected the saved ones."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept the current app password and user code."""
        return self._update_credentials(
            "reauth_confirm", self._get_reauth_entry(), user_input
        )

    def _update_credentials(
        self, step_id: str, entry: ConfigEntry, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        # Only the app password and user code change; the panel identity stays fixed.
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                **entry.data,
                "PanelPwd": user_input["app_password"],
                "UserCode": user_input["user_code"],
            }
            try:
                validate_auth(data)
            except ProtocolError:
                errors["base"] = "invalid_connection"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id=step_id, data_schema=CREDENTIALS, errors=errors
        )

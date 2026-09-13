"""UI-managed connection details and credential rotation; no YAML configuration."""

import hashlib
import json

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from . import DOMAIN
from .client import ProtocolError, validate_auth

PASSWORD = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


class PyronixConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                auth = validate_auth(json.loads(user_input["connection_details"]))
            except ValueError, KeyError, ProtocolError:
                errors["base"] = "invalid_connection"
            else:
                await self.async_set_unique_id(
                    hashlib.sha256(auth["PanelId"].encode()).hexdigest()[:24]
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=auth["SystemName"], data=auth)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("connection_details"): PASSWORD}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            data = dict(entry.data)
            data.update(PanelPwd=user_input["app_password"], UserCode=user_input["user_code"])
            try:
                validate_auth(data)
            except ProtocolError:
                errors["base"] = "invalid_connection"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required("app_password"): PASSWORD, vol.Required("user_code"): PASSWORD}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data = dict(entry.data)
            data.update(PanelPwd=user_input["app_password"], UserCode=user_input["user_code"])
            try:
                validate_auth(data)
            except ProtocolError:
                errors["base"] = "invalid_connection"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required("app_password"): PASSWORD, vol.Required("user_code"): PASSWORD}
            ),
            errors=errors,
        )

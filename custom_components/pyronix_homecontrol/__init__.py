"""Native Home Assistant support for the owner's HomeControl panel."""

import hashlib
import logging
from datetime import timedelta

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AuthenticationError, PanelBusyError, PanelClient, ProtocolError

DOMAIN = "pyronix_homecontrol"
LOGGER = logging.getLogger(__name__)
NORMAL_INTERVAL = timedelta(minutes=2)
SETTING_INTERVAL = timedelta(seconds=10)


class PanelCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(
            hass,
            LOGGER,
            name="Pyronix HomeControl",
            config_entry=entry,
            update_interval=NORMAL_INTERVAL,
        )
        self.client = PanelClient(async_get_clientsession(hass), entry.data)
        self.device_id = hashlib.sha256(entry.data["PanelId"].encode()).hexdigest()[:24]

    async def _async_update_data(self):
        try:
            data = await self.client.query()
        except PanelBusyError as exc:
            # An active manual command will publish its result when it completes.
            # Preserve the last observation instead of calling this a connection failure.
            self.update_interval = SETTING_INTERVAL
            if self.data is not None:
                return self.data
            raise UpdateFailed(str(exc)) from None
        except AuthenticationError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from None
        except ProtocolError as exc:
            self.update_interval = NORMAL_INTERVAL
            raise UpdateFailed(str(exc)) from None
        self._set_interval(data)
        return data

    def _set_interval(self, data):
        self.update_interval = (
            SETTING_INTERVAL
            if any(row["value"] == 3 for row in data["areas"].values())
            else NORMAL_INTERVAL
        )

    @callback
    def async_set_updated_data(self, data):
        self._set_interval(data)
        super().async_set_updated_data(data)


async def async_setup_entry(hass, entry):
    coordinator = PanelCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.ALARM_CONTROL_PANEL])
    return True


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, [Platform.ALARM_CONTROL_PANEL])

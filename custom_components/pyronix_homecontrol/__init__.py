"""Native Home Assistant support for the owner's HomeControl panel."""

import hashlib
import logging
from datetime import timedelta

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AuthenticationError, PanelClient, ProtocolError

DOMAIN = "pyronix_homecontrol"
LOGGER = logging.getLogger(__name__)


class PanelCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(
            hass,
            LOGGER,
            name="Pyronix HomeControl",
            config_entry=entry,
            update_interval=timedelta(minutes=2),
        )
        self.client = PanelClient(async_get_clientsession(hass), entry.data)
        self.device_id = hashlib.sha256(entry.data["PanelId"].encode()).hexdigest()[:24]

    async def _async_update_data(self):
        try:
            return await self.client.query()
        except AuthenticationError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from None
        except ProtocolError as exc:
            raise UpdateFailed(str(exc)) from None


async def async_setup_entry(hass, entry):
    coordinator = PanelCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.ALARM_CONTROL_PANEL])
    return True


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, [Platform.ALARM_CONTROL_PANEL])

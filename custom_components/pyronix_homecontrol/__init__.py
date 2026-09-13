"""Home Assistant owns discovery; the client owns the explicitly opened session."""

import hashlib
import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import PanelClient

DOMAIN = "pyronix_homecontrol"
LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.ALARM_CONTROL_PANEL]


class PanelCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(
            hass, LOGGER, name="Pyronix HomeControl", config_entry=entry, update_interval=None
        )
        self.device_id = hashlib.sha256(entry.data["PanelId"].encode()).hexdigest()[:24]
        self.catalog = {"areas": [], "firmware": None}
        self._store = Store(hass, 1, DOMAIN + "." + entry.entry_id)
        self.data = PanelClient._empty_snapshot()
        self.last_update_success = False
        self.client = PanelClient(
            async_get_clientsession(hass),
            entry.data,
            on_update=self._receive_snapshot,
            on_state=self._connection_changed,
            create_task=lambda coro: hass.async_create_background_task(coro, "Pyronix session"),
        )

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            name="Pyronix " + self.config_entry.title,
            manufacturer="Pyronix",
            sw_version=self.catalog.get("firmware"),
        )

    async def async_load_catalog(self):
        saved = await self._store.async_load()
        if saved:
            self.catalog = saved
            self.data["areas"] = {
                row["index"]: {"name": row["name"], "value": None, "status": "Unknown"}
                for row in saved.get("areas", [])
            }

    @callback
    def _receive_snapshot(self, data):
        permitted = data["can_arm"] | data["can_disarm"]
        data = {**data, "areas": {i: row for i, row in data["areas"].items() if i in permitted}}
        catalog = {
            "areas": [
                {"index": i, "name": row["name"]} for i, row in sorted(data["areas"].items())
            ],
            "firmware": data.get("firmware", self.catalog.get("firmware")),
        }
        if catalog != self.catalog:
            self.catalog = catalog
            self._store.async_delay_save(lambda: self.catalog, 1)
        self.async_set_updated_data(data)

    @callback
    def _connection_changed(self):
        self.last_update_success = self.client.connected and self.data["observed_at"] is not None
        self.async_update_listeners()

    async def _async_update_data(self):
        # A generic HA refresh must not silently open a disconnected alarm session.
        if not self.client.connected:
            raise UpdateFailed("Disconnected. Press Connect to read the panel.")
        return self.client.snapshot()

    async def async_connect(self):
        await self.client.connect()

    async def async_disconnect(self):
        await self.client.disconnect()


async def async_setup_entry(hass, entry):
    coordinator = PanelCoordinator(hass, entry)
    await coordinator.async_load_catalog()
    entry.runtime_data = coordinator

    async def stop_session(event):
        await coordinator.async_disconnect()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_session))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    await entry.runtime_data.async_disconnect()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

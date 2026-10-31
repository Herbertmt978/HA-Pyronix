"""Shares the explicitly opened panel session with the panel's entities."""

import hashlib
import logging
from typing import TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AuthenticationError, PanelClient, Snapshot, empty_snapshot
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

type PyronixConfigEntry = ConfigEntry[PanelCoordinator]


class CatalogArea(TypedDict):
    """A permitted area kept so its entity exists before the next Connect."""

    index: int
    name: str


class Catalog(TypedDict):
    """Metadata kept across restarts; never alarm state, permissions or credentials."""

    areas: list[CatalogArea]
    firmware: str | None


class PanelCoordinator(DataUpdateCoordinator[Snapshot]):
    """Publish snapshots from the session the user opened; never poll."""

    config_entry: PyronixConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PyronixConfigEntry) -> None:
        """Prepare the panel client without connecting."""
        super().__init__(
            hass,
            _LOGGER,
            name="Pyronix HomeControl",
            config_entry=entry,
            update_interval=None,
        )
        self.device_id = hashlib.sha256(entry.data["PanelId"].encode()).hexdigest()[:24]
        self.catalog: Catalog = {"areas": [], "firmware": None}
        self._store: Store[Catalog] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._session_failed = False
        self.data = empty_snapshot()
        self.last_update_success = False
        self.client = PanelClient(
            async_get_clientsession(hass),
            entry.data,
            on_update=self._receive_snapshot,
            on_state=self._connection_changed,
            create_task=lambda coro: entry.async_create_background_task(
                hass, coro, "Pyronix session"
            ),
        )

    async def async_load_catalog(self) -> None:
        """Restore permitted area names; their state stays unknown until Connect."""
        if saved := await self._store.async_load():
            self.catalog = saved
            self.data["areas"] = {
                row["index"]: {"name": row["name"], "value": None, "status": "Unknown"}
                for row in saved["areas"]
            }

    @callback
    def _receive_snapshot(self, data: Snapshot) -> None:
        permitted = data["can_arm"] | data["can_disarm"]
        data["areas"] = {i: row for i, row in data["areas"].items() if i in permitted}
        catalog: Catalog = {
            "areas": [
                {"index": i, "name": row["name"]}
                for i, row in sorted(data["areas"].items())
            ],
            "firmware": data.get("firmware", self.catalog["firmware"]),
        }
        if catalog != self.catalog:
            if catalog["firmware"] != self.catalog["firmware"]:
                self._update_firmware(catalog["firmware"])
            self.catalog = catalog
            self._store.async_delay_save(lambda: self.catalog, 1)
        self.async_set_updated_data(data)

    @callback
    def _update_firmware(self, firmware: str | None) -> None:
        # Device info is applied only when entities are added; a later upgrade needs this.
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            registry.async_update_device(device.id, sw_version=firmware)

    @callback
    def _connection_changed(self) -> None:
        if self.client.state == "error" and not self._session_failed:
            self._session_failed = True
            _LOGGER.info("Pyronix session ended: %s", self.client.detail)
        elif self.client.connected and self._session_failed:
            self._session_failed = False
            _LOGGER.info("Pyronix session connected again")
        self.last_update_success = (
            self.client.connected and self.data["observed_at"] is not None
        )
        self.async_update_listeners()

    async def _async_update_data(self) -> Snapshot:
        # A generic HA refresh must not silently open a disconnected alarm session.
        if not self.client.connected:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="not_connected"
            )
        return self.client.snapshot()

    async def async_connect(self) -> None:
        """Open or extend the session; ask for new credentials if they are rejected."""
        try:
            await self.client.connect()
        except AuthenticationError:
            # A rejection is not retried; ask for the changed app password or user code.
            self.config_entry.async_start_reauth(self.hass)
            raise

    async def async_disconnect(self) -> None:
        """Close the session without changing any area."""
        await self.client.disconnect()

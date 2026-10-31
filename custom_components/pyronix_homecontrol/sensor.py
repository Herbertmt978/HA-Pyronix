"""Visible connection state independent of area availability."""

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PanelCoordinator, PyronixConfigEntry
from .entity import PyronixEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PyronixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Connection sensor."""
    async_add_entities([ConnectionSensor(entry.runtime_data)])


class ConnectionSensor(PyronixEntity, SensorEntity):
    """Whether HA is disconnected, connecting, connected, or stopped by an error."""

    _attr_translation_key = "connection"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["disconnected", "connecting", "connected", "error"]

    def __init__(self, coordinator: PanelCoordinator) -> None:
        """Attach the sensor to the panel device."""
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        """Report the session state even while the areas are unavailable."""
        return True

    @property
    def native_value(self) -> str:
        """Return the session state."""
        return self.coordinator.client.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the fixed error explanation and idle expiry."""
        client = self.coordinator.client
        return {
            "detail": client.detail,
            "idle_timeout_seconds": client.IDLE_TIMEOUT,
            "idle_expires_at": datetime.fromtimestamp(
                client.idle_expires_at, UTC
            ).isoformat()
            if client.idle_expires_at is not None
            else None,
        }

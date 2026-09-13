"""Visible connection state independent of area availability."""

from datetime import UTC, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ConnectionSensor(entry.runtime_data)])


class ConnectionSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Connection"
    _attr_icon = "mdi:connection"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["disconnected", "connecting", "connected", "error"]

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.device_id + "_connection"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        return self.coordinator.client.state

    @property
    def extra_state_attributes(self):
        client = self.coordinator.client
        return {
            "detail": client.detail,
            "idle_timeout_seconds": client.IDLE_TIMEOUT,
            "idle_expires_at": datetime.fromtimestamp(client.idle_expires_at, UTC).isoformat()
            if client.idle_expires_at is not None
            else None,
        }

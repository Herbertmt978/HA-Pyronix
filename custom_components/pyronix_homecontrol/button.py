"""Connection controls remain available when the alarm areas are unavailable."""

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import ProtocolError


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        ConnectionButton(entry.runtime_data, action) for action in ("connect", "disconnect")
    )


class ConnectionButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, action):
        super().__init__(coordinator)
        self.action = action
        self._attr_name = action.title()
        self._attr_unique_id = coordinator.device_id + "_" + action
        self._attr_icon = "mdi:lan-connect" if action == "connect" else "mdi:lan-disconnect"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self):
        return True

    async def async_press(self):
        try:
            if self.action == "connect":
                await self.coordinator.async_connect()
            else:
                await self.coordinator.async_disconnect()
        except ProtocolError as exc:
            raise HomeAssistantError(str(exc)) from None

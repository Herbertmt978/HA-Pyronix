"""Four independent native alarm areas; physical user code required to control."""

import hmac
from datetime import UTC, datetime

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import ProtocolError


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known = set()

    def discover():
        indices = set(coordinator.data["areas"]) - known
        if indices:
            known.update(indices)
            async_add_entities(PanelArea(coordinator, index) for index in sorted(indices))

    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()


class PanelArea(CoordinatorEntity, AlarmControlPanelEntity):
    _attr_has_entity_name = True
    _attr_code_format = CodeFormat.NUMBER
    _attr_code_arm_required = True

    def __init__(self, coordinator, index):
        super().__init__(coordinator)
        self.index = index
        self._attr_name = coordinator.data["areas"][index]["name"]
        self._attr_unique_id = f"{coordinator.device_id}_area_{index}"
        self._night = self._attr_name.casefold() == "night set"
        self._attr_supported_features = (
            AlarmControlPanelEntityFeature.ARM_NIGHT
            if self._night
            else AlarmControlPanelEntityFeature.ARM_AWAY
        )
        self._attr_device_info = coordinator.device_info

    @property
    def available(self):
        return (
            super().available
            and self.coordinator.client.connected
            and self.index in self.coordinator.data["areas"]
        )

    @property
    def alarm_state(self):
        value = self.coordinator.data["areas"].get(self.index, {}).get("value")
        if value == 1:
            return (
                AlarmControlPanelState.ARMED_NIGHT
                if self._night
                else AlarmControlPanelState.ARMED_AWAY
            )
        return {
            0: AlarmControlPanelState.DISARMED,
            2: AlarmControlPanelState.TRIGGERED,
            3: AlarmControlPanelState.ARMING,
            4: AlarmControlPanelState.DISARMED,
            5: AlarmControlPanelState.DISARMED,
            6: AlarmControlPanelState.DISARMED,
        }.get(value)

    @property
    def extra_state_attributes(self):
        return {
            "panel_status": self.coordinator.data["areas"].get(self.index, {}).get("status"),
            "last_observed": datetime.fromtimestamp(
                self.coordinator.data["observed_at"], UTC
            ).isoformat()
            if self.coordinator.data["observed_at"] is not None
            else None,
            "area_number": self.index,
            "state_source": "Pyronix panel",
            "connection": self.coordinator.client.state,
        }

    async def _control(self, operation, code):
        if (
            not isinstance(code, str)
            or not code.isascii()
            or not hmac.compare_digest(code, self.coordinator.client.auth["UserCode"])
        ):
            raise ServiceValidationError("Incorrect Pyronix user code")
        if not self.available:
            raise HomeAssistantError("Disconnected. Press Connect before choosing an alarm action.")
        try:
            await self.coordinator.client.control(operation, self.index)
        except ProtocolError as exc:
            raise HomeAssistantError(str(exc)) from None

    async def async_alarm_arm_away(self, code=None):
        await self._control("arm", code)

    async def async_alarm_arm_night(self, code=None):
        await self._control("arm", code)

    async def async_alarm_disarm(self, code=None):
        await self._control("disarm", code)

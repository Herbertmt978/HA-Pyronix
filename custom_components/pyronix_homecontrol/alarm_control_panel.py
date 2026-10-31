"""Independent native alarm areas; the physical user code is required to control them."""

from datetime import UTC, datetime
import hmac
from typing import Any

from aiopyronix import AreaState, CommandUnconfirmedError, PanelBusyError, ProtocolError

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import PanelCoordinator, PyronixConfigEntry
from .entity import PyronixEntity

PARALLEL_UPDATES = 0

STATES = {
    0: AlarmControlPanelState.DISARMED,
    2: AlarmControlPanelState.TRIGGERED,
    3: AlarmControlPanelState.ARMING,
    4: AlarmControlPanelState.DISARMED,
    5: AlarmControlPanelState.DISARMED,
    6: AlarmControlPanelState.DISARMED,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PyronixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each permitted area, including areas first reported after setup."""
    coordinator = entry.runtime_data
    known: set[int] = set()

    @callback
    def _async_add_new_areas() -> None:
        if new := set(coordinator.data["areas"]) - known:
            known.update(new)
            async_add_entities(PanelArea(coordinator, index) for index in sorted(new))

    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_areas))
    _async_add_new_areas()


class PanelArea(PyronixEntity, AlarmControlPanelEntity):
    """One panel area; `Night Set` arms with the night action, others with away."""

    _attr_code_format = CodeFormat.NUMBER
    _attr_code_arm_required = True

    def __init__(self, coordinator: PanelCoordinator, index: int) -> None:
        """Name the area as the panel does."""
        super().__init__(coordinator, f"area_{index}")
        self.index = index
        self._attr_name = name = coordinator.data["areas"][index]["name"]
        self._night = name.casefold() == "night set"
        self._attr_supported_features = (
            AlarmControlPanelEntityFeature.ARM_NIGHT
            if self._night
            else AlarmControlPanelEntityFeature.ARM_AWAY
        )

    @property
    def available(self) -> bool:
        """Only a live session can vouch for the area's state."""
        return (
            super().available
            and self.coordinator.client.connected
            and self._area is not None
        )

    @property
    def _area(self) -> AreaState | None:
        return self.coordinator.data["areas"].get(self.index)

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the area state; unfamiliar panel values are unknown."""
        value = area["value"] if (area := self._area) else None
        if value == 1:
            return (
                AlarmControlPanelState.ARMED_NIGHT
                if self._night
                else AlarmControlPanelState.ARMED_AWAY
            )
        return STATES.get(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the panel's own status wording and when it was last observed."""
        observed_at = self.coordinator.data["observed_at"]
        return {
            "panel_status": area["status"] if (area := self._area) else None,
            "last_observed": datetime.fromtimestamp(observed_at, UTC).isoformat()
            if observed_at is not None
            else None,
            "area_number": self.index,
        }

    async def _async_control(self, operation: str, code: str | None) -> None:
        if (
            not isinstance(code, str)
            or not code.isascii()
            or not hmac.compare_digest(code, self.coordinator.client.auth["UserCode"])
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="incorrect_code"
            )
        try:
            await self.coordinator.client.control(operation, self.index)
        except PanelBusyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_in_progress"
            ) from err
        except CommandUnconfirmedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_unconfirmed"
            ) from err
        except ProtocolError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Arm this area."""
        await self._async_control("arm", code)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Arm the night area."""
        await self._async_control("arm", code)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Disarm this area."""
        await self._async_control("disarm", code)

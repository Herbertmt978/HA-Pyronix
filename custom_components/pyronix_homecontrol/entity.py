"""Device and naming shared by every entity of one panel."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PanelCoordinator


class PyronixEntity(CoordinatorEntity[PanelCoordinator]):
    """An entity of the panel device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PanelCoordinator, key: str) -> None:
        """Attach the entity to the panel device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            name=f"Pyronix {coordinator.config_entry.title}",
            manufacturer="Pyronix",
            sw_version=coordinator.catalog["firmware"],
        )

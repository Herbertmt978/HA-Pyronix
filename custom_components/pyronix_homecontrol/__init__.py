"""The Pyronix HomeControl integration; the user opens and closes each panel session."""

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant

from .coordinator import PanelCoordinator, PyronixConfigEntry

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BUTTON,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: PyronixConfigEntry) -> bool:
    """Set up the panel's entities without connecting to it."""
    coordinator = PanelCoordinator(hass, entry)
    await coordinator.async_load_catalog()
    entry.runtime_data = coordinator

    async def _async_stop_session(event: Event) -> None:
        await coordinator.async_disconnect()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_session)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PyronixConfigEntry) -> bool:
    """Close any open session and unload the entities."""
    await entry.runtime_data.async_disconnect()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

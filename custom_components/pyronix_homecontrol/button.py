"""Connection controls remain available when the alarm areas are unavailable."""

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import AuthenticationError
from .const import DOMAIN
from .coordinator import PanelCoordinator, PyronixConfigEntry
from .entity import PyronixEntity
from .panel_protocol import ProtocolError

PARALLEL_UPDATES = 0

BUTTONS = (
    ButtonEntityDescription(key="connect", translation_key="connect"),
    ButtonEntityDescription(key="disconnect", translation_key="disconnect"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PyronixConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Connect and Disconnect buttons."""
    async_add_entities(
        ConnectionButton(entry.runtime_data, description) for description in BUTTONS
    )


class ConnectionButton(PyronixEntity, ButtonEntity):
    """Open or close the panel session."""

    def __init__(
        self, coordinator: PanelCoordinator, description: ButtonEntityDescription
    ) -> None:
        """Bind the button to its session action."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Stay usable while disconnected, so a failed session can be retried."""
        return True

    async def async_press(self) -> None:
        """Open or close the session."""
        if self.entity_description.key == "disconnect":
            await self.coordinator.async_disconnect()
            return
        try:
            await self.coordinator.async_connect()
        except AuthenticationError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="authentication_failed"
            ) from err
        except ProtocolError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="connect_failed",
                translation_placeholders={"error": str(err)},
            ) from err

"""Troubleshooting data without credentials, panel identifiers or raw panel records."""

from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import PyronixConfigEntry


def _timestamp(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PyronixConfigEntry
) -> dict[str, Any]:
    """Return the session state and each permitted area."""
    coordinator = entry.runtime_data
    client = coordinator.client
    data = coordinator.data
    return {
        "connection": {
            "state": client.state,
            "detail": client.detail,
            "idle_expires_at": _timestamp(client.idle_expires_at),
        },
        "app_version": entry.data.get("AppVersion"),
        "firmware": coordinator.catalog["firmware"],
        "observed_at": _timestamp(data["observed_at"]),
        "permissions_received": data["permissions_received"],
        "areas": [
            {
                "index": index,
                "name": row["name"],
                "status": row["status"],
                "value": row["value"],
                "can_arm": index in data["can_arm"],
                "can_disarm": index in data["can_disarm"],
            }
            for index, row in sorted(data["areas"].items())
        ],
    }

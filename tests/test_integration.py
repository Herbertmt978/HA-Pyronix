import copy
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pyronix_homecontrol.client import (
    ProtocolError,
    control_frame,
    ingest,
    validate_auth,
)
from custom_components.pyronix_homecontrol.panel_protocol import PanelSession, crypt_body, unwrap

AUTH = dict(
    AppUser="fixture",
    PushId="fixture",
    DeviceId="fixture",
    PanelId="12345678",
    SystemName="Test Panel",
    PanelPwd="fixture-password",
    UserCode="9876",
)
DATA = {
    "areas": {
        0: {"name": "Day Set", "value": 0, "status": "Unset"},
        1: {"name": "Night Set", "value": 0, "status": "Unset"},
        2: {"name": "Garage", "value": 0, "status": "Unset"},
        3: {"name": "Bike Shed", "value": 0, "status": "Unset"},
    },
    "can_arm": {0, 1, 2, 3},
    "can_disarm": {0, 1, 2, 3},
    "permissions_received": True,
    "firmware": "2.11",
    "observed_at": time.time(),
}


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations):
    yield


async def setup(hass, online=True, saved=None):
    entry = MockConfigEntry(
        domain="pyronix_homecontrol", title="Test Panel", data=AUTH, unique_id="test-panel"
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.pyronix_homecontrol.Store.async_load", new=AsyncMock(return_value=saved)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    if online:
        publish(entry, DATA)
    await hass.async_block_till_done()
    return entry


def publish(entry, data):
    client = entry.runtime_data.client
    client._ws = object()
    client.state = "connected"
    client.data = copy.deepcopy(data)
    entry.runtime_data._receive_snapshot(client.snapshot())


def area(hass, index=0):
    return next(
        s.entity_id
        for s in hass.states.async_all("alarm_control_panel")
        if s.attributes["area_number"] == index
    )


async def test_setup_offline_exposes_connection_controls_without_network(hass):
    with patch(
        "custom_components.pyronix_homecontrol.PanelClient.connect", new=AsyncMock()
    ) as connect:
        entry = await setup(hass, online=False)
        assert len(hass.states.async_all("button")) == 2
        assert all(s.state != "unavailable" for s in hass.states.async_all("button"))
        assert hass.states.async_all("sensor")[0].state == "disconnected"
        assert entry.runtime_data.update_interval is None
        await entry.runtime_data.async_refresh()
        connect.assert_not_awaited()
        assert all(s.state != "unavailable" for s in hass.states.async_all("button"))


async def test_real_client_lifecycle_through_native_buttons_and_unload(hass):
    from tests.test_client import FakeHTTP, FakeSocket

    entry = await setup(hass, online=False)
    wire = FakeSocket()
    entry.runtime_data.client.http = FakeHTTP(wire)
    connect_id = next(
        s.entity_id
        for s in hass.states.async_all("button")
        if s.attributes["friendly_name"].endswith(" Connect")
    )
    await hass.services.async_call("button", "press", {"entity_id": connect_id}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(area(hass)).state == "disarmed"
    assert hass.states.async_all("sensor")[0].state == "connected"
    client = entry.runtime_data.client
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert wire.closed and not client.connected and wire.commands == []


async def test_connect_button_recovers_from_error_and_disconnect_stays_usable(hass):
    entry = await setup(hass, online=False)
    buttons = hass.states.async_all("button")
    connect_id = next(
        s.entity_id for s in buttons if s.attributes["friendly_name"].endswith(" Connect")
    )
    disconnect_id = next(
        s.entity_id for s in buttons if s.attributes["friendly_name"].endswith(" Disconnect")
    )
    with patch.object(
        entry.runtime_data, "async_connect", new=AsyncMock(side_effect=ProtocolError("Panel busy"))
    ):
        with pytest.raises(HomeAssistantError, match="Panel busy"):
            await hass.services.async_call(
                "button", "press", {"entity_id": connect_id}, blocking=True
            )
    assert hass.states.get(connect_id).state != "unavailable"

    async def connected():
        publish(entry, DATA)

    with patch.object(entry.runtime_data, "async_connect", new=AsyncMock(side_effect=connected)):
        await hass.services.async_call("button", "press", {"entity_id": connect_id}, blocking=True)
        await hass.async_block_till_done()
    assert len(hass.states.async_all("alarm_control_panel")) == 4
    assert all(s.state == "disarmed" for s in hass.states.async_all("alarm_control_panel"))
    await hass.services.async_call("button", "press", {"entity_id": disconnect_id}, blocking=True)
    assert all(s.state == "unavailable" for s in hass.states.async_all("alarm_control_panel"))
    assert all(s.state != "unavailable" for s in hass.states.async_all("button"))
    assert hass.states.async_all("sensor")[0].state == "disconnected"


async def test_catalog_restores_names_but_not_old_alarm_state_or_credentials(hass):
    catalog = {
        "areas": [{"index": i, "name": row["name"]} for i, row in DATA["areas"].items()],
        "firmware": "2.11",
    }
    entry = await setup(hass, online=False, saved=catalog)
    assert len(hass.states.async_all("alarm_control_panel")) == 4
    assert all(s.state == "unavailable" for s in hass.states.async_all("alarm_control_panel"))
    assert entry.runtime_data.data["can_disarm"] == set()
    publish(entry, DATA)
    assert entry.runtime_data.catalog == catalog
    assert "UserCode" not in json.dumps(entry.runtime_data.catalog)
    assert "value" not in json.dumps(entry.runtime_data.catalog)


async def test_unpermitted_panel_areas_are_not_discovered_or_cached(hass):
    entry = await setup(hass, online=False)
    data = copy.deepcopy(DATA)
    data["areas"][4] = {"name": "Unused", "value": 4, "status": "Cannot Set"}
    publish(entry, data)
    await hass.async_block_till_done()
    assert len(hass.states.async_all("alarm_control_panel")) == 4
    assert {row["index"] for row in entry.runtime_data.catalog["areas"]} == {0, 1, 2, 3}


async def test_native_services_preserve_area_ids_and_confirmed_states(hass):
    entry = await setup(hass)
    night = area(hass, 1)
    assert hass.states.get(night).attributes["supported_features"] == 4

    async def control(operation, index):
        data = copy.deepcopy(DATA)
        data["areas"][index].update(
            value=1 if operation == "arm" else 0, status="Set" if operation == "arm" else "Unset"
        )
        publish(entry, data)

    with patch.object(
        entry.runtime_data.client, "control", new=AsyncMock(side_effect=control)
    ) as send:
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_arm_night",
            {"entity_id": night, "code": "9876"},
            blocking=True,
        )
        send.assert_awaited_once_with("arm", 1)
        assert hass.states.get(night).state == "armed_night"
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_disarm",
            {"entity_id": night, "code": "9876"},
            blocking=True,
        )
        assert hass.states.get(night).state == "disarmed"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("code", [None, "wrong", "１２３４"])
async def test_wrong_code_never_contacts_panel(hass, code):
    entry = await setup(hass)
    with patch.object(entry.runtime_data.client, "control", new=AsyncMock()) as send:
        with pytest.raises(ServiceValidationError):
            data = {"entity_id": area(hass)}
            if code is not None:
                data["code"] = code
            await hass.services.async_call(
                "alarm_control_panel", "alarm_disarm", data, blocking=True
            )
        send.assert_not_awaited()


async def test_uncertain_command_does_not_invent_disarmed_state_or_disable_live_controls(hass):
    entry = await setup(hass)
    data = copy.deepcopy(DATA)
    data["areas"][0].update(value=1, status="Set")
    publish(entry, data)
    entity = area(hass)
    with patch.object(
        entry.runtime_data.client,
        "control",
        new=AsyncMock(side_effect=ProtocolError("Unconfirmed")),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "alarm_control_panel",
                "alarm_disarm",
                {"entity_id": entity, "code": "9876"},
                blocking=True,
            )
    assert hass.states.get(entity).state == "armed_away"
    assert entry.runtime_data.last_update_success


@pytest.mark.parametrize(
    "value,status,expected",
    [
        (3, "Setting", "arming"),
        (999, "Unknown", "unknown"),
        (4, "Cannot Set", "disarmed"),
        (5, "Can Override", "disarmed"),
    ],
)
async def test_live_status_mapping_without_polling(hass, value, status, expected):
    entry = await setup(hass)
    data = copy.deepcopy(DATA)
    data["areas"][0].update(value=value, status=status)
    publish(entry, data)
    assert hass.states.get(area(hass)).state == expected
    assert hass.states.get(area(hass)).attributes["panel_status"] == status
    assert entry.runtime_data.update_interval is None


async def test_ui_config_flow_and_duplicate(hass):
    first = await hass.config_entries.flow.async_init(
        "pyronix_homecontrol", context={"source": "user"}
    )
    assert first["type"] == "form"
    bad = await hass.config_entries.flow.async_configure(
        first["flow_id"], {"connection_details": "not-json"}
    )
    assert bad["errors"] == {"base": "invalid_connection"}
    with patch("custom_components.pyronix_homecontrol.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            first["flow_id"], {"connection_details": json.dumps(AUTH)}
        )
    assert result["type"] == "create_entry"
    assert result["data"]["UserCode"] == "9876"
    await hass.async_block_till_done()
    second = await hass.config_entries.flow.async_init(
        "pyronix_homecontrol", context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        second["flow_id"], {"connection_details": json.dumps(AUTH)}
    )
    assert result["type"] == "abort" and result["reason"] == "already_configured"


def test_command_wire_ids_and_permission_bounds():
    session = PanelSession("12345678", "fixture")
    session.phase = "data"
    session.key = bytes(16)
    for operation, expected in [("arm", b"-\x01\x02"), ("disarm", b"/\x01\x02")]:
        raw = unwrap(control_frame(session, operation, 2, DATA))
        assert (
            crypt_body(session.key, ord("D"), 3, session.tx_sequence, raw[5:], decrypt=True)[:-16]
            == expected
        )
    for operation, area in [("arm", 4), ("disarm", 255), ("omit", 0), ("arm", -1), ("arm", True)]:
        with pytest.raises(ProtocolError):
            control_frame(session, operation, area, DATA)
    snapshot = copy.deepcopy(DATA)
    snapshot["areas"][2]["value"] = 5
    with pytest.raises(ProtocolError):
        control_frame(session, "arm", 2, snapshot)


def test_status_retains_only_needed_fields_and_credentials_are_narrow():
    snapshot = {"areas": {}, "can_arm": set(), "can_disarm": set(), "permissions_received": False}
    ingest(
        snapshot,
        {
            "type": "area",
            "Detail": [
                {
                    "R": 0,
                    "N": "Day Set",
                    "V": 0,
                    "LastSet": {"U": "private-person"},
                    "S": "arbitrary",
                }
            ],
        },
    )
    assert snapshot["areas"][0] == {"name": "Day Set", "value": 0, "status": "Unset"}
    ingest(
        snapshot,
        {"type": "user", "U": "private-person", "SetAreas": {"R": [0]}, "UnsetAreas": {"R": [0]}},
    )
    assert "private-person" not in repr(snapshot)
    assert "unrelated_secret" not in validate_auth({**AUTH, "unrelated_secret": "never-keep"})

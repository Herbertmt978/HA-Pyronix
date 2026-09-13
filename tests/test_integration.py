import copy
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pyronix_homecontrol.client import (
    CommandUnconfirmedError,
    PanelClient,
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


async def setup(hass):
    entry = MockConfigEntry(
        domain="pyronix_homecontrol", title="Test Panel", data=AUTH, unique_id="test-panel"
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.pyronix_homecontrol.PanelClient.query",
        new=AsyncMock(return_value=copy.deepcopy(DATA)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_four_native_entities_and_manual_arm_disarm(hass):
    entry = await setup(hass)
    states = hass.states.async_all("alarm_control_panel")
    assert len(states) == 4 and all(s.state == "disarmed" for s in states)
    night = next(s.entity_id for s in states if s.attributes["friendly_name"].endswith("Night Set"))
    assert hass.states.get(night).attributes["supported_features"] == 4
    changed = copy.deepcopy(DATA)
    changed["areas"][1].update(value=1, status="Set")
    with patch.object(
        entry.runtime_data.client, "query", new=AsyncMock(return_value=changed)
    ) as query:
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_arm_night",
            {"entity_id": night, "code": "9876"},
            blocking=True,
        )
        query.assert_awaited_once_with("arm", 1)
        assert hass.states.get(night).state == "armed_night"
    with patch.object(
        entry.runtime_data.client, "query", new=AsyncMock(return_value=DATA)
    ) as query:
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_disarm",
            {"entity_id": night, "code": "9876"},
            blocking=True,
        )
        query.assert_awaited_once_with("disarm", 1)
        assert hass.states.get(night).state == "disarmed"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("code", [None, "wrong", "１２３４"])
async def test_wrong_code_never_contacts_panel(hass, code):
    entry = await setup(hass)
    entity = hass.states.async_all("alarm_control_panel")[0].entity_id
    with patch.object(entry.runtime_data.client, "query", new=AsyncMock()) as query:
        with pytest.raises(ServiceValidationError):
            data = {"entity_id": entity}
            if code is not None:
                data["code"] = code
            await hass.services.async_call(
                "alarm_control_panel", "alarm_disarm", data, blocking=True
            )
        query.assert_not_awaited()


async def test_ambiguous_command_failure_does_not_report_disarmed(hass):
    entry = await setup(hass)
    entry.runtime_data.data["areas"][0].update(value=1, status="Set")
    entry.runtime_data.async_set_updated_data(entry.runtime_data.data)
    entity = next(
        s.entity_id
        for s in hass.states.async_all("alarm_control_panel")
        if s.attributes["friendly_name"].endswith("Day Set")
    )
    with patch.object(
        entry.runtime_data.client, "query", new=AsyncMock(side_effect=ProtocolError("Unconfirmed"))
    ) as query:
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "alarm_control_panel",
                "alarm_disarm",
                {"entity_id": entity, "code": "9876"},
                blocking=True,
            )
        assert hass.states.get(entity).state == "unavailable"
        query.assert_awaited_once()


async def test_background_poll_during_command_preserves_last_observation(hass):
    entry = await setup(hass)
    coordinator = entry.runtime_data
    before = copy.deepcopy(coordinator.data)
    async with coordinator.client.lock:
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data == before
    assert all(s.state == "disarmed" for s in hass.states.async_all("alarm_control_panel"))
    assert coordinator.update_interval.total_seconds() == 10


async def test_second_manual_request_does_not_mark_other_controls_unavailable(hass):
    entry = await setup(hass)
    entity = hass.states.async_all("alarm_control_panel")[0].entity_id
    async with entry.runtime_data.client.lock:
        with pytest.raises(HomeAssistantError, match="in progress"):
            await hass.services.async_call(
                "alarm_control_panel",
                "alarm_arm_away",
                {"entity_id": entity, "code": "9876"},
                blocking=True,
            )
    assert entry.runtime_data.last_update_success
    assert all(s.state == "disarmed" for s in hass.states.async_all("alarm_control_panel"))


async def test_setting_confirmation_is_arming_with_prompt_read_only_followup(hass):
    entry = await setup(hass)
    coordinator = entry.runtime_data
    entity = next(
        s.entity_id
        for s in hass.states.async_all("alarm_control_panel")
        if s.attributes["area_number"] == 3
    )
    setting = copy.deepcopy(DATA)
    setting["areas"][3].update(value=3, status="Setting")
    with patch.object(coordinator.client, "query", new=AsyncMock(return_value=setting)):
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_arm_away",
            {"entity_id": entity, "code": "9876"},
            blocking=True,
        )
    state = hass.states.get(entity)
    assert state.state == "arming" and state.attributes["poll_interval_seconds"] == 10
    assert coordinator._unsub_refresh is not None
    armed = copy.deepcopy(setting)
    armed["areas"][3].update(value=1, status="Set")
    with patch.object(coordinator.client, "query", new=AsyncMock(return_value=armed)) as query:
        await coordinator.async_refresh()
    query.assert_awaited_once_with()
    state = hass.states.get(entity)
    assert state.state == "armed_away" and state.attributes["poll_interval_seconds"] == 120
    assert all(
        s.state == "disarmed"
        for s in hass.states.async_all("alarm_control_panel")
        if s.attributes["area_number"] != 3
    )


async def test_unconfirmed_command_preserves_fresh_readback_without_claiming_success(hass):
    entry = await setup(hass)
    entity = hass.states.async_all("alarm_control_panel")[0].entity_id
    with patch.object(
        entry.runtime_data.client,
        "query",
        new=AsyncMock(side_effect=CommandUnconfirmedError(copy.deepcopy(DATA))),
    ):
        with pytest.raises(HomeAssistantError, match="not confirmed"):
            await hass.services.async_call(
                "alarm_control_panel",
                "alarm_arm_away",
                {"entity_id": entity, "code": "9876"},
                blocking=True,
            )
    assert hass.states.get(entity).state == "disarmed"
    assert entry.runtime_data.last_update_success


async def test_failed_fast_poll_returns_to_normal_interval(hass):
    entry = await setup(hass)
    data = copy.deepcopy(DATA)
    data["areas"][3].update(value=3, status="Setting")
    entry.runtime_data.async_set_updated_data(data)
    assert entry.runtime_data.update_interval.total_seconds() == 10
    with patch.object(
        entry.runtime_data.client,
        "query",
        new=AsyncMock(side_effect=ProtocolError("Connection failed")),
    ):
        await entry.runtime_data.async_refresh()
    assert entry.runtime_data.update_interval.total_seconds() == 120
    assert not entry.runtime_data.last_update_success


async def test_unknown_state_is_not_disarmed(hass):
    entry = await setup(hass)
    entry.runtime_data.data["areas"][0].update(value=999, status="Unknown")
    entry.runtime_data.async_set_updated_data(entry.runtime_data.data)
    states = hass.states.async_all("alarm_control_panel")
    assert (
        next(s.state for s in states if s.attributes["friendly_name"].endswith("Day Set"))
        == "unknown"
    )


@pytest.mark.parametrize("value,status", [(4, "Cannot Set"), (5, "Can Override")])
async def test_known_blocked_area_stays_disarmed_and_cannot_be_forced(hass, value, status):
    entry = await setup(hass)
    data = copy.deepcopy(DATA)
    data["areas"][0].update(value=value, status=status)
    entry.runtime_data.async_set_updated_data(data)
    state = next(
        s for s in hass.states.async_all("alarm_control_panel") if s.attributes["area_number"] == 0
    )
    assert state.state == "disarmed" and state.attributes["panel_status"] == status
    session = PanelSession("12345678", "fixture")
    session.phase = "data"
    session.key = bytes(16)
    with pytest.raises(ProtocolError, match="cannot be set"):
        control_frame(session, "arm", 0, data)
    data["areas"][0].update(value=0, status="Unset")
    entry.runtime_data.async_set_updated_data(data)
    assert hass.states.get(state.entity_id).attributes["panel_status"] == "Unset"


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


async def test_busy_request_is_not_queued_or_retried():
    client = PanelClient(None, AUTH)
    async with client.lock:
        with patch.object(client, "_transaction", new=AsyncMock()) as send:
            with pytest.raises(ProtocolError):
                await client.query("arm", 0)
            send.assert_not_awaited()

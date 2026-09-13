import ast
import base64
import getpass
import io
import json
import urllib.error
import warnings
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tools.setup_from_adb import (
    NoRedirect,
    SetupError,
    configure_ha,
    extract_connection,
    private_input,
    validate_url,
)


def connection(name, password="fixture-password"):
    return f"<p><n>fixture-user</n><u>fixture-push</u><i>fixture-device</i><a>12345678</a><b>{name}</b><z>{password}</z></p>"


def logged(xml, encoded=True):
    value = base64.encodebytes(xml.encode()).decode() if encoded else xml
    return "webSocketwriteString message sent: (" + value + ")\n"


def test_only_requested_system_and_latest_connection_are_selected():
    log = (
        logged(connection("Other house"))
        + logged(connection("My alarm", "old-fixture"))
        + logged(connection("My alarm"))
    )
    result = extract_connection(log, "My alarm")
    assert result["SystemName"] == "My alarm" and result["PanelPwd"] == "fixture-password"
    assert "UserCode" not in result


def test_literal_xml_and_malformed_frames():
    log = "webSocketwriteString message sent: (not-base64!)\n" + logged(
        connection("My alarm"), False
    )
    assert extract_connection(log, "My alarm")["PanelId"] == "12345678"
    with pytest.raises(SetupError, match="No matching connection"):
        extract_connection(log, "Another alarm")


@pytest.mark.parametrize("url", ["http://homeassistant.local:8123/", "https://ha.example.com"])
def test_valid_ha_urls(url):
    assert validate_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://ha.example.com",
        "https://token@ha.example.com",
        "https://ha.example.com/dashboard",
        "https://ha.example.com?token=fixture",
    ],
)
def test_unsafe_or_incorrect_ha_urls(url):
    with pytest.raises(SetupError):
        validate_url(url)


def test_config_flow_receives_fields_without_printing(capsys):
    opener = Mock()
    opener.open.side_effect = [
        io.BytesIO(
            json.dumps({"type": "form", "step_id": "user", "flow_id": "test-flow"}).encode()
        ),
        io.BytesIO(b'{"type":"create_entry"}'),
    ]
    with patch("tools.setup_from_adb.urllib.request.build_opener", return_value=opener):
        configure_ha("https://ha.example.com", "fixture-token", {"PanelPwd": "fixture-password"})
    requests = [call.args[0] for call in opener.open.call_args_list]
    assert json.loads(json.loads(requests[1].data)["connection_details"]) == {
        "PanelPwd": "fixture-password"
    }
    assert "fixture" not in capsys.readouterr().out


def test_http_errors_never_echo_request_or_response_data():
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError(
        "https://ha.example.com", 400, "secret-fixture", {}, io.BytesIO(b"secret-fixture")
    )
    with patch("tools.setup_from_adb.urllib.request.build_opener", return_value=opener):
        with pytest.raises(SetupError) as result:
            configure_ha("https://ha.example.com", "fixture-token", {})
    assert "secret-fixture" not in str(result.value)


def test_redirect_cannot_forward_authorization():
    with pytest.raises(SetupError):
        NoRedirect().redirect_request(None, None, 302, None, None, "https://another.example")


def test_hidden_prompt_stops_if_echo_cannot_be_disabled():
    def warn(_):
        warnings.warn("Cannot hide input", getpass.GetPassWarning)

    with patch("tools.setup_from_adb.getpass.getpass", side_effect=warn):
        with pytest.raises(SetupError, match="hidden input"):
            private_input("Private prompt")


def test_helper_remains_compatible_with_python_311():
    source = Path(__file__).parents[1] / "tools/setup_from_adb.py"
    ast.parse(source.read_text(), feature_version=(3, 11))

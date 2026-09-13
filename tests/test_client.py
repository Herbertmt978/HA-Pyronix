import asyncio
import hashlib
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.pyronix_homecontrol.client import (
    AuthenticationError,
    CommandUnconfirmedError,
    PanelClient,
    ProtocolError,
)
from custom_components.pyronix_homecontrol.panel_protocol import crypt_body, fixed_key, unwrap
from tests.test_integration import AUTH
from tests.test_protocol import panel_frame


class FakeSocket:
    def __init__(self, mode="normal", initial_value=0):
        self.mode = mode
        self.replies = deque()
        self.commands = []
        self.sent = []
        self.seq = 0
        self.value = initial_value
        self.key = None
        self.private = ec.derive_private_key(987654321, ec.SECP256R1())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def packet(self, header, body):
        self.seq += 1
        return panel_frame(header, self.seq, body, self.key if header in "deg" else None)

    def area(self):
        return self.packet(
            "d",
            json.dumps(
                {"type": "area", "Detail": [{"R": 0, "N": "Day Set", "V": self.value}]}
            ).encode(),
        )

    async def send_str(self, text):
        self.sent.append(text)
        if text.startswith("(<p>"):
            return
        raw = unwrap(text)
        if raw.startswith(b"<p>"):
            if b"<w>" in raw:
                self.replies.append("<e>j never-log-this</e>")
            elif b"<a>" in raw:
                self.replies.append("<e>b </e>" if self.mode == "busy" else "<e>u </e>")
        elif raw[:1] == b"A":
            if self.mode == "handshake_timeout":
                return
            self.replies.append(self.packet("a", b"\x01\x20" + b"12345678"))
        elif raw[:1] == b"B":
            public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw[6:-16])
            shared = self.private.exchange(ec.ECDH(), public)
            panel_public = self.private.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
            )
            self.replies.append(self.packet("b", b"\x01" + panel_public))
            self.key = hashlib.sha256(
                fixed_key(AUTH["PanelPwd"], AUTH["PanelId"]) + shared
            ).digest()[:16]
        elif raw[:1] == b"D":
            body = crypt_body(
                self.key,
                ord("D"),
                len(raw) - 21,
                int.from_bytes(raw[1:3], "little"),
                raw[5:],
                decrypt=True,
            )[:-16]
            if body.startswith(b","):
                assert body == b",9876\n"
                if self.mode == "bad_login":
                    self.replies.append(self.packet("e", b""))
                    return
                first = self.area()
                user = self.packet(
                    "d", b'{"type":"user","SetAreas":{"R":[0]},"UnsetAreas":{"R":[0]}}'
                )
                # Fragmentation and coalescing occur across real WebSocket messages.
                self.replies.extend(["\n" + first[:9], first[9:] + user])
            elif body[:1] in (b"-", b"/"):
                self.commands.append(body)
                self.value = 1 if body[:1] == b"-" else 0
                if self.mode == "send_failure":
                    raise ConnectionError()
                if self.mode == "setting_ack":
                    self.value = 3
                if self.mode not in ("lost_ack", "closed_ack"):
                    self.replies.append(self.area())

    async def receive(self):
        if not self.replies:
            if self.mode == "closed_ack":
                return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)
            raise TimeoutError()
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=self.replies.popleft())


class FakeHTTP:
    def __init__(self, socket):
        self.socket = socket
        self.connections = 0

    def ws_connect(self, *args, **kwargs):
        self.connections += 1
        return self.socket


class SequencedHTTP:
    def __init__(self, *sockets):
        self.sockets = deque(sockets)
        self.connections = 0

    def ws_connect(self, *args, **kwargs):
        self.connections += 1
        return self.sockets.popleft()


async def test_status_reconnects_once_after_stalled_handshake_without_actuation():
    stalled, working = FakeSocket("handshake_timeout"), FakeSocket()
    http = SequencedHTTP(stalled, working)
    result = await PanelClient(http, AUTH).query()
    assert result["areas"][0]["value"] == 0 and http.connections == 2
    assert stalled.commands == working.commands == []


async def test_persistent_status_failure_stops_after_two_connections():
    http = SequencedHTTP(FakeSocket("handshake_timeout"), FakeSocket("handshake_timeout"))
    with pytest.raises(ProtocolError, match="unconfirmed"):
        await PanelClient(http, AUTH).query()
    assert http.connections == 2


@pytest.mark.parametrize("operation", ["arm", "disarm"])
async def test_controls_do_not_retry_even_a_stalled_preflight(operation):
    stalled = FakeSocket("handshake_timeout")
    http = SequencedHTTP(stalled)
    with pytest.raises(ProtocolError, match="unconfirmed"):
        await PanelClient(http, AUTH).query(operation, 0)
    assert http.connections == 1 and stalled.commands == []


async def test_rejected_status_credentials_are_not_retried():
    http = SequencedHTTP(FakeSocket("bad_login"))
    with pytest.raises(AuthenticationError):
        await PanelClient(http, AUTH).query()
    assert http.connections == 1


async def test_status_reconnect_shares_one_overall_deadline():
    class SlowHandshake(FakeSocket):
        def __init__(self):
            super().__init__("handshake_timeout")
            self.cancelled = False

        async def receive(self):
            if not self.replies:
                try:
                    await asyncio.sleep(0.03)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return await super().receive()

    first, second = SlowHandshake(), SlowHandshake()
    http = SequencedHTTP(first, second)
    original_timeout = asyncio.timeout
    with patch(
        "custom_components.pyronix_homecontrol.client.asyncio.timeout",
        side_effect=lambda limit: original_timeout(limit / 1000),
    ):
        with pytest.raises(ProtocolError, match="unconfirmed"):
            await PanelClient(http, AUTH).query()
    assert http.connections == 2 and second.cancelled


async def test_exact_client_full_handshake_status_and_single_arm():
    wire = FakeSocket()
    client = PanelClient(FakeHTTP(wire), AUTH)
    result = await client.query()
    assert result["areas"][0]["value"] == 0 and wire.commands == []
    wire = FakeSocket()
    client = PanelClient(FakeHTTP(wire), AUTH)
    result = await client.query("arm", 0)
    assert result["areas"][0]["value"] == 1 and wire.commands == [b"-\x01\x00"]
    assert unwrap(wire.sent[-1])[:1] == b"E"


async def test_lost_ack_with_unchanged_readback_is_unconfirmed_and_never_retried():
    wire = FakeSocket("lost_ack")
    readback = FakeSocket()
    http = SequencedHTTP(wire, readback)
    client = PanelClient(http, AUTH)
    with pytest.raises(CommandUnconfirmedError) as error:
        await client.query("arm", 0)
    assert wire.commands == [b"-\x01\x00"] and http.connections == 2
    assert readback.commands == [] and error.value.snapshot["areas"][0]["value"] == 0


@pytest.mark.parametrize(
    "operation,initial,wanted", [("arm", 0, 1), ("disarm", 1, 0), ("arm", 0, 3)]
)
async def test_lost_ack_is_confirmed_by_a_read_only_connection(operation, initial, wanted):
    wire = FakeSocket("lost_ack", initial)
    readback = FakeSocket(initial_value=wanted)
    http = SequencedHTTP(wire, readback)
    result = await PanelClient(http, AUTH).query(operation, 0)
    assert result["areas"][0]["value"] == wanted
    assert len(wire.commands) == 1 and readback.commands == [] and http.connections == 2


async def test_setting_ack_returns_arming_without_waiting_for_exit_delay():
    wire = FakeSocket("setting_ack")
    result = await PanelClient(FakeHTTP(wire), AUTH).query("arm", 0)
    assert result["areas"][0]["value"] == 3
    assert wire.commands == [b"-\x01\x00"]


async def test_already_setting_is_not_armed_again():
    wire = FakeSocket(initial_value=3)
    result = await PanelClient(FakeHTTP(wire), AUTH).query("arm", 0)
    assert result["areas"][0]["value"] == 3 and wire.commands == []


async def test_failure_during_write_is_read_back_and_never_repeated():
    wire = FakeSocket("send_failure")
    readback = FakeSocket(initial_value=1)
    result = await PanelClient(SequencedHTTP(wire, readback), AUTH).query("arm", 0)
    assert result["areas"][0]["value"] == 1
    assert len(wire.commands) == 1 and readback.commands == []


async def test_closed_command_socket_is_confirmed_without_sending_again():
    wire, readback = FakeSocket("closed_ack"), FakeSocket(initial_value=1)
    http = SequencedHTTP(wire, readback)
    result = await PanelClient(http, AUTH).query("arm", 0)
    assert result["areas"][0]["value"] == 1
    assert http.connections == 2 and len(wire.commands) == 1 and readback.commands == []


async def test_failed_confirmation_read_stops_without_a_third_connection():
    wire, readback = FakeSocket("lost_ack"), FakeSocket("handshake_timeout")
    http = SequencedHTTP(wire, readback)
    with pytest.raises(CommandUnconfirmedError) as error:
        await PanelClient(http, AUTH).query("arm", 0)
    assert error.value.snapshot is None
    assert http.connections == 2 and len(wire.commands) == 1 and readback.commands == []


async def test_rejected_confirmation_credentials_are_not_retried():
    wire, readback = FakeSocket("lost_ack"), FakeSocket("bad_login")
    http = SequencedHTTP(wire, readback)
    with pytest.raises(AuthenticationError):
        await PanelClient(http, AUTH).query("arm", 0)
    assert http.connections == 2 and len(wire.commands) == 1 and readback.commands == []


async def test_command_deadline_reserves_time_for_read_only_confirmation():
    class SlowAck(FakeSocket):
        async def receive(self):
            if not self.replies:
                await asyncio.Event().wait()
            return await super().receive()

    wire, readback = SlowAck("lost_ack"), FakeSocket(initial_value=1)
    http = SequencedHTTP(wire, readback)
    original_timeout = asyncio.timeout
    with patch(
        "custom_components.pyronix_homecontrol.client.asyncio.timeout",
        side_effect=lambda limit: original_timeout(limit / 1000),
    ):
        result = await PanelClient(http, AUTH).query("arm", 0)
    assert result["areas"][0]["value"] == 1
    assert http.connections == 2 and len(wire.commands) == 1 and readback.commands == []


async def test_control_and_confirmation_share_overall_deadline():
    class NeverReplies(FakeSocket):
        async def receive(self):
            if not self.replies:
                await asyncio.Event().wait()
            return await super().receive()

    wire, readback = NeverReplies("lost_ack"), NeverReplies("handshake_timeout")
    http = SequencedHTTP(wire, readback)
    original_timeout = asyncio.timeout
    with patch(
        "custom_components.pyronix_homecontrol.client.asyncio.timeout",
        side_effect=lambda limit: original_timeout(limit / 1000),
    ):
        with pytest.raises(CommandUnconfirmedError):
            await PanelClient(http, AUTH).query("arm", 0)
    assert http.connections == 2 and len(wire.commands) == 1 and readback.commands == []


async def test_cancelled_control_does_not_open_another_connection():
    class CancelledAck(FakeSocket):
        async def receive(self):
            if not self.replies:
                raise asyncio.CancelledError()
            return await super().receive()

    wire = CancelledAck("lost_ack")
    http = SequencedHTTP(wire)
    with pytest.raises(asyncio.CancelledError):
        await PanelClient(http, AUTH).query("arm", 0)
    assert http.connections == 1 and len(wire.commands) == 1


async def test_already_disarmed_does_not_send_actuation():
    wire = FakeSocket()
    client = PanelClient(FakeHTTP(wire), AUTH)
    result = await client.query("disarm", 0)
    assert result["areas"][0]["value"] == 0 and wire.commands == []


@pytest.mark.parametrize(
    "mode,error", [("busy", ProtocolError), ("bad_login", AuthenticationError)]
)
async def test_cloud_busy_and_login_failure_are_bounded(mode, error):
    wire = FakeSocket(mode)
    http = FakeHTTP(wire)
    client = PanelClient(http, AUTH)
    with pytest.raises(error):
        await client.query()
    assert wire.commands == [] and http.connections == 1

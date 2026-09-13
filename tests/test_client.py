import asyncio
import hashlib
import json
from collections import deque
from types import SimpleNamespace

import aiohttp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.pyronix_homecontrol.client import (
    AuthenticationError,
    CommandUnconfirmedError,
    PanelBusyError,
    PanelClient,
    ProtocolError,
)
from custom_components.pyronix_homecontrol.panel_protocol import crypt_body, fixed_key, unwrap
from tests.test_integration import AUTH
from tests.test_protocol import panel_frame


class FakeSocket:
    def __init__(self, mode="normal", initial_value=0):
        self.mode = mode
        self.replies = asyncio.Queue()
        self.closed = False
        self.command_sent = asyncio.Event()
        self.heartbeat_seen = asyncio.Event()
        self.commands = []
        self.sent = []
        self.seq = 0
        self.value = initial_value
        self.key = None
        self.private = ec.derive_private_key(987654321, ec.SECP256R1())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

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
                self.replies.put_nowait("<e>j never-log-this</e>")
            elif b"<a>" in raw:
                self.replies.put_nowait("<e>b </e>" if self.mode == "busy" else "<e>u </e>")
        elif raw[:1] == b"A":
            if self.mode == "handshake_timeout":
                return
            self.replies.put_nowait(self.packet("a", b"\x01\x20" + b"12345678"))
        elif raw[:1] == b"B":
            public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw[6:-16])
            shared = self.private.exchange(ec.ECDH(), public)
            panel_public = self.private.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
            )
            self.replies.put_nowait(self.packet("b", b"\x01" + panel_public))
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
                    self.replies.put_nowait(self.packet("e", b""))
                    return
                first = self.area()
                user = self.packet(
                    "d", b'{"type":"user","SetAreas":{"R":[0]},"UnsetAreas":{"R":[0]}}'
                )
                # Fragmentation and coalescing occur across real WebSocket messages.
                self.replies.put_nowait("\n" + first[:9])
                self.replies.put_nowait(first[9:] + user)
            elif body[:1] in (b"-", b"/"):
                self.commands.append(body)
                self.command_sent.set()
                self.value = 1 if body[:1] == b"-" else 0
                if self.mode == "send_failure":
                    raise ConnectionError()
                if self.mode == "setting_ack":
                    self.value = 3
                if self.mode == "closed_ack":
                    self.replies.put_nowait(None)
                if self.mode not in ("lost_ack", "closed_ack"):
                    self.replies.put_nowait(self.area())

            elif body == b" ":
                self.heartbeat_seen.set()
                if self.mode != "silent":
                    self.replies.put_nowait(self.packet("d", b" "))

    async def receive(self):
        reply = await self.replies.get()
        return SimpleNamespace(
            type=aiohttp.WSMsgType.CLOSED if reply is None else aiohttp.WSMsgType.TEXT,
            data=reply,
        )

    def push_area(self, value):
        self.value = value
        self.replies.put_nowait(self.area())


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


@pytest.fixture
async def make_client():
    clients = []

    def create(mode="normal", initial=0, **kwargs):
        wire = FakeSocket(mode, initial)
        http = FakeHTTP(wire)
        client = PanelClient(http, AUTH, **kwargs)
        clients.append(client)
        return client, wire, http

    yield create
    for client in clients:
        await client.disconnect()


async def test_connect_controls_and_live_push_share_one_socket(make_client):
    updated = asyncio.Event()
    client, wire, http = make_client(on_update=lambda data: updated.set())
    result = await client.connect()
    assert result["areas"][0]["value"] == 0 and wire.commands == []
    assert client.connected and not wire.closed
    assert (await client.control("arm", 0))["areas"][0]["value"] == 1
    assert (await client.control("disarm", 0))["areas"][0]["value"] == 0
    updated.clear()
    wire.push_area(2)
    await asyncio.wait_for(updated.wait(), 1)
    assert client.snapshot()["areas"][0]["value"] == 2
    assert http.connections == 1 and wire.commands == [b"-\x01\x00", b"/\x01\x00"]
    await client.disconnect()
    assert not client.connected and wire.closed
    assert unwrap(wire.sent[-1])[:1] == b"E"


async def test_offline_control_never_opens_a_connection(make_client):
    client, wire, http = make_client()
    with pytest.raises(ProtocolError, match="Press Connect"):
        await client.control("disarm", 0)
    assert http.connections == 0 and wire.commands == []


@pytest.mark.parametrize(
    "mode,error", [("busy", ProtocolError), ("bad_login", AuthenticationError)]
)
async def test_failed_connect_does_not_retry_and_can_be_disconnected(make_client, mode, error):
    client, wire, http = make_client(mode)
    with pytest.raises(error):
        await client.connect()
    assert client.state == "error" and http.connections == 1 and wire.commands == []
    await client.disconnect()
    assert client.state == "disconnected" and wire.closed


async def test_failed_connection_can_be_explicitly_retried():
    first, second = FakeSocket("busy"), FakeSocket()
    http = SequencedHTTP(first, second)
    client = PanelClient(http, AUTH)
    try:
        with pytest.raises(ProtocolError):
            await client.connect()
        await client.connect()
        assert client.connected and http.connections == 2
        assert first.commands == second.commands == []
    finally:
        await client.disconnect()


async def test_connect_timeout_is_bounded_and_releases_socket(make_client):
    client, wire, http = make_client("handshake_timeout")
    client.CONNECT_TIMEOUT = 0.02
    with pytest.raises(ProtocolError, match="timed out"):
        await client.connect()
    assert client.state == "error" and wire.closed and http.connections == 1


async def test_disconnect_cancels_connect_promptly(make_client):
    connecting = asyncio.Event()
    client, wire, http = make_client("handshake_timeout", on_state=lambda: connecting.set())
    request = asyncio.create_task(client.connect())
    await connecting.wait()
    await client.disconnect()
    with pytest.raises(ProtocolError, match="disconnected"):
        await asyncio.wait_for(request, 1)
    assert client.state == "disconnected" and not client.connected


async def test_duplicate_connect_uses_existing_session(make_client):
    client, wire, http = make_client()
    await asyncio.gather(client.connect(), client.connect())
    await client.connect()
    assert http.connections == 1 and client.connected and wire.commands == []


@pytest.mark.parametrize("mode", ["closed_ack", "send_failure"])
async def test_lost_connection_after_write_never_reconnects_or_retries(make_client, mode):
    client, wire, http = make_client(mode)
    await client.connect()
    with pytest.raises(CommandUnconfirmedError):
        await client.control("arm", 0)
    assert len(wire.commands) == 1 and http.connections == 1 and not client.connected


async def test_late_confirmation_updates_live_state_without_resending(make_client):
    updated = asyncio.Event()
    client, wire, http = make_client("lost_ack", on_update=lambda data: updated.set())
    await client.connect()
    client.COMMAND_TIMEOUT = 0.02
    with pytest.raises(CommandUnconfirmedError) as error:
        await client.control("arm", 0)
    assert error.value.snapshot["areas"][0]["value"] == 0 and client.connected
    updated.clear()
    wire.push_area(1)
    await asyncio.wait_for(updated.wait(), 1)
    assert client.snapshot()["areas"][0]["value"] == 1
    assert len(wire.commands) == 1 and http.connections == 1


async def test_stalled_write_is_bounded_and_never_repeated(make_client):
    client, wire, http = make_client()
    await client.connect()
    client.COMMAND_TIMEOUT = 0.02
    original_send = wire.send_str

    async def stalled_send(text):
        if unwrap(text)[:1] == b"D":
            await asyncio.Event().wait()
        await original_send(text)

    wire.send_str = stalled_send
    with pytest.raises(CommandUnconfirmedError):
        await asyncio.wait_for(client.control("arm", 0), 1)
    assert http.connections == 1 and client._pending is None


async def test_setting_is_confirmed_before_exit_delay(make_client):
    client, wire, http = make_client("setting_ack")
    await client.connect()
    assert (await client.control("arm", 0))["areas"][0]["value"] == 3
    sequence = client._session.tx_sequence
    await client.control("arm", 0)
    assert client._session.tx_sequence == sequence and len(wire.commands) == 1


async def test_disconnect_during_pending_command_reports_uncertainty(make_client):
    client, wire, http = make_client("lost_ack")
    await client.connect()
    request = asyncio.create_task(client.control("arm", 0))
    await wire.command_sent.wait()
    with pytest.raises(PanelBusyError):
        await client.control("disarm", 0)
    assert client.connected
    await client.disconnect()
    with pytest.raises(CommandUnconfirmedError):
        await request
    assert len(wire.commands) == 1 and wire.closed and http.connections == 1


async def test_cancelled_command_does_not_cancel_live_reader_or_resend(make_client):
    client, wire, http = make_client("lost_ack")
    await client.connect()
    request = asyncio.create_task(client.control("arm", 0))
    await wire.command_sent.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert client.connected and client._pending is None
    assert len(wire.commands) == 1 and http.connections == 1


async def test_heartbeat_keeps_session_without_refreshing_user_idle_deadline(make_client):
    client, wire, http = make_client()
    client.HEARTBEAT_INTERVAL = 0.01
    await client.connect()
    deadline = client.idle_deadline
    await asyncio.wait_for(wire.heartbeat_seen.wait(), 1)
    assert client.connected and client.idle_deadline == deadline and wire.commands == []


async def test_idle_session_closes_without_changing_alarm(make_client):
    client, wire, http = make_client()
    client.HEARTBEAT_INTERVAL = 0.01
    client.IDLE_TIMEOUT = 0.02
    await client.connect()
    await asyncio.wait_for(client._reader, 1)
    assert client.state == "disconnected" and wire.closed and wire.commands == []
    assert client.idle_expires_at is None


async def test_unresponsive_live_session_becomes_error(make_client):
    client, wire, http = make_client("silent")
    client.HEARTBEAT_INTERVAL = 0.01
    client.SILENCE_TIMEOUT = 0.02
    await client.connect()
    await asyncio.wait_for(client._reader, 1)
    assert client.state == "error" and wire.closed and http.connections == 1


async def test_disconnect_then_connect_opens_a_new_session():
    first, second = FakeSocket(), FakeSocket(initial_value=1)
    http = SequencedHTTP(first, second)
    client = PanelClient(http, AUTH)
    try:
        await client.connect()
        await client.disconnect()
        assert first.closed
        result = await client.connect()
        assert result["areas"][0]["value"] == 1 and http.connections == 2
        assert first.commands == second.commands == []
    finally:
        await client.disconnect()

"""Explicit Pyronix sessions. Never log connection fields or wire payloads."""

import asyncio
import copy
import json
import time
from collections.abc import Mapping

import aiohttp

from .panel_protocol import FrameBuffer, PanelSession, ProtocolError, cloud_command, cloud_message

ENDPOINT = "wss://app.pyronixcloud.com:443"
STATUS = {
    0: "Unset",
    1: "Set",
    2: "Alarmed",
    3: "Setting",
    4: "Cannot Set",
    5: "Can Override",
    6: "Set Cancelled",
}


class AuthenticationError(ProtocolError):
    """A confirmed rejection; caller must not repeatedly retry credentials."""


class PanelBusyError(ProtocolError):
    """A local operation owns the connection; this is not a panel outage."""


class CommandUnconfirmedError(ProtocolError):
    """The command was attempted, but its outcome remains uncertain."""

    def __init__(self, snapshot=None):
        super().__init__(
            "Command attempted, but the requested state is not confirmed. "
            "Check the panel before trying again; the command was not repeated."
        )
        self.snapshot = snapshot


def operation_confirmed(operation, value):
    """Setting confirms an arm request, without claiming the area is fully armed."""
    return value in (1, 3) if operation == "arm" else value == 0


def validate_auth(auth):
    required = ("AppUser", "PushId", "DeviceId", "PanelId", "SystemName", "PanelPwd", "UserCode")
    if not isinstance(auth, Mapping) or not all(
        isinstance(auth.get(k), str) and auth[k] for k in required
    ):
        raise ProtocolError("Missing connection details")
    if (
        len(auth["PanelId"]) != 8
        or not auth["UserCode"].isascii()
        or not auth["UserCode"].isdigit()
        or not 4 <= len(auth["UserCode"]) <= 8
    ):
        raise ProtocolError("Invalid panel connection details")
    allowed = set(required) | {
        "AppVersion",
        "AppType",
        "OSName",
        "OSVersion",
        "NetworkType",
        "AppLang",
    }
    return {k: str(v) for k, v in auth.items() if k in allowed}


def ingest(snapshot, record):
    """Retain only area state, allowed operations and firmware; drop raw user data."""
    kind = record.get("type")
    if kind == "area":
        for row in record.get("Detail", []):
            index, value = row.get("R"), row.get("V")
            if type(index) is int and 0 <= index <= 255 and type(value) is int:
                snapshot["areas"][index] = {
                    "name": str(row.get("N", "Area"))[:80],
                    "value": value,
                    "status": STATUS.get(value, "Unknown"),
                }
    elif kind == "user":
        for source, target in (("SetAreas", "can_arm"), ("UnsetAreas", "can_disarm")):
            values = record.get(source, {}).get("R", [])
            if not isinstance(values, list) or any(
                type(v) is not int or not 0 <= v <= 255 for v in values
            ):
                raise ProtocolError("Invalid panel permissions")
            snapshot[target] = set(values)
        snapshot["permissions_received"] = True
    elif kind == "panel":
        hi, lo = record.get("VersionHi"), record.get("VersionLo")
        if type(hi) is int and type(lo) is int:
            snapshot["firmware"] = f"{hi}.{lo}"


def complete(snapshot):
    permitted = snapshot["can_arm"] | snapshot["can_disarm"]
    return (
        snapshot["permissions_received"]
        and bool(permitted)
        and permitted <= snapshot["areas"].keys()
    )


def validate_control(operation, area, snapshot):
    """Exactly one area, no forced omissions or output/installer operations."""
    if operation not in ("arm", "disarm") or type(area) is not int or not 0 <= area <= 255:
        raise ProtocolError("Unsupported alarm operation")
    if not complete(snapshot) or area not in snapshot["can_" + operation]:
        raise ProtocolError("Panel user cannot control this area")
    if operation == "arm" and snapshot["areas"][area]["value"] in (4, 5):
        raise ProtocolError("Panel reports the area cannot be set without intervention")


def control_frame(session, operation, area, snapshot):
    validate_control(operation, area, snapshot)
    if session.phase != "data":
        raise ProtocolError("Panel session is not ready")
    # Installed HomeControl SDK sendArmAreas / sendUnArmAreas: 45 / 47.
    return session._frame("D", bytes([45 if operation == "arm" else 47, 1, area]))


class PanelClient:
    """One explicitly opened session; commands and incoming status share its socket."""

    CONNECT_TIMEOUT = 50
    COMMAND_TIMEOUT = 25
    HEARTBEAT_INTERVAL = 10
    SILENCE_TIMEOUT = 40
    IDLE_TIMEOUT = 300

    def __init__(self, http, auth, on_update=None, on_state=None, create_task=None):
        self.http = http
        self.auth = validate_auth(auth)
        self.on_update = on_update
        self.on_state = on_state
        self.create_task = create_task or asyncio.create_task
        self.state = "disconnected"
        self.detail = None
        self.idle_deadline = None
        self.idle_expires_at = None
        self.last_received = None
        self.data = self._empty_snapshot()
        self._reader = self._ws = self._session = self._ready = self._pending = None
        self._write_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._stop_requested = False

    @staticmethod
    def _empty_snapshot():
        return {
            "areas": {},
            "can_arm": set(),
            "can_disarm": set(),
            "permissions_received": False,
            "observed_at": None,
        }

    @property
    def connected(self):
        return self.state == "connected" and self._ws is not None

    def snapshot(self):
        return copy.deepcopy(self.data)

    def _set_state(self, state, detail=None):
        self.state, self.detail = state, detail
        if self.on_state:
            self.on_state()

    def _touch(self):
        self.idle_deadline = time.monotonic() + self.IDLE_TIMEOUT
        self.idle_expires_at = time.time() + self.IDLE_TIMEOUT
        if self.on_state:
            self.on_state()

    @staticmethod
    def _future():
        future = asyncio.get_running_loop().create_future()
        # Disconnect can settle a request whose caller has already been cancelled.
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        return future

    async def connect(self):
        if self.connected:
            self._touch()
            return self.snapshot()
        if self._reader is not None and not self._reader.done() and self.state != "connecting":
            await self.disconnect()
        if self._reader is None or self._reader.done():
            self.data = self._empty_snapshot()
            self._stop_requested = False
            self._ready = self._future()
            self._set_state("connecting")
            self._reader = self.create_task(self._run())
        try:
            return await asyncio.wait_for(asyncio.shield(self._ready), self.CONNECT_TIMEOUT)
        except TimeoutError:
            await self.disconnect()
            error = ProtocolError("Connection timed out. Press Connect to try again.")
            self._set_state("error", str(error))
            raise error from None
        except asyncio.CancelledError:
            await self.disconnect()
            raise

    async def disconnect(self):
        self._stop_requested = True
        self._set_state("disconnected")
        reader = self._reader
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        self._reader = None
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(ProtocolError("Connection was disconnected."))
        self.idle_deadline = None
        self.idle_expires_at = None
        self._set_state("disconnected")

    async def control(self, operation, area):
        if self._command_lock.locked():
            raise PanelBusyError("Another panel command is in progress; wait for its result")
        async with self._command_lock:
            if not self.connected:
                raise ProtocolError("Disconnected. Press Connect before choosing an alarm action.")
            self._touch()
            future = None
            try:
                async with asyncio.timeout(self.COMMAND_TIMEOUT):
                    async with self._write_lock:
                        if not self.connected:
                            raise ProtocolError(
                                "Disconnected. Press Connect before choosing an alarm action."
                            )
                        validate_control(operation, area, self.data)
                        if operation_confirmed(operation, self.data["areas"][area]["value"]):
                            return self.snapshot()
                        frame = control_frame(self._session, operation, area, self.data)
                        future = self._future()
                        self._pending = (operation, area, future)
                        # Set pending before writing: a failed write may still reach the panel.
                        await self._ws.send_str(frame)
                    return await asyncio.shield(future)
            except TimeoutError:
                raise CommandUnconfirmedError(self.snapshot() if self.connected else None) from None
            except aiohttp.ClientError, OSError, ConnectionError:
                await self.disconnect()
                raise CommandUnconfirmedError() from None
            finally:
                if future is not None and self._pending and self._pending[2] is future:
                    self._pending = None
                if future is not None and not future.done():
                    future.cancel()

    async def _send(self, build_frame):
        async with self._write_lock:
            if self._ws is None:
                raise ConnectionError("Panel connection closed")
            await self._ws.send_str(build_frame())

    async def _run(self):
        error = None
        self._session = PanelSession(self.auth["PanelId"], self.auth["PanelPwd"])
        self._phase = "cloud"
        self._password_requested = self._connect_requested = False
        framing = FrameBuffer()
        try:
            async with self.http.ws_connect(
                ENDPOINT,
                timeout=aiohttp.ClientWSTimeout(ws_close=3),
                heartbeat=None,
                max_msg_size=1000000,
            ) as ws:
                self._ws = ws
                await self._send(lambda: cloud_message(self.auth, "subscription"))
                try:
                    while True:
                        if (
                            self.connected
                            and self._pending is None
                            and time.monotonic() >= self.idle_deadline
                        ):
                            break
                        try:
                            message = await asyncio.wait_for(
                                ws.receive(), self.HEARTBEAT_INTERVAL if self.connected else 20
                            )
                        except TimeoutError:
                            if (
                                not self.connected
                                or time.monotonic() - self.last_received >= self.SILENCE_TIMEOUT
                            ):
                                raise ConnectionError("Panel stopped responding") from None
                            await self._send(self._session.heartbeat)
                            continue
                        if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                            raise ConnectionError("Panel connection closed")
                        self.last_received = time.monotonic()
                        if not message.data.strip():
                            if self._phase == "data":
                                await self._send(self._session.heartbeat)
                            continue
                        for raw in framing.feed(message.data):
                            await self._handle_frame(raw)
                finally:
                    if self._phase in ("handshake", "data"):
                        try:
                            await asyncio.wait_for(self._send(self._session.close), 2)
                        except TimeoutError, aiohttp.ClientError, OSError, RuntimeError:
                            pass
        except asyncio.CancelledError:
            pass
        except ProtocolError as exc:
            error = exc
        except TimeoutError, aiohttp.ClientError, OSError, ValueError, UnicodeError:
            error = ProtocolError("Panel connection failed or closed. Press Connect to try again.")
        finally:
            self._ws = None
            self.idle_deadline = None
            self.idle_expires_at = None
            if self._pending and not self._pending[2].done():
                self._pending[2].set_exception(CommandUnconfirmedError())
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(error or ProtocolError("Connection was disconnected."))
            if error and not self._stop_requested:
                self._set_state("error", str(error))
            else:
                self._set_state("disconnected")

    async def _handle_frame(self, raw):
        command = cloud_command(raw)
        if command:
            if command in ("w", "m") and not self._password_requested:
                self._password_requested = True
                await self._send(lambda: cloud_message(self.auth, "password"))
            elif command == "j" and not self._connect_requested:
                self._connect_requested = True
                await self._send(lambda: cloud_message(self.auth, "connect"))
            elif command == "u" and self._phase == "cloud":
                await self._send(lambda: cloud_message(self.auth, "connect", encoded=False))
                await self._send(self._session.announce)
                self._phase = "handshake"
            elif command == "a":
                raise AuthenticationError("Panel access was denied; reconfigure credentials")
            else:
                raise ProtocolError(
                    {
                        "b": "Panel is busy. Disconnect the other app and press Connect again.",
                        "n": "Panel is not polling its cloud service. Press Connect to try again.",
                        "i": "Panel subscription has expired",
                    }.get(command, "Unexpected cloud response")
                )
        elif self._phase == "handshake":
            reply = self._session.handshake(raw)
            if reply:
                await self._send(lambda: reply)
            else:
                await self._send(lambda: self._session.login(self.auth["UserCode"]))
                self._phase = "data"
        elif self._phase == "data":
            try:
                header, body = self._session.receive(raw)
            except ProtocolError as exc:
                if not self.data["areas"] and str(exc) == "Panel integrity check failed":
                    raise AuthenticationError(
                        "Panel login integrity failed; verify credentials"
                    ) from None
                raise
            if header in ("e", "f", "c") and not self.data["areas"]:
                raise AuthenticationError("Panel login rejected; reconfigure credentials")
            if header == "g":
                return  # Authenticated photo data; this integration only consumes area state.
            if header != "d":
                raise ProtocolError("Panel closed or rejected the session. Press Connect again.")
            if not body.strip():
                return
            record = json.loads(body)
            if not isinstance(record, dict):
                raise ProtocolError("Invalid panel status")
            ingest(self.data, record)
            if complete(self.data):
                if record.get("type") == "area" or self.data["observed_at"] is None:
                    self.data["observed_at"] = time.time()
                first = not self.connected
                if first:
                    self._set_state("connected")
                    self._touch()
                snapshot = self.snapshot()
                if self.on_update:
                    self.on_update(snapshot)
                if not self._ready.done():
                    self._ready.set_result(snapshot)
                if self._pending and record.get("type") == "area":
                    operation, area, future = self._pending
                    if not future.done() and any(
                        r.get("R") == area for r in record.get("Detail", [])
                    ):
                        value = self.data["areas"][area]["value"]
                        if operation_confirmed(operation, value):
                            future.set_result(snapshot)
                        elif value in (4, 5, 6):
                            future.set_exception(
                                ProtocolError("Panel could not complete the requested operation")
                            )
        else:
            raise ProtocolError("Unexpected panel session state")

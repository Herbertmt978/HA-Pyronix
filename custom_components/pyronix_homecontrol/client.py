"""Bounded Pyronix transactions. Never log connection fields or wire payloads."""

import asyncio
import json
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

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


@dataclass
class CommandAttempt:
    """Track a write attempt even if sending it loses the connection."""

    sent: bool = False


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


def control_frame(session, operation, area, snapshot):
    """Exactly one area, no forced omissions or output/installer operations."""
    if operation not in ("arm", "disarm") or type(area) is not int or not 0 <= area <= 255:
        raise ProtocolError("Unsupported alarm operation")
    if not complete(snapshot) or area not in snapshot["can_" + operation]:
        raise ProtocolError("Panel user cannot control this area")
    if operation == "arm" and snapshot["areas"][area]["value"] in (4, 5):
        raise ProtocolError("Panel reports the area cannot be set without intervention")
    if session.phase != "data":
        raise ProtocolError("Panel session is not ready")
    # Installed HomeControl SDK sendArmAreas / sendUnArmAreas: 45 / 47.
    return session._frame("D", bytes([45 if operation == "arm" else 47, 1, area]))


class PanelClient:
    def __init__(self, http, auth):
        self.http = http
        self.auth = validate_auth(auth)
        self.lock = asyncio.Lock()

    async def query(self, operation=None, area=None):
        """Fresh status before each command. No actuation retries or queue."""
        if self.lock.locked():
            raise PanelBusyError("Another panel operation is in progress; try again")
        async with self.lock:
            attempt = CommandAttempt() if operation is not None else None
            try:
                async with asyncio.timeout(50):
                    if operation is not None:
                        return await self._control_transaction(operation, area, attempt)
                    attempts = 2
                    for connection_index in range(attempts):
                        try:
                            return await self._transaction(operation, area)
                        except TimeoutError, aiohttp.ClientError, OSError:
                            if connection_index + 1 == attempts:
                                raise
            except asyncio.CancelledError:
                raise
            except AuthenticationError, ProtocolError:
                raise
            except TimeoutError, aiohttp.ClientError, ValueError, UnicodeError, OSError:
                if attempt is not None and attempt.sent:
                    raise CommandUnconfirmedError() from None
                raise ProtocolError(
                    "Panel connection failed or timed out; state is unconfirmed"
                ) from None

    async def _control_transaction(self, operation, area, attempt):
        try:
            # Leave part of the overall 50-second limit for a read-only confirmation.
            async with asyncio.timeout(25):
                return await self._transaction(operation, area, attempt)
        except TimeoutError, aiohttp.ClientError, OSError:
            if not attempt.sent:
                raise
        try:
            snapshot = await self._transaction(None, None)
        except AuthenticationError:
            raise
        except ProtocolError, TimeoutError, aiohttp.ClientError, OSError:
            raise CommandUnconfirmedError() from None
        if operation_confirmed(operation, snapshot["areas"].get(area, {}).get("value")):
            return snapshot
        raise CommandUnconfirmedError(snapshot)

    async def _transaction(self, operation, area, attempt=None):
        session = PanelSession(self.auth["PanelId"], self.auth["PanelPwd"])
        framing, frames = FrameBuffer(), deque()
        snapshot = {
            "areas": {},
            "can_arm": set(),
            "can_disarm": set(),
            "permissions_received": False,
        }
        phase = "cloud"
        password_requested = connect_requested = False
        async with self.http.ws_connect(
            ENDPOINT,
            timeout=aiohttp.ClientWSTimeout(ws_close=3),
            heartbeat=None,
            max_msg_size=1000000,
        ) as ws:
            await ws.send_str(cloud_message(self.auth, "subscription"))
            try:
                while True:
                    if not frames:
                        message = await asyncio.wait_for(ws.receive(), 20)
                        if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                            raise ConnectionError("Panel connection closed; state is unconfirmed")
                        if not message.data.strip():
                            if phase == "data":
                                await ws.send_str(session.heartbeat())
                            continue
                        frames.extend(framing.feed(message.data))
                        if not frames:
                            continue
                    raw = frames.popleft()
                    command = cloud_command(raw)
                    if command:
                        if command in ("w", "m") and not password_requested:
                            password_requested = True
                            await ws.send_str(cloud_message(self.auth, "password"))
                        elif command == "j" and not connect_requested:
                            connect_requested = True
                            await ws.send_str(cloud_message(self.auth, "connect"))
                        elif command == "u" and phase == "cloud":
                            await ws.send_str(cloud_message(self.auth, "connect", encoded=False))
                            await ws.send_str(session.announce())
                            phase = "handshake"
                        elif command == "a":
                            raise AuthenticationError(
                                "Panel access was denied; reconfigure credentials"
                            )
                        else:
                            raise ProtocolError(
                                {
                                    "b": "Panel is busy; connection released",
                                    "n": "Panel is not polling its cloud service",
                                    "i": "Panel subscription has expired",
                                }.get(command, "Unexpected cloud response")
                            )
                    elif phase == "handshake":
                        reply = session.handshake(raw)
                        if reply:
                            await ws.send_str(reply)
                        else:
                            await ws.send_str(session.login(self.auth["UserCode"]))
                            phase = "data"
                    elif phase == "data":
                        try:
                            header, body = session.receive(raw)
                        except ProtocolError as exc:
                            if not snapshot["areas"] and str(exc) == "Panel integrity check failed":
                                raise AuthenticationError(
                                    "Panel login integrity failed; "
                                    "verify credentials before retrying"
                                ) from None
                            raise
                        if header in ("e", "f", "c") and not snapshot["areas"]:
                            raise AuthenticationError(
                                "Panel login rejected; reconfigure credentials"
                            )
                        if header != "d":
                            raise ProtocolError(
                                "Panel rejected the operation; state is unconfirmed"
                            )
                        if not body.strip():
                            continue
                        record = json.loads(body)
                        if not isinstance(record, dict):
                            raise ProtocolError("Invalid panel status")
                        ingest(snapshot, record)
                        if complete(snapshot):
                            if operation is None:
                                snapshot["observed_at"] = time.time()
                                return snapshot
                            if not attempt.sent:
                                frame = control_frame(session, operation, area, snapshot)
                                if operation_confirmed(operation, snapshot["areas"][area]["value"]):
                                    snapshot["observed_at"] = time.time()
                                    return (
                                        snapshot  # Already at target; no physical command needed.
                                    )
                                # Mark before sending: a failed write can still reach the panel.
                                attempt.sent = True
                                await ws.send_str(frame)
                            elif record.get("type") == "area" and any(
                                r.get("R") == area for r in record.get("Detail", [])
                            ):
                                value = snapshot["areas"][area]["value"]
                                if operation_confirmed(operation, value):
                                    snapshot["observed_at"] = time.time()
                                    return snapshot
                                if value in (4, 5, 6):
                                    raise ProtocolError(
                                        "Panel could not complete the requested operation"
                                    )
                    else:
                        raise ProtocolError("Unexpected panel session state")
            finally:
                if phase in ("handshake", "data"):
                    try:
                        await asyncio.wait_for(ws.send_str(session.close()), 2)
                    except TimeoutError, aiohttp.ClientError, ConnectionError, RuntimeError:
                        pass

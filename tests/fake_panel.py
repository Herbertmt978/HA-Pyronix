"""A simulated PyronixCloud socket that answers the real aiopyronix client."""

import asyncio
from collections import deque
import hashlib
import json
import struct
from types import SimpleNamespace

import aiohttp
from aiopyronix.protocol import crypt_body, fixed_key, unwrap, wrap
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tests.const import AUTH


def panel_frame(header, seq, body, key=None):
    head = struct.pack("<BHH", ord(header), seq, len(body))
    tail = body + hashlib.sha256(head + body).digest()[:16]
    if key:
        tail = crypt_body(key, ord(header), len(body), seq, tail)
    return wrap(head + tail)


class FakeSocket:
    def __init__(self, initial_value=0):
        self.replies = asyncio.Queue()
        self.closed = False
        self.commands = []
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
        return panel_frame(
            header, self.seq, body, self.key if header in "deg" else None
        )

    def area(self):
        return self.packet(
            "d",
            json.dumps(
                {"type": "area", "Detail": [{"R": 0, "N": "Day Set", "V": self.value}]}
            ).encode(),
        )

    async def send_str(self, text):
        if text.startswith("(<p>"):
            return
        raw = unwrap(text)
        if raw.startswith(b"<p>"):
            if b"<w>" in raw:
                self.replies.put_nowait("<e>j never-log-this</e>")
            elif b"<a>" in raw:
                self.replies.put_nowait("<e>u </e>")
        elif raw[:1] == b"A":
            self.replies.put_nowait(self.packet("a", b"\x01\x20" + b"12345678"))
        elif raw[:1] == b"B":
            public = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), raw[6:-16]
            )
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
                first = self.area()
                user = self.packet(
                    "d", b'{"type":"user","SetAreas":{"R":[0]},"UnsetAreas":{"R":[0]}}'
                )
                self.replies.put_nowait(first + user)
            elif body[:1] in (b"-", b"/"):
                self.commands.append(body)
                self.value = 1 if body[:1] == b"-" else 0
                self.replies.put_nowait(self.area())
            elif body == b" ":
                self.replies.put_nowait(self.packet("d", b" "))

    async def receive(self):
        reply = await self.replies.get()
        return SimpleNamespace(
            type=aiohttp.WSMsgType.CLOSED if reply is None else aiohttp.WSMsgType.TEXT,
            data=reply,
        )


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

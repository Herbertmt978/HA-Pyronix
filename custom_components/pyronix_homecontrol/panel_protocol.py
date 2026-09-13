"""Pyronix framing and read-only login, transcribed from the installed vendor SDK.

No arm, disarm, omit, output, installer or cloud account mutation command exists
in this module. Payloads and cryptographic material must never be logged.
"""

import base64
import hashlib
import hmac
import struct
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:  # cryptography 48 moved this vendor-required mode; local probe uses 46.
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:
    from cryptography.hazmat.primitives.ciphers.modes import CFB


class ProtocolError(Exception):
    """Fixed messages only; never include untrusted server data."""


class FrameBuffer:
    """A WebSocket message may contain part of, or several, panel packets."""

    def __init__(self):
        self.pending = ""

    def feed(self, text):
        if isinstance(text, bytes):
            text = text.decode("ascii")
        self.pending += text
        if len(self.pending) > 1000000:
            raise ProtocolError("Panel receive buffer limit exceeded")
        frames = []
        while self.pending.strip():
            self.pending = self.pending.lstrip()
            if self.pending.startswith("("):
                end = self.pending.find(")")
                if end == -1:
                    break
                frames.append(self.pending[: end + 1])
                self.pending = self.pending[end + 1 :]
            elif self.pending.startswith("<"):
                end = self.pending.find("</e>")
                if end == -1:
                    break
                frames.append(self.pending[: end + 4])
                self.pending = self.pending[end + 4 :]
            else:
                raise ProtocolError("Unexpected panel transport prefix")
        if not self.pending.strip():
            self.pending = ""
        return frames


def wrap(raw):
    return "(" + base64.encodebytes(raw).decode("ascii") + ")"


def unwrap(text):
    if isinstance(text, bytes):
        text = text.decode("ascii")
    text = text.strip()  # The vendor WebSocket callback trims framing whitespace.
    if not (text.startswith("(") and text.endswith(")")):
        raise ProtocolError("Unexpected transport frame")
    try:
        return base64.b64decode("".join(text[1:-1].split()), validate=True)
    except ValueError:
        raise ProtocolError("Invalid transport encoding") from None


def cloud_message(auth, kind, encoded=True):
    """Allow only subscription, saved credential read and panel connection."""
    if kind not in ("subscription", "password", "connect"):
        raise ProtocolError("Unsupported cloud operation")
    values = [("n", auth["AppUser"]), ("u", auth["PushId"])]
    if kind == "connect":
        values += [("a", auth["PanelId"]), ("b", auth["SystemName"]), ("z", auth["PanelPwd"])]
    else:
        values += [("w" if kind == "subscription" else "j", auth["PanelId"])]
    values += [
        (key, auth.get(field, ""))
        for key, field in (
            ("i", "DeviceId"),
            ("l", "AppVersion"),
            ("t", "AppType"),
            ("m", "OSName"),
            ("v", "OSVersion"),
            ("c", "NetworkType"),
            ("lang", "AppLang"),
        )
    ]
    root = ET.Element("p")
    for key, value in values:
        ET.SubElement(root, key).text = str(value)
    raw = ET.tostring(root, encoding="utf-8", short_empty_elements=False)
    return wrap(raw) if encoded else "(" + raw.decode() + ")"


def cloud_command(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError, TypeError:
        return None
    # Do not return the data: some replies include the app password.
    body = "".join(root.itertext())
    return body[:1] if root.tag == "e" and body[:1] in "wjmuabionx" else None


def fixed_key(password, panel_id):
    if len(password) < 32:
        return hashlib.sha256(password.encode() + b"\0" + panel_id.encode()).digest()[:16]
    try:
        key = bytes.fromhex(password[:32])
    except ValueError:
        raise ProtocolError("Invalid protected credential format") from None
    if len(key) != 16:
        raise ProtocolError("Invalid protected credential format")
    return key


def crypt_body(key, header, length, sequence, body, decrypt=False):
    # SDK: AES-ECB of four LE uint32s seeds the CFB128 feedback register.
    # Its PKCS padding grows unused trailing blocks; only the first matters.
    seed = struct.pack("<IIII", header, length, sequence, 0)
    ecb = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    iv = ecb.update(seed) + ecb.finalize()
    cipher = Cipher(algorithms.AES(key), CFB(iv))
    worker = cipher.decryptor() if decrypt else cipher.encryptor()
    return worker.update(body) + worker.finalize()


class PanelSession:
    def __init__(self, panel_id, password):
        self.panel_id = panel_id.encode()
        if len(self.panel_id) != 8:
            raise ProtocolError("Unexpected panel identifier format")
        self.fixed = fixed_key(password, panel_id)
        self.tx_sequence = self.rx_sequence = 0
        self.key = self.private = None
        self.extra = b""
        self.phase = "announce"

    def _frame(self, header, body=b""):
        self.tx_sequence = (self.tx_sequence + 1) & 0xFFFFFFFF
        head = struct.pack("<BHH", ord(header), self.tx_sequence & 0xFFFF, len(body))
        tail = body + hashlib.sha256(head + body).digest()[:16]
        if header in "DEG":
            if self.key is None:
                raise ProtocolError("Session key is unavailable")
            tail = crypt_body(self.key, ord(header), len(body), self.tx_sequence, tail)
        return wrap(head + tail)

    def receive(self, text):
        raw = unwrap(text)
        if len(raw) < 21:
            raise ProtocolError("Truncated panel frame")
        header, sequence, length = struct.unpack("<BHH", raw[:5])
        if length != len(raw) - 21 or chr(header) not in "abcdefg":
            raise ProtocolError("Invalid panel frame")
        delta = (sequence - (self.rx_sequence & 0xFFFF)) & 0xFFFF
        if not 0 < delta < 128:
            raise ProtocolError("Stale or invalid panel sequence")
        full_sequence = self.rx_sequence + delta
        tail = raw[5:]
        if chr(header) in "deg":
            if self.key is None:
                raise ProtocolError("Encrypted data before session establishment")
            tail = crypt_body(self.key, header, length, full_sequence, tail, decrypt=True)
        if not hmac.compare_digest(tail[-16:], hashlib.sha256(raw[:5] + tail[:-16]).digest()[:16]):
            raise ProtocolError("Panel integrity check failed")
        self.rx_sequence = full_sequence
        return chr(header), tail[:-16]

    def announce(self):
        return self._frame("A")

    def handshake(self, text):
        header, body = self.receive(text)
        if self.phase == "announce":
            if header != "a" or len(body) < 10 or body[2:10] != self.panel_id:
                raise ProtocolError("Panel announcement mismatch")
            if body[1] & 128:
                self.extra = body[10 : 10 + min(max(len(body) - 21, 0), 4)]
            self.private = ec.generate_private_key(ec.SECP256R1())
            public = self.private.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
            self.phase = "ecdh"
            return self._frame("B", b"\x01" + public)
        if self.phase != "ecdh" or header != "b" or len(body) not in (34, 66):
            raise ProtocolError("Panel key exchange rejected")
        try:
            public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), body[1:])
            shared = self.private.exchange(ec.ECDH(), public)
        except ValueError:
            raise ProtocolError("Invalid panel public key") from None
        self.key = hashlib.sha256(self.fixed + shared + self.extra).digest()[:16]
        self.private = None
        self.phase = "data"
        return None

    def login(self, code):
        if (
            self.phase != "data"
            or not code.isascii()
            or not code.isdigit()
            or not 4 <= len(code) <= 8
        ):
            raise ProtocolError("Invalid protected login configuration")
        return self._frame("D", b"," + code.encode() + b"\n")

    def heartbeat(self):
        if self.phase != "data":
            raise ProtocolError("Session is not ready")
        return self._frame("D", b" ")

    def close(self):
        return self._frame("E" if self.key else "F")

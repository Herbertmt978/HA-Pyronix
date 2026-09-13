"""Read one owner's active HomeControl connection and configure HA privately.

No logs, passwords or tokens are written to files or printed. The temporary ADB
server is stopped on exit. Android root access is neither requested nor used.
"""

import argparse
import base64
import getpass
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

PACKAGE = "com.pyronixhc2.android.app"
FIELD_MAP = {
    "n": "AppUser",
    "u": "PushId",
    "i": "DeviceId",
    "l": "AppVersion",
    "t": "AppType",
    "m": "OSName",
    "v": "OSVersion",
    "c": "NetworkType",
    "lang": "AppLang",
    "a": "PanelId",
    "b": "SystemName",
    "z": "PanelPwd",
}


class SetupError(Exception):
    """Fixed public messages, without request or response contents."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError(
            "HA redirected the request. Use its final base URL so the token stays on that server."
        )


def private_input(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise SetupError(
                "Run setup in an interactive terminal that supports hidden input."
            ) from None


def extract_connection(log, system_name):
    candidates = []
    for match in re.finditer(r"webSocketwriteString message sent:\s*\((.*?)\)", log, re.S):
        frame = match.group(1).strip()
        if not frame.startswith("<p>"):
            try:
                frame = base64.b64decode("".join(frame.split()), validate=True).decode()
            except (ValueError, UnicodeError):
                continue
        try:
            root = ET.fromstring(frame)
        except ET.ParseError:
            continue
        if root.tag != "p":
            continue
        fields = {child.tag: child.text or "" for child in root}
        if fields.get("b") == system_name and all(fields.get(k) for k in ("n", "u", "i", "a", "z")):
            candidates.append(
                {dest: fields[src] for src, dest in FIELD_MAP.items() if src in fields}
            )
    if not candidates:
        raise SetupError(
            "No matching connection was found. Open that system in HomeControl, wait for its areas to load, then try again."
        )
    return candidates[-1]


def validate_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SetupError("Use your Home Assistant base URL, without a token or login details.")
    if parsed.path not in ("", "/"):
        raise SetupError("Use the base URL without a dashboard path.")
    if parsed.scheme == "http":
        host = parsed.hostname
        try:
            local = ipaddress.ip_address(host).is_private
        except ValueError:
            local = host == "localhost" or host.endswith(".local")
        if not local:
            raise SetupError("Use HTTPS for a remote Home Assistant address.")
    return value.rstrip("/")


def find_adb(value):
    candidate = value or shutil.which("adb")
    if candidate and Path(candidate).is_file():
        return str(Path(candidate))
    bluestacks = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "BlueStacks_nxt/HD-Adb.exe"
    )
    if bluestacks.is_file():
        return str(bluestacks)
    raise SetupError("ADB was not found. Use --adb with the path to adb or HD-Adb.exe.")


class PrivateADB:
    def __init__(self, executable, device):
        self.executable = executable
        self.device = device
        self.port = None

    def run(self, *args, device=False):
        command = [self.executable, "-P", str(self.port)]
        if device:
            command += ["-s", self.device]
        command += list(args)
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            raise SetupError(
                "ADB failed. Check that debugging is enabled and the device address is correct."
            )
        if len(result.stdout) > 16 * 1024 * 1024:
            raise SetupError("The app log is too large. Reopen HomeControl and try again.")
        return result.stdout.decode(errors="replace")

    def __enter__(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        try:
            self.run("start-server")
            if re.fullmatch(r"127\.0\.0\.1:\d+", self.device):
                self.run("connect", self.device)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        if self.port is not None:
            try:
                self.run("kill-server")
            except (SetupError, subprocess.TimeoutExpired):
                pass

    def read_homecontrol(self):
        pid = self.run("shell", "pidof", PACKAGE, device=True).strip()
        if not re.fullmatch(r"\d+", pid):
            raise SetupError("Open HomeControl 2.0 before running this helper.")
        return self.run("logcat", "--pid=" + pid, "-d", "-v", "raw", "-t", "10000", device=True)


def configure_ha(base, token, details):
    opener = urllib.request.build_opener(NoRedirect())

    def api(path, body):
        request = urllib.request.Request(
            base + path,
            json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with opener.open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise SetupError("Home Assistant rejected the access token.") from None
            raise SetupError(
                "Home Assistant rejected setup. Check that the integration is installed and HA has restarted."
            ) from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise SetupError(
                "Home Assistant could not be reached. Check its URL and connection."
            ) from None

    flow = api("/api/config/config_entries/flow", {"handler": "pyronix_homecontrol"})
    if flow.get("type") != "form" or flow.get("step_id") != "user":
        raise SetupError(
            "The Pyronix setup form is unavailable. Check installation and existing entries."
        )
    result = api(
        "/api/config/config_entries/flow/" + flow["flow_id"],
        {"connection_details": json.dumps(details)},
    )
    if result.get("type") == "abort" and result.get("reason") == "already_configured":
        raise SetupError(
            "This panel is already configured. Use Reconfigure in Home Assistant to update its credentials."
        )
    if result.get("type") != "create_entry":
        raise SetupError("Home Assistant did not accept the connection details.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--system-name", required=True, help="Exact system name shown in HomeControl"
    )
    parser.add_argument(
        "--device", required=True, help="ADB device serial, or BlueStacks loopback address and port"
    )
    parser.add_argument("--ha-url", required=True, help="Home Assistant base URL")
    parser.add_argument("--adb", help="Path to adb or BlueStacks HD-Adb.exe")
    args = parser.parse_args()
    base = validate_url(args.ha_url)
    with PrivateADB(find_adb(args.adb), args.device) as adb:
        details = extract_connection(adb.read_homecontrol(), args.system_name)
    code = private_input("Pyronix user code (hidden): ")
    if not code.isascii() or not code.isdigit() or not 4 <= len(code) <= 8:
        raise SetupError("The user code must contain 4 to 8 digits.")
    details["UserCode"] = code
    token = private_input("Temporary Home Assistant access token (hidden): ")
    if not token:
        raise SetupError("A Home Assistant access token is required for setup.")
    configure_ha(base, token, details)
    print("Pyronix was added. Check Settings > Devices & services in Home Assistant.")
    print("You can now delete the temporary HA access token and close BlueStacks.")


if __name__ == "__main__":
    try:
        main()
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except (KeyboardInterrupt, EOFError):
        print("Setup cancelled.", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(
            "Setup failed. No credentials were saved. Check the app, ADB and HA connection.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

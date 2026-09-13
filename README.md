# Pyronix HomeControl for Home Assistant

I built this so I can use my Pyronix alarm from Home Assistant without leaving BlueStacks running. HA connects directly to PyronixCloud and opens an encrypted connection to the panel.

This is an early version. I have verified a real login and area status reads with HomeControl 2.0 app **6.3.0** and panel firmware **2.11**. Arm and disarm pass simulated tests, but I have **not yet completed a real arm/disarm test**. I have not confirmed compatibility with other panels or app versions.

## What it does

- Adds an alarm entity for each area the panel user can control.
- Reads status every two minutes and releases the connection after each request.
- Requires the Pyronix user code before arming or disarming an area.
- Reads fresh status before a command and waits for the panel to confirm the result.
- Does not retry a command if its acknowledgement is lost.

The area named `Night Set` uses HA's **Arm night** action. Other areas use **Arm away**. Each action applies only to that entity's area. I have not added an “arm everything” action, forced omissions, outputs or installer settings.

A failed or uncertain request makes the entity unavailable. An unfamiliar panel state appears as unknown. HA does not assume the alarm is disarmed when it loses contact.

## Before starting

You need a working HomeControl 2.0 account, a panel that connects successfully in the app, its separate **user code** and **app password**, and access to your HA configuration folder. The integration uses the Pyronix cloud service; it is not an offline LAN integration.

I used BlueStacks on Windows for the initial setup. It is only needed to obtain the connection details from your own signed-in app. The helper does not need Android root access. App versions that stop exposing those details in their process log will need another setup method.

## Install the integration

1. Download this repository using **Code → Download ZIP**, then extract it.
2. Copy `custom_components/pyronix_homecontrol` into your HA `/config/custom_components/` folder. The result should include `/config/custom_components/pyronix_homecontrol/manifest.json`.
3. Restart Home Assistant.

This is a manual installation; I have not published a HACS release yet. I check compatibility with HA's 2026.7.1 and 2026.9.1 test fixtures, alongside import and startup checks on my DEV system.

## Connect your own alarm

1. Install Python 3.11 or later on the computer running BlueStacks. The setup helper only uses Python's standard library.
2. Sign in to **HomeControl 2.0** in BlueStacks.
3. Enable **Android Debug Bridge** in BlueStacks settings. Leave root access off. Note its loopback address and port; these can differ between installations.
4. Open your alarm system in HomeControl. Enter its user code and app password, then wait until its areas are visible.
5. In HA, open your user profile's **Security** page and create a temporary **Long-lived access token** for setup.
6. Open a terminal in the extracted repository and run the command below. Replace the system name, ADB port and HA address with your own values.

```powershell
python tools/setup_from_adb.py --system-name "My alarm" --device "127.0.0.1:5555" --ha-url "http://homeassistant.local:8123"
```

7. Enter the **Pyronix user code** and the temporary **HA access token** at the hidden prompts. Do not put either in the command itself.
8. Open **Settings → Devices & services → Pyronix HomeControl** in HA. Check that the area states agree with the app before trying a control.
9. Delete the temporary HA token. The integration does not need it after setup. You can close BlueStacks and turn its debugging option off again.

The helper looks for ADB on your PATH or in the usual BlueStacks installation folder. If necessary, add `--adb "C:\path\to\HD-Adb.exe"` to the command.

The helper reads only the HomeControl process log, selects the system name you supplied and sends the required fields to your HA setup flow. It does not print or save those fields. It also stops its own temporary ADB server. HA stores the resulting connection details in its normal integration configuration, so treat HA backups as private.

## Add controls to a dashboard

Edit a dashboard and add an **Alarm panel** card for each Pyronix area you want to use. Enter the **Pyronix user code** when arming or disarming. This integration does not change any separate PIN you use for HA notifications or tablet kiosks.

The panel may refuse to arm when a detector is open or a fault needs attention. I leave that decision with the panel. I have not enabled forced arming or automatic alarm schedules.

## If something goes wrong

- **No matching connection found:** reopen the selected system in HomeControl, wait for the areas to load and run the helper again. Match the system name exactly.
- **ADB cannot connect:** check that debugging is enabled and use the address and port for the correct BlueStacks instance.
- **Integration unavailable:** restart HA after copying the files. Check that the folder was not nested twice.
- **Panel busy or offline:** close the live panel screen in the phone app and check its network connection. HA releases each connection instead of keeping a permanent session open.
- **Credentials changed:** use **Reconfigure** on the integration and enter the new app password and user code.
- **Command timed out:** check the real panel or app before trying again. A timeout does not prove the command failed.

Do not upload app logs, access tokens, connection details or HA backups to an issue. A description of the problem, versions and the fixed error message is enough to start.

## Development

I run the HA tests in an isolated Linux environment with Python 3.14:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
ruff check .
ruff format --check .
pytest -q
```

The tests use synthetic credentials and a simulated panel. They cover the encrypted handshake, fragmented messages, area permissions, HA services, wrong codes and lost acknowledgements. They do not arm a real alarm.

I wrote this as an independent integration. It is not an official Pyronix or Home Assistant product.

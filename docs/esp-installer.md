# State Life Device Installer

The State Life Device Installer is a Windows app for staff at the sites. It
prepares a new board for a ZKTeco or Hikvision attendance machine, checks a
board that is not working, and says plainly what to press, unplug or plug back
in. The approved design is in [esp-installer-design.md](esp-installer-design.md).

Parts 1 to 3 are for the people who use the app. Parts 4 and 5 are for head
office and developers.

## 1. Install the app

You need:

- a computer with Windows 10 or Windows 11;
- internet on that computer;
- the Setup file from head office, called
  `StateLife-Device-Installer-Setup-<version>.exe`;
- your ADD username and password (the same ones you use for the dashboard).

You do not need an administrator password. The app installs only for you.

1. Copy the Setup file onto the computer. It can come by WhatsApp, by email or
   on a USB stick. Put it in your **Downloads** folder.
2. Double-click the Setup file.
3. Windows may show a blue box that says **"Windows protected your PC"**.
   This is normal for this app. It appears because the app does not have a
   paid Microsoft certificate.
   1. Click **More info**.
   2. Check that the app name is the Setup file from head office.
   3. Click **Run anyway**.
4. Read the licence. Click **I accept the agreement**, then **Next**.
5. Leave **Put a shortcut on the desktop** ticked. Click **Next**, then
   **Install**.
6. Click **Finish**. The app opens.

**A new version:** close the app first, then run the new Setup file in the
same way. You do not need to remove the old version first. If the app is still
open, Setup says so and waits: close the app, then click **OK**. If a board is
being set up, let the app finish it first.

**Removing the app:** close the app first. Then open **Settings**, then
**Apps**, then **Installed apps**.
Find **State Life Device Installer** and click **Uninstall**. The app asks if
you also want to delete the saved install records and logs. Choose **No**,
unless head office tells you otherwise.

## 2. Use the app

1. Open **State Life Device Installer** from the desktop or the Start menu.
2. Sign in with your ADD username and password.
3. Choose **Set up a new board**, or **Check or fix a board**.
4. Choose the attendance machine: **ZKTeco** or **Hikvision**.
5. Type the **ZONE ID**. The app fills in the zone name. Check the name and
   the group (Life or Health) in the preview.
   - Leave everything under **Advanced settings** as it is, unless head
     office tells you to change it.
   - For **Hikvision**, also type the attendance machine's IP address and
     password, choose its model, and type its serial number exactly as the
     machine's menu shows it. The board checks it letter for letter.
6. Connect the board to the computer with a USB cable. Use the socket on the
   board marked **USB**.
7. Wait while the app checks the board, then click **Next**. Read the
   summary. Tick **I understand** and click **Start the setup**.
8. Wait while the app sets up the board. **Keep the cable plugged in.**
9. When the app asks, unplug the cable. Wait 3 seconds. Plug it back in.
   When the board has started, the app asks you to do it once more: unplug
   the board again, wait 3 seconds, and plug it back in. The app checks that
   the board keeps its settings.
10. The app watches the board start. When it says **The board is ready**:
    - write down the board's Wi-Fi name and the board's Wi-Fi password shown
      on the screen;
    - click **Save a record**.
11. At the site, power on the board. Connect a phone to the board's own Wi-Fi
    (`SLIC ATTENDANCE-` followed by four characters), using the board's Wi-Fi
    password from step 10. Open `http://192.168.254.1` and choose the site's
    Wi-Fi. If you need the name and password again, connect the board to the
    computer and choose **Check or fix a board**: the app shows them there too.

Press **Help** on any screen to see pictures of what to do.

Keep the computer plugged in to power if you can. The app stops it from going
to sleep while it sets up a board and while it watches the board start.

## 3. If something goes wrong

| What you see | What to do |
|---|---|
| "Windows protected your PC" and no **Run anyway** button | Click **More info** first. If there is still no button, this computer blocks new apps. Ask head office for help. |
| Your antivirus removes or blocks the Setup file | Ask your IT person to allow it. Head office can check the file with `SHA256SUMS.txt`. |
| The app says there is no internet | Check the network cable or Wi-Fi. Open any website to test. The app checks again by itself. |
| The app cannot reach head office | Wait a few minutes and try again. If it goes on, call head office. |
| The app pauses sign-in | You typed a wrong password 3 times. Wait 10 minutes, then try again. More wrong tries would also lock your dashboard sign-in. |
| No board is found | Use a different USB cable: some cables only charge. Try another USB socket on the computer. Some new boards only show up in setup mode: hold down the **BOOT** button. While you hold it, press and release **RST**. Then let go of **BOOT**. |
| "Use the socket marked USB" | The cable is in the other socket on the board. Move it to the socket marked **USB**. |
| "Put the board into setup mode" on the **Connect the board** screen | The board is running other software. This is normal for a new board. Hold down the **BOOT** button. While you hold it, press and release **RST**. Let go of **BOOT**. When the screen says **Board found**, click **Next**. If the message is still there after 5 seconds, unplug the board, hold down **BOOT** while you plug it back into the socket marked **USB**, then let go of **BOOT**. If it is still there after 3 tries, click **Help**, then **Save a help file**, and send the file to head office. |
| "Connect only one board" | Unplug every other board. |
| "This computer cannot read the board ID" | This computer, or the USB socket or hub, does not tell the app which board it is, so the app will not write to the board there. Plug the cable straight into another USB socket on the computer, not into a hub. Then click **Try again** (on **Check or fix a board**: **Check again**). If that does not help, use another computer. |
| "The board keeps restarting" | Usually the cable or the USB socket does not give enough power. Use a short cable straight into the computer, not a hub, or another USB socket. Then click **Check again** (or **Try again** after the first start). |
| "This board is faulty" | Put a FAULTY label on it, keep it aside and tell head office. |
| The app cannot start the board's setup mode | Hold down the **BOOT** button. While you hold it, press and release **RST**. Let go of **BOOT**. Click **Try again**. |
| Setup stopped before the end | Do what the screen says, then click **Finish the setup**. If the app was closed: choose **Set up a new board**, type the same details and connect the same board. The check then offers **Finish the setup**. |
| The board started with other settings | Click **Fix settings**. The app checks the board again and offers **Change its settings**. |
| "The app did not see the board start" | Unplug the board, wait 3 seconds and plug it back in without pressing any button. Click **Try again**. If it happens again, click **Check the board**. |
| "The board lost its settings" | It lost them when it restarted, or it came back with other settings. Do not use the board; if it came from a site, do not put it back. Unplug it, put a label on it that says DO NOT USE with its board ID (the screen shows it), and keep it aside; do not throw it away. Click **Help**, then **Save a help file**, at once (before you set up another board), and send the file to head office. |
| "The app did not see the board read its settings" | Click **Try again**. When the screen asks, unplug the board, wait 3 seconds and plug it back in without pressing any button. If it happens again, do not install the board: put it aside, click **Help**, then **Save a help file**, and send the file to head office. |
| "This app cannot change this board's settings" | The board was locked in an older way. Leave it as it is and call head office with the board ID. |
| "The app is already open" | Only one window of the app can run. Use the window that is open; look for its icon on the taskbar. |
| Setup or the uninstaller says the app is running | Close the app. If it is setting up a board, wait until it has finished. Then click **OK**. |
| "Please wait a moment" | Head office is busy. Wait as many minutes as the screen says, then click **Try again**. |
| Anything else | Click **Help**, then **Save a help file**. Send the file to head office. It contains no passwords. |

## 4. Release runbook

### Build a release

1. Set the new version in `apps/esp_installer/src/sl_installer/__init__.py`
   (`__version__`) and merge to `main`.
2. Check that the `firmware-signing` environment has both secrets:
   - `ESP_INSTALLER_FIELD_DEFAULTS_JSON` (see below);
   - `ZONE_LITE_SETUP_PASSWORD`, the board's Wi-Fi password (the password of
     each board's own setup Wi-Fi, its hotspot). The firmware builds already
     use it; the app shows it on the last screen and on Check a board.
3. Start the workflow **ESP installer release**
   (`.github/workflows/esp-installer-release.yml`) on `main`, with the input
   `confirmation` set to `BUILD INSTALLER RELEASE`:

   ```sh
   gh workflow run esp-installer-release.yml --ref main \
     -f confirmation="BUILD INSTALLER RELEASE"
   ```

   Any other branch or confirmation skips the job. Approve the
   `firmware-signing` environment if GitHub asks.
4. The job runs on a clean `windows-2022` machine. It installs the
   hash-locked packages, runs the app tests, builds the app with the secrets,
   then installs the Setup silently, runs `--tool esptool version`,
   `--self-test` and `--screenshots`, uninstalls it and checks that it is gone.

Pull requests that touch `apps/esp_installer/**` run the same steps without
any secrets. They make a **TEST** Setup (`...-Setup-<version>-TEST.exe`) and
the app shows a **TEST BUILD** banner. Never give a TEST Setup to the sites.

### Check the restart check on Windows before the first share

The restart check (part 2, step 9) passes only when the app sees the board name
its settings after the second replug. The board prints those lines once, about
1.1 s after it gets power. On the Mac the app's first line after a replug came
at about 0.9 s; nobody has measured a Windows laptop yet, and Windows can list
the port later. A computer that opens the port too late fails every restart
check with "The app did not see the board read its settings", so no setup on it
can finish. Before you share the first Setup that has the restart check:

1. On a Windows 11 field laptop, set up one new board with that Setup, or watch
   the first supervised setup at a site.
2. Both starts must pass, and the app must reach **The board is ready**.
3. Open `%LOCALAPPDATA%\StateLife\DeviceInstaller\logs\console-<board ID>-<request>.log`.
   In its second start, the first line the board printed after `the port ... is
   open` carries the time since power-on in brackets, for example `I (942)`. It
   must come well before the settings line, `I (1106) zone_config: Loaded
   per-device provisioning for zone=...`.
4. Look at a few more starts: on the same board, **Check or fix a board** then
   **Fix settings** watches both starts again.

If that laptop keeps saying "The app did not see the board read its settings",
do not share the Setup. Ask the owner whether a later line of the board's
program may count at the restart check.

### Where the Setup appears

The run page lists the artifact `StateLife-Device-Installer-Setup-<version>`.
It holds the Setup `.exe`, `SHA256SUMS.txt` and the screenshots, and is kept
for 30 days. To download it from a terminal:

```sh
gh run list --workflow esp-installer-release.yml --limit 5
gh run download <run-id> --name StateLife-Device-Installer-Setup-<version>
```

The smoke-test logs are in `esp-installer-smoke-logs-<commit>`.

### Share it

- **WhatsApp:** send the `.exe` as a **Document**, not as a photo or video.
- **Email:** Gmail and many other mail systems block `.exe` files, even
  inside a `.zip`. Send a shared-folder link instead.
- **USB stick:** copy the `.exe` onto it.
- Tell the sites to expect the "Windows protected your PC" screen (part 1).
- To prove a copy is unchanged, compare its SHA-256 with `SHA256SUMS.txt`:
  `Get-FileHash .\StateLife-Device-Installer-Setup-<version>.exe` in
  PowerShell, or `certutil -hashfile <file> SHA256`.

### Update the field defaults secret

The defaults that look secret (Wi-Fi password, comm key, hotspot password)
never go into git. A release build reads them from the environment and bakes
them into the app. To change one, update the secret, then build a new release.

`ESP_INSTALLER_FIELD_DEFAULTS_JSON` is one JSON object with exactly these keys:

| Key | Type | Meaning |
|---|---|---|
| `wifi_ssid` | text, 1-32 bytes | Wi-Fi name the board uses until the site Wi-Fi is chosen |
| `wifi_password` | text, 8-63 characters | password of that Wi-Fi (a dummy network) |
| `zkt_port` | number, 1-65535 | ZKTeco port |
| `zkt_comm_key` | number, 0-4294967295 | ZKTeco comm key |
| `zkt_preferred_ip` | text | `0.0.0.0` (find the machine), or the machine's LAN address |
| `device_id` | text | first device ID offered in a zone |
| `hik_port` | number, 1-65535 | Hikvision port |
| `hik_transport` | text | `http_digest`. `https` is refused: it needs the attendance machine's own certificate, which the app cannot add |
| `hik_username` | text | Hikvision user name |

The JSON looks like this; replace each `<...>` with the real value (numbers
have no quotes):

```json
{"wifi_ssid": "RE7", "wifi_password": "<dummy Wi-Fi password>", "zkt_port": 4370,
 "zkt_comm_key": <comm key number>, "zkt_preferred_ip": "0.0.0.0", "device_id": "1",
 "hik_port": 80, "hik_transport": "http_digest", "hik_username": "admin"}
```

Set it without saving it in a file or in your shell history: run the command
below, paste the JSON on one line when `gh` asks for the value, and press
Enter.

```sh
gh secret set ESP_INSTALLER_FIELD_DEFAULTS_JSON --env firmware-signing
```

The build
refuses unknown or missing keys, wrong types, and values the app itself would
not accept, and names only the key in its error. It never prints a value, and
it masks the JSON's values in the log as well as the whole secret.

`ZONE_LITE_SETUP_PASSWORD` must stay the board's Wi-Fi (hotspot) password that
the firmware bundles were built with, or the app shows the wrong password at
the end of a setup and on Check a board.

## 5. Developer notes

### Layout

```
apps/esp_installer/
  pyproject.toml              package metadata; the version comes from sl_installer.__version__
  requirements.lock           runtime packages, hash-pinned (what the app bundles)
  requirements-build.lock     PyInstaller, pytest, pytest-qt and setuptools, hash-pinned
  src/sl_installer/           the app (core = engine without Qt, ui = screens)
  tests/                      core and ui tests
  packaging/
    build.py                  the whole build, see below
    sl_installer.spec         PyInstaller recipe (one folder, no console)
    launcher.py               PyInstaller entry script
    installer.iss             Inno Setup 6 script
    smoke_test.ps1            install, check and uninstall a built Setup (Windows PowerShell 5.1)
    update_locks.py           regenerates both locks
    notices.py                writes THIRD-PARTY-NOTICES.txt from the installed packages
    licenses/                 licence texts that some packages do not ship
    EULA.txt                  the licence shown by Setup
    version_info.txt          Windows version resource template
    tests/                    tests for the tools above and for __main__.py
```

The app runs on Python 3.9 or newer; releases use 3.12. Check the syntax with
the Mac's Python 3.9:

```sh
python3 -m compileall -q apps/esp_installer/src
```

### The bench tool

`python -m sl_installer.core.bench inspect|watch|diagnose|install` drives the
engine from a terminal with a real board (see its docstring). Every command
line the engine builds is esptool 5.1.0's, and esptool 5.1.0 needs Python 3.10
or newer: the Mac's own `python3` (3.9) has esptool 4.7.0, which refuses them.
From a source checkout the tools run as `<python> -m esptool|espefuse`, so name
a Python that has esptool 5.1.0:

```sh
PYTHONPATH=apps/esp_installer/src python3 -m sl_installer.core.bench \
  --tool-python /path/to/python3.12 inspect
```

`SL_INSTALLER_TOOL_PYTHON=/path/to/python3.12` does the same for the bench and
for `python -m sl_installer` from a checkout. The frozen app ignores both and
always runs its own copy. Before `inspect` and `install` touch a board, the
bench runs `esptool version` and `espefuse --help` (neither opens a port) and
stops unless both say 5.1.0. The bench itself still needs `httpx`, `pyserial`
and `cryptography` in the Python that runs it. `install` needs `--bundle-id`
for a new or unfinished board; changing the settings of a finished board
writes no board software, so it needs none. `inspect` and `install` take the
app's own lock for the same data folder: while the app's window is open they
stop (exit code 6) before touching the board, and the window does not open
while they run.

### Runtime behaviour worth knowing

- Only one window runs at a time. The app holds an operating-system lock on
  `%LOCALAPPDATA%\StateLife\DeviceInstaller\state\app.lock` while its window is
  open; a second start shows "The app is already open" and stops before it
  opens the log. The lock goes away by itself when the app exits or crashes.
  Tool runs (`--tool`), `--self-test` and `--screenshots` are not limited.
- Every setup runs in a holder of that lock (the window, or the bench's
  `install`). So once the window holds it, no setup can be running, and it
  wipes every work folder (`work\sl-install-*`) that a crash or power cut left
  behind, however new: the board's settings and, after a cut during the key
  step, its security key file. Without the lock (`--self-test`, the bench's
  `watch` and `diagnose`) only folders older than 6 hours are wiped.
- Before every call of a setup the app finds the board again by its USB board
  ID, and it writes only to a port that has the board's ID. Some computers and
  hubs give none (Windows then reports a location ID such as
  `5&2B33E3B5&0&12`, which never counts as a board ID): the check says "This
  computer cannot read the board ID" and offers no setup, finish or settings
  change there.
- Every setup watches two starts: the first one and, after one more replug,
  the restart check, which passes only when the board shows its settings again
  for the same zone. What the board prints in each watched start is kept,
  redacted like the log, in `logs\console-<board ID>-<request>.log` (one file
  per setup, at most 256 KB, the newest 20); a help file carries the newest 10.
- While a board is being set up, and while its starts are watched, the app
  asks Windows to keep the computer and the screen awake
  (`SetThreadExecutionState`, `core/keepawake.py`): a laptop that sleeps
  halfway suspends USB under esptool.

### Tests in the dev container

The `esp-installer-dev:local` image has Python 3.12, PySide6-Essentials,
esptool, pytest and pytest-qt, with `QT_QPA_PLATFORM=offscreen`:

```sh
docker run --rm -v "$PWD":/w -w /w/apps/esp_installer -e PYTHONPATH=src \
  esp-installer-dev:local python -m pytest tests -q
```

### The command line

| Command | What it does |
|---|---|
| `State Life Device Installer.exe` | the app |
| `... --tool esptool ARGS` / `--tool espefuse ARGS` | runs the bundled tool and exits with its code. The engine starts the frozen app like this where a source checkout runs `python -m esptool`. Output is UTF-8. |
| `... --self-test` | runs `sl_installer.core.selftest.run()`, opens and closes the window on fake services, then builds the real services and a window on them without starting it (nothing is contacted); prints `PROBLEM:` lines and exits 0 or 1 |
| `... --screenshots DIR` | saves every screen as PNG files in `DIR` |
| `... --server URL` | uses another ADD server (tests only). It must use `https`, unless it runs on this computer (`127.0.0.1`, `localhost` or `::1`), and the address may not hold a user name or password |
| `python -m sl_installer ...` | the same, from source |

### Build

`packaging/build.py` does every step:

1. with both secrets, writes `src/sl_installer/resources/field_defaults.json`
   and deletes it again once PyInstaller has copied it. Without them it is a
   TEST build: there is no such file, so the app shows its TEST BUILD banner.
   `--release` refuses to build without the secrets;
2. makes `app.ico` (16-256 px) and the Inno Setup pictures (100-250 % scale)
   from `apps/add_frontend/public/state-life-logo.png` into `build/branding`;
3. writes the version resource and `THIRD-PARTY-NOTICES.txt`;
4. runs PyInstaller into `dist/State Life Device Installer/`, then adds
   `EULA.txt`, the notices and `licenses/esptool-5.1.0.tar.gz` (the exact
   esptool source, checked against the lock's hash);
5. checks that the folder has every resource and tool data file;
6. on Windows, runs Inno Setup 6 (`ISCC.exe`, or the path in `ISCC`) and
   writes `dist/SHA256SUMS.txt`.

On Windows, with Python 3.12 and Inno Setup 6:

```powershell
python -m pip install --require-hashes --no-deps -r apps\esp_installer\requirements-build.lock
python -m pip install --require-hashes --no-build-isolation -r apps\esp_installer\requirements.lock -r apps\esp_installer\requirements-build.lock
python apps\esp_installer\packaging\build.py
powershell -File apps\esp_installer\packaging\smoke_test.ps1 -Setup apps\esp_installer\dist\StateLife-Device-Installer-Setup-1.0.0-TEST.exe -OutputDir smoke
```

PyPI has esptool 5.1.0 only as source, so the first command installs the
locked setuptools and the second builds esptool with it
(`--no-build-isolation`). That keeps every download hash-checked.

To check the PyInstaller recipe in the dev container (Linux, no installer):

```sh
docker run --rm -v "$PWD":/w -w /w/apps/esp_installer esp-installer-dev:local sh -c '
  pip install -q --require-hashes --no-deps -r requirements-build.lock &&
  pip install -q --require-hashes --no-build-isolation -r requirements.lock -r requirements-build.lock &&
  python packaging/build.py &&
  app="dist/State Life Device Installer/State Life Device Installer" &&
  "$app" --tool esptool version && "$app" --self-test && "$app" --screenshots /tmp/shots'
```

What the bundle keeps: Qt Core, Gui, Widgets and Svg, with only the
`qwindows` platform plugin, the styles, the SVG and ICO image plugins and the
SVG icon engine. No Qt translations, no software OpenGL, no UPX. The app runs
in Python's UTF-8 mode.

### Locks

`requirements.lock` and `requirements-build.lock` come from `pyproject.toml`.
After changing a dependency there, regenerate both in the dev container:

```sh
docker run --rm -v "$PWD":/w -w /w/apps/esp_installer esp-installer-dev:local \
  sh -c "pip install -q pip-tools==7.6.2 && python packaging/update_locks.py"
```

It adds the packages that only Windows needs (colorama, pefile,
pywin32-ctypes), takes each file's SHA-256 from PyPI, and fails unless every
pin has a Windows x64 / CPython 3.12 wheel (esptool: its source release) and
all of its Windows dependencies are locked.

### Installer details

- Per-user install into `%LOCALAPPDATA%\Programs\State Life Device Installer`;
  no administrator rights; Windows 10 1809 or newer, x64.
- `AppId` `{49E622E9-C86D-45A8-B698-80A6E828B344}` must never change: Windows
  uses it to find the app for updates and removal.
- An update deletes the old `_internal` folder first, so files of two versions
  never mix, as long as the app is closed. Setup and the uninstaller make sure
  of that: the open window holds the named mutex `StateLifeDeviceInstaller`
  (`AppMutex` in `installer.iss`, `APP_MUTEX` in `__main__.py`), so they ask for
  the app to be closed and wait. The window does not close while a board is at a
  step that must not stop, and Setup never forces it closed.
- The app keeps its data in `%LOCALAPPDATA%\StateLife\DeviceInstaller`
  (`sl_installer/config.py`): `bundles`, `logs`, `records`, `state`, `work`.
- Uninstall removes the app, its shortcuts, the downloaded firmware
  (`bundles`) and the scratch folders (`work`, `state`). It then asks, with No
  as the default, whether to delete the install records and logs too. A
  silent uninstall keeps them.
- Neither the app nor the Setup is code-signed (owner decision), so Windows
  SmartScreen shows "Windows protected your PC".
- On a new Windows 11 computer, **Smart App Control** can block an unsigned
  app with no **Run anyway** button at all. It is under Windows Security, then
  **App & browser control**, then **Smart App Control settings**. Turning it
  off is the computer owner's decision: on many Windows versions it cannot be
  turned back on without resetting Windows. Code-signing the Setup is the only
  fix that needs no change on the computer.

### Licences

`EULA.txt` is the app's licence. `THIRD-PARTY-NOTICES.txt` is generated at
build time from the runtime lock and each package's licence files. It covers
Python (PSF), Qt and PySide6 (LGPL-3.0: unmodified, dynamically linked DLLs,
with where to get their source), esptool (GPL-2.0-or-later: unmodified, in a
separate process where a small part of the app hands over to it; its source
ships in `licenses\`), the flasher stub that esptool loads into the board and the
PyInstaller bootloader. The app uses only the stub in `stub_flasher\1`
(esptool-legacy-flasher-stub v1.8.0, GPL-2.0-or-later). esptool ships only the
stub's binary, so the notice says where the stub's source is published
(<https://github.com/espressif/esptool-legacy-flasher-stub/releases/tag/v1.8.0>)
and that State Life head office can supply a copy on request. The stubs in
`stub_flasher\2`, which the app does not use, are Apache-2.0 or MIT.

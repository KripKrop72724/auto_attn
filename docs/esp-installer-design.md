# State Life Device Installer: design

Status: approved for build on 1 Nov 2026 (owner decisions below). Branch `feature/windows-esp-installer`.

## 1. Goal

A Windows 11 desktop app, with an installer, that a non-technical person at a remote site can use. It prepares a Zone Lite ESP32-S3 board for a ZKTeco or Hikvision attendance terminal, finds faulty boards, and says plainly what to press, unplug or replug. Nothing else may need to be installed on the PC. The app needs internet, because per-board secrets are made only on the ADD server.

## 2. Owner decisions (1 Nov 2026)

| Topic | Decision |
|---|---|
| Sign-in | The same ADD admin username and password as the dashboard. The owner controls who gets the app. |
| Defaults | ZKT: Wi-Fi `RE7` with its (dummy) password, port 4370, comm key 1979, preferred IP 0.0.0.0, device ID 1. Everything except ZONE_ID and ZONE_NAME sits under Advanced. The password may be shown. |
| Site Wi-Fi | Real Wi-Fi is chosen at site through the board's hotspot (setup portal). The app **shows the hotspot name and password** on the final screen. |
| Hikvision | Supported from main. New boards get **HIK 3.1.1 built from exact source dc7d9c3**, the same build as Jhelum. |
| Zone name | Auto-filled from ZONE_ID, with a preview of the public name and Life/Health group. |
| Code signing | None. Windows shows "Windows protected your PC"; the owner accepts this. |
| Language | English only: plain words and pictures. |
| Already-set-up boards | The app may rewrite their settings (zone, comm key, Hikvision password) after safety checks. |
| Distribution | The owner shares the Setup `.exe` by WhatsApp, email or USB stick. |

Firmware defaults that look secret (Wi-Fi password, comm key, hotspot password) never go into git. The release build injects them from GitHub secrets (§9).

## 3. Architecture

```
Windows app (PySide6 + bundled esptool 5.1.0, PyInstaller onedir, Inno Setup)
   |  HTTPS, ADD admin cookie + CSRF + password step-up
   v
ADD API  /api/v1/installer/*   (new router, apps/add_backend/zk_add/installer_api.py)
   |  internal network, bearer token
   v
add-provisioner  POST /internal/v2/nvs-packages   (builds encrypted NVS + HMAC key from the fleet root)
ADD API reads signed "installer bundles" from a read-only host store (/installer-bundles)
   ^
   |  published once per firmware version by firmware-key-bootstrap.yml (PUBLISH INSTALLER BUNDLE)
self-hosted Windows runner (vault keys sign the bootloader; key-1 signs the bundle manifest)
```

- **Stays on the server:** the fleet root, HKDF derivations, ORDS credentials, NVS generation and signing keys.
- **Happens on the PC:** USB discovery, board inspection, flashing, the eFuse burn, guiding the replug and reading the console.
- **One-time decryption:** the PC decrypts the per-board envelope with a one-time X25519 key that is held only in memory.

### Code layout

```
apps/esp_installer/
  pyproject.toml, requirements.lock (hash-pinned), README.md
  src/sl_installer/
    __init__.py            __version__
    __main__.py            GUI entry; --tool esptool|espefuse dispatch; --self-test; --screenshots DIR
    config.py              server URL, data dirs, pinned signing key, field defaults loader
    resources/             signing key, empty storage image (gz), icons, SVG pictures, generated field_defaults.json (gitignored)
    core/                  engine, Qt-free (api.py is the contract)
    ui/                    PySide6 screens
  packaging/               PyInstaller spec, build.py, installer.iss, EULA.txt, THIRD-PARTY-NOTICES.txt, icons
  tests/                   core and ui tests
```

**Python compatibility:** all code must run on Python 3.9+. The release ships 3.12. The Mac's own Python is 3.9, but esptool 5.1.0 needs 3.10 or newer: from a source checkout, the bench runs the tools with the Python named by `SL_INSTALLER_TOOL_PYTHON` (or `bench --tool-python`), and refuses to inspect or install unless that Python reports esptool and espefuse 5.1.0. The frozen app ignores the setting. So:
- use `from __future__ import annotations`
- no `match`
- no `X | Y` outside annotations
- no `datetime.UTC`

## 4. Board safety rules (non-negotiable)

1. **Never let a Zone Lite image boot before the HMAC key is burned.** If KEY0 is empty, `nvs_flash_init` burns a random key and the board is lost. So:
   - never use `--after hard-reset` or any reset that boots the app;
   - every esptool/espefuse call uses `--after no-reset`;
   - the only first boot is the person's physical unplug and replug.
2. **New-board write order:**
   1. Erase 0x0–0xFFFF, so no older bootloader can start anything.
   2. Write every range except the bootloader; the last is the blank redo area (rule 10).
   3. Verify on the chip (stub `verify-flash`).
   4. Burn the HMAC key into BLOCK_KEY0 with purpose HMAC_UP.
   5. Check that KEY_PURPOSE_0 is HMAC_UP and that the block is read- and write-protected.
   6. Write the bootloader at 0x0 **last** and verify it.
   7. Ask for the replug.

   A failure before step 4 leaves the board blank and safe to retry. A failure after step 4 leaves a "key burned, not started" board, which can only be **resumed**: same MAC, same deterministic key, rewrite everything, no burn.
3. **Resume:** KEY_PURPOSE_0 must already be HMAC_UP and protected. Never burn again.
4. **Secured boards (Secure Download Mode):**
   - only `get-security-info`, `write-flash` and `erase-region` work, with `--no-stub --flash-size 16MB --flash-mode keep --flash-freq keep`;
   - reads and verifies are impossible;
   - the MAC comes from the USB serial number.

   To change settings, write NVS at 0x11000 (0x6000), the empty storage image at 0x7A0000 (0x800000) and the blank redo area at 0xFA0000 (0x60000), in that order, in one call. The boot log confirms the settings and storage; only a ZKT board's heartbeat in ADD shows the redo area (`reclaim.area` `READY`, `ota_floor` false).
5. **Hardware gate before anything irreversible:** an ESP32-S3 with exactly 16 MB flash and 8 MB embedded PSRAM. A settings change outside Secure Download Mode also refuses a flash size other than 16 MB.
6. **Bundle gate:** before writing, check all of these against the pinned State Life key (`resources/firmware-signing-key-1.pem`):
   - the manifest signature (RSA-PSS SHA-256, salt 32, over canonical JSON);
   - every image's size and SHA-256;
   - the partition table equals the production layout;
   - the app descriptor family and version;
   - for ZKT, the ota_1 image is a qualified predecessor.
7. **Family rule:** refuse to set up a board as the other family (ZKT vs Hikvision) when ADD already knows it. ADD would 500-loop, and the firmware aborts on a family mismatch.
8. **Secrets stay out of logs:** passwords, comm keys, HMAC keys, NVS plaintext, tokens and cookies are never logged or saved. The HMAC key file for espefuse lives in a private temp directory and is overwritten, then deleted, immediately after the burn. If a crash or power cut leaves a work folder behind, the window wipes it the next time it opens: it holds the single-instance lock, and every install runs in a holder of that lock (the window or the bench's `install`), so none can be running. Callers without the lock (`--self-test`, the bench's `watch` and `diagnose`) wipe only folders older than 6 hours.
9. **The same board for every call.** The board ID is the MAC: the banner's, or the USB serial number in Secure Download Mode, whose banner has none.
   - Before every install call the app finds the port again by the USB board ID and uses only a port that has this board's ID. A swapped board is then caught before anything reaches it, and a port that moved is followed.
   - A USB serial number counts only when the whole string is a MAC. A Windows computer that ignores serial numbers reports a location ID such as `5&2B33E3B5&0&12` instead, which can hold 12 hex digits; it never counts, and the cfgmgr32 fallback is asked.
   - A board whose ID neither gives is never written to: between two calls any board could be plugged into the same socket, under the same port name. Checking the board says so first (`BOARD_ID_NOT_READABLE`: another USB socket, or another computer) and offers no new board, resume or settings change; an install that loses the ID stops with the same advice (once the key is locked, inside the advice to finish the setup). Reading such a board (inspection, its console, the replug) still works, except in Secure Download Mode: with no MAC in the banner either, its check stops at `BOARD_ID_UNKNOWN`.
   - Inspection refuses a USB board ID that is not the chip's own (`BOARD_ID_UNKNOWN`).
   - After each esptool erase, write or verify, a MAC in the banner must be the board's. A new or unfinished board must show it: a Secure Download Mode banner, which has none, means another board, because only a first start turns that mode on. A settings change on a finished board accepts that banner. espefuse prints no MAC; its summaries are checked by their eFuse MAC.
10. **The redo area is cleared in every mode, after the settings and the storage.** ZKT 2.7.6+ keeps its last-resort reformat records at 0xFA0000–0xFFFFFF, past the last partition (`firmware/zone_lite/main/storage_repair.c`, `SR_REDO_OFFSET`/`SR_REDO_BYTES`). Their journal (`CLAIMED`) is in NVS and they describe the store, and every mode replaces both. Leftovers would mean an area that stays `FOREIGN` for good, false `REFORMAT_INTERRUPTED` gaps sent to ADD, retained records and a latched OTA floor. So the app writes 0x60000 bytes of 0xFF (sha256 c97fce78…) after NVS and storage, verified on the chip in NEW_BOARD and RESUME, and the same on Hikvision boards, whose 3.1.1 never reads the area. A bundle whose partition table reaches past 0xFA0000 is refused.

Partition layout (`zone-lite-ota-v1`):

| Partition | Offset | Size |
|---|---|---|
| bootloader | 0x0 | — |
| partition table | 0x10000 | — |
| nvs | 0x11000 | 0x6000 |
| otadata | 0x17000 | 0x2000 |
| factory | 0x20000 | — |
| ota_0 | 0x2A0000 | — |
| ota_1 | 0x520000 | — |
| storage | 0x7A0000 | 0x800000 |
| redo area (no partition) | 0xFA0000 | 0x60000 |

What the bundles write:
- **ZKT:** factory and ota_0 get the production release; ota_1 gets the predecessor (2.5.2); otadata selects ota_0; storage is empty SPIFFS (sha256 f39db7c1…).
- **HIK:** factory gets 3.1.1; otadata is blank, so it boots factory; storage is empty SPIFFS.
- **Added by the app in every mode, never in a bundle:** NVS from ADD and the blank redo area.

## 5. Server API (ADD) — `/api/v1/installer/*`

All routes need the admin session cookie. Mutations also need `X-CSRF-Token`. Package creation needs `password` (step-up) in the body. Errors use the shape `{"code": "...", "field": optional, "message": plain words}`, and never echo submitted values.

| Method and path | Result |
|---|---|
| `GET /capabilities` | `{enabled, provisioner_ready, min_app_version, server_time, bundle_count}` |
| `GET /bundles` | `{bundles: [{bundle_id, family, label, version, recommended, release_state, notes, download_bytes, manifest_sha256}]}`. Only bundles whose signature verifies with `ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64`; a bad bundle is skipped, never a 500. A bundle carrying an application image of a release ADD has revoked is left out, and its manifest and files answer 404. |
| `GET /bundles/{id}/manifest` | `{manifest, signature}` |
| `GET /bundles/{id}/files/{name}` | bytes; only names listed in the manifest |
| `GET /devices/{mac}` | `{known, mac, connector_id, zone_id, zone_name, device_id, family, firmware_version, online, last_seen_at, ever_online, public_name, group, status_text, hik_source_epoch, settings_change_allowed, settings_change_reason}` |
| `GET /zones` | `{zones: [{zone_id, zone_name, device_ids, families}]}` |
| `POST /packages` | Body: `{password, request_id, mac, mode, bundle_id, app_version, recipient_public_key, configuration{firmware_family, zone_id, zone_name, device_id, wifi_ssid, wifi_password, zkt_port, zkt_comm_key, zkt_preferred_ip, hik_*}}` → 201 `{request_id, provisioning_manifest, provisioning_ciphertext_b64, hmac_ciphertext_b64}` |
| `POST /events` | `{request_id, mac, kind, detail}` → 204. Appends audit `INSTALLER_<KIND>`. |

`bundle_id` is required for `new_board` and `resume`, and not needed for `change_settings`, which writes no firmware.

**Which boards may get new settings.** Boards set up before this app may use an older security root. Settings built from the fleet root would leave such a board unable to start. So:
- `change_settings` is allowed only when ADD has no connector for the MAC, the connector has never been online, or the audit log holds a successful `INSTALLER_PACKAGE_ISSUED` with mode `new_board` for that MAC (a `resume` burns no key, so its row proves nothing about the board's key);
- `resume` is allowed only for a board ADD does not know or that has never been online;
- otherwise the answer is 409 `SETTINGS_CHANGE_NOT_SUPPORTED`: "This board was set up before this app. Only head office can change its settings."

`GET /devices/{mac}` reports the same rule as `settings_change_allowed` and `settings_change_reason`, and the app stops before the Fix-settings form when it is false.

`POST /packages` errors:
- 409 `FAMILY_MISMATCH`
- 409 `DEVICE_ID_IN_USE` with `suggested_device_id`
- 409 `SETTINGS_CHANGE_NOT_SUPPORTED`
- 409 `BUNDLE_UNAVAILABLE` or `BUNDLE_FAMILY_MISMATCH` (field `bundle_id`)
- 426 `APP_UPDATE_REQUIRED` below `installer_min_app_version`
- 422 `INVALID_FIELD` with `field`
- 502 `PROVISIONER_UNAVAILABLE`
- 429 `RATE_LIMITED` with `Retry-After` in seconds

Every installer route can also answer `AUTH_REQUIRED`, `SESSION_EXPIRED`, `CSRF_INVALID`, `PASSWORD_REJECTED`, 409 `INSTALLER_DISABLED` (kill switch `ADD_INSTALLER_ENABLED=false`), `BUNDLE_NOT_FOUND`, `FILE_NOT_FOUND`, `NOT_FOUND` or `INVALID_REQUEST`.

It appends the audit event `INSTALLER_PACKAGE_ISSUED`, which contains no secrets.

Provisioner `POST /internal/v2/nvs-packages` takes `{request_id, target_mac, recipient_public_key, configuration}` and returns 201 with the three envelope fields. It needs no factory bundle and writes no artifact files. Failures return 422 `{code, field}` or 500 `{code: "BUILD_FAILED"}`.

Two more server changes:
- `/device/v2/onboard` returns 409 instead of 500 on `FIRMWARE_FAMILY_MISMATCH`.
- Compose mounts `${ADD_INSTALLER_BUNDLE_STORE_HOST_PATH}` read-only at `/installer-bundles` in add-api. The production default is `C:\Users\Public\Documents\StateLife\AttendanceDeviceDashboard\installer-bundles`.

## 6. Installer bundle format v1

Store layout: `<store>/<bundle_id>/manifest.json`, `manifest.sig` (base64 RSA-PSS by vault key-1), and the image files.

```json
{
  "schema": "zone-lite-installer-bundle-v1",
  "bundle_id": "zkt-2.7.8-b8d0ca7e",
  "firmware_family": "zkt",
  "label": "ZKT 2.7.8",
  "version": "2.7.8",
  "release_state": "AVAILABLE",
  "recommended": true,
  "git_sha": "<40 hex>",
  "created_at": "2026-11-01T00:00:00Z",
  "partition_layout": "zone-lite-ota-v1",
  "notes": "Production release for ZKTeco terminals.",
  "images": [
    {"role": "bootloader", "name": "bootloader-signed.bin", "offset": 0, "size": 0, "sha256": "..."},
    {"role": "partition-table", "name": "partition-table.bin", "offset": 65536, "size": 3072, "sha256": "..."},
    {"role": "otadata", "name": "ota-data.bin", "offset": 94208, "size": 8192, "sha256": "..."},
    {"role": "factory", "name": "zone-lite-signed.bin", "offset": 131072, "size": 0, "sha256": "...", "app_project": "zone_lite", "app_version": "2.7.8", "app_digest": "..."},
    {"role": "ota_0", "name": "zone-lite-signed.bin", "offset": 2752512, "size": 0, "sha256": "..."},
    {"role": "ota_1", "name": "rollback-predecessor.bin", "offset": 5373952, "size": 0, "sha256": "...", "app_version": "2.5.2", "app_digest": "..."},
    {"role": "storage", "name": "storage-empty.bin", "offset": 7995392, "size": 8388608, "sha256": "f39db7c1..."}
  ]
}
```

- Bundle IDs: `zkt-<version>-<app digest 8>` and `hikvision-<version>-<source sha 7>-<app digest 8>`.
- Release states: `AVAILABLE` (ZKT production) and `FIELD_BASELINE` (HIK 3.1.1 exact source).
- The app carries a gzip copy of the 8 MiB empty storage image and uses it when the manifest hash matches, so remote sites do not download 8 MB.
- Published by `firmware-key-bootstrap.yml` with confirmation `PUBLISH INSTALLER BUNDLE`:
  - **ZKT:** newest AVAILABLE release plus predecessor, with the existing checks.
  - **HIK:** builds `hikvision_source_ref` (default dc7d9c3) with `ZONE_LITE_FAMILY=hikvision` and an empty storage image, then signs with `-FirmwareFamily hikvision`.
  - **Both:** written atomically into the store and never overwritten.

## 7. App flows and screens

Every screen has:
- **Top bar:** State Life logo, app name, and chips for "Internet", "Head office server" and "Signed in as …" with Sign out.
- **Title and explanation:** a title that says what we are doing, and one plain sentence.
- **Buttons:** Next/Back at the bottom right, and Help, which opens pictures for that screen.
- **Errors:** an Advice card with a picture.

Writing rules:
- short sentences; second person;
- no jargon (no eFuse, NVS, flash, partition, COM port);
- say "board" for the ESP, and "the attendance machine" for the terminal.

Screens:

1. **Sign in.** Username and password with a show/hide eye.
   - After 3 wrong tries in 10 minutes, the app pauses sign-in for 10 minutes and explains why. ADD locks the account for 15 minutes after 5 tries, which would also lock the dashboard.
   - The pause is timed on the monotonic clock, so putting the computer's clock right cannot stretch it. The end saved for a restart is dropped if it lies more than 15 minutes ahead (ADD's own lock, the longest pause).
   - The password is kept in memory only, for step-up calls and silent re-login on 401.
2. **Home.** "Set up a new board", "Check or fix a board", and "How to connect a board".
3. **Machine type.** Two big cards: ZKTeco and Hikvision.
4. **Details.**
   - **ZONE ID:** upper-cased and checked live (`^[A-Za-z0-9][A-Za-z0-9._-]*$`, at most 64 bytes). Suggestions come from ADD zones.
   - **ZONE NAME:** auto-filled from ZONE ID, with "Change" to edit it. A preview card shows the public name and the Life/Health group.
   - **Device ID:** next free number in that zone.
   - **Firmware:** dropdown with the recommended bundle preselected.
   - **Advanced (collapsed):** Wi-Fi name and password (visible with the eye), ZKT port, comm key, preferred IP, device ID.
   - **HIK only:** machine address (required, LAN IPv4), password (required), model (sets the profile) and the machine's serial number (required: the firmware will not talk to a terminal without it). Advanced holds username (admin), port (80) and connection (HTTP digest only). HTTPS is not offered and is refused in the field defaults and the bench: without the machine's own certificate (`hik_ca_pem`), which the app cannot add, the board checks a LAN terminal against the public certificate authorities and never connects. The source epoch is automatic: `<zone_id lower>-<yyyymmdd>`, or the epoch ADD already has for the MAC.
5. **Connect the board.** A live USB watcher with a picture of the cable and the socket marked USB.
   - **No board for 15 s:** cable, port and socket tips, then setup mode by hand (BOOT and RST) before "may be faulty": a maker's demo that offers no serial port (a USB keyboard or drive) lists nothing until the board is in setup mode.
   - **Bridge chip only:** "use the socket marked USB".
   - **Espressif chip in another USB mode** (303A but not 1001, e.g. a new board's demo at 303A:4001): "Put the board into setup mode" (hold BOOT, press and release RST, let go of BOOT), then Board found and Next. Still there after 5 s: unplug, and hold BOOT while plugging back in; after 3 tries, a help file for head office, never "faulty". The advice stays up through a restart gap of up to 3 s instead of flashing away. It comes before the bridge advice: only the socket marked USB shows an Espressif chip. Accepted: any other Espressif USB serial gadget on the computer looks the same, and a 1001 port next to such a gadget is the board (its ID is shown; writes stay bound to it).
   - **Several boards:** "connect only one".
   - **Slow restart loop** (the port comes back about every 10 s or more slowly, with no output): "This board is faulty", put it aside; ADD gets `BOARD_FAULTY`.
   - **Fast restart loop:** "The board keeps restarting": a power or cable problem first (short cable, another socket), then Check again. Never called faulty.
6. **Checking the board.** Inspect the board, then look it up in ADD. The result card shows one of:
   - new board → Next;
   - unfinished setup → Resume, unless ADD has seen the board online (then ADD's reason and "Use another board");
   - already set up → Fix-settings flow;
   - wrong board or secured by someone else → stop.
   - **USB gives no board ID** (rule 9): "This computer cannot read the board ID", with Try again. Try again, like every retry of the check, reads the board where USB lists it now, so a cable moved to another socket is followed.
   - **Auto download mode fails:** show manual steps: hold BOOT, tap RST, release BOOT, Try again.
   - **Board back in its own software** (no 303A:1001 port but another 303A one, e.g. replugged after Board found): the same manual steps and Try again, never "unplugged" for a board that is plugged in.
7. **Confirm.** A summary of the board, place and firmware, plus "This prepares the board for State Life for good" and an "I understand" checkbox.
8. **Installing.** The steps from `steps_for(mode)` with progress, and a "Keep the cable plugged in" banner. Cancel works only before the key step.
9. **First start.**
   1. Animated "Unplug the cable"; wait for the port to go.
   2. "Wait 3 seconds", then "Plug it back in".
   3. Watch the console for up to 120 s. The console's USB copy drops what the board prints before the app opens the port, so the start line (`Zone Lite starting zone=… device_id=…`, under 2 s after power-on) can be gone when a slow computer lists the port again after the replug. Any later line of the program also shows the start: storage, Wi-Fi, the setup hotspot, head office or the attendance machine. ZKT 2.7.8 and 2.8.x, the 2.5.2 predecessor in ota_1, and HIK 3.1.1 print these only after loading and checking the board's settings. The storage wait and the crash rules count from the first such line. Without the start line its time is unknown, so then any crash seen in the watch counts.
   4. Report the result: zone, device ID and version seen, or the fault advice. Without the start line no device is claimed, and no zone unless the settings line (`Loaded per-device provisioning for zone=…`) came; with neither, the screen says "It started and is working", and the record and ADD get none. "The board is silent" (may be faulty) only when no line at all arrived; other lines without a start give "The app did not see the board start".
   5. **Restart check, in every mode.** A good first start is not enough (2 Nov 2026: two new boards started well once, then had no settings at every later start). Ask for one more replug ("Unplug the board again, wait 3 seconds, and plug it back in. The app checks that the board keeps its settings.") and watch that start the same way. It passes only when the board names its settings again for the same zone (the settings line, or the start line with zone and device) with no settings error. Missing or unreadable settings give `SETTINGS_LOST_ON_RESTART`, and so does a zone or device other than the first start showed (writing the settings again would last only until the next restart): do not use the board, label it DO NOT USE with its board ID, keep it aside, save a help file at once. Running without either line gives `SETTINGS_NOT_SEEN` (Try again; if it happens again, do not install the board); everything else, the first start's advice. Until it passes, the screen says the setup is not finished, and closing the app asks first. Only then do the screen, Done, the setup record and ADD (`RESTART_OK`, else `RESTART_FAILED` with `settings_kept`) say it started correctly and kept its settings; the record calls a start that passed only "Passed". Both lines come about 1.1 s after power-on, once: a computer that opens the port later can never pass this check, so the replug-to-port time on Windows is a release check (user guide, release runbook).
10. **Done.** Reached only after the restart check passed. "The board is ready". Shows what was set and what to do at the site:
    - hotspot `SLIC ATTENDANCE-XXXX`, where XXXX is the last 2 bytes of MAC+1;
    - the hotspot password;
    - `http://192.168.254.1` and the portal steps;
    - "Save a record" (text file), "Set up another board".
11. **Check a board.** Board ID, security state, the ADD record (online/offline in plain words), live console lines in plain words, a "What is the light doing?" guide, and "Fix settings". When USB gives no board ID, Fix settings is not offered and the screen says why (rule 9); Check again reads the board where it is now.
12. **Fix settings.** Offered only when ADD answered and `settings_change_allowed` is true; otherwise the screen shows `settings_change_reason` (or "head office could not be reached") and stops. ADD supplies only the ZONE ID, ZONE NAME and device number: the person must re-enter the comm key, machine address and port (or the Hikvision address, password, model and serial), because the board's copy cannot be read and a default would silently replace it. If `ever_online`, a strong warning ("unsent punches on the board will be lost") and the person must type the ZONE ID to confirm. Then mode `CHANGE_SETTINGS` (no bundle), then First start.
13. **Light guide.** Pick the blink pattern, then the colour. The meanings come from `firmware/zone_lite/main/led_status.c`.

Faulty board signature: USB appears for about 10 s, then vanishes, in a loop, with no console output and no LED. The verdict is "faulty — put it aside". ADD gets a `BOARD_FAULTY` event.

## 8. Engine modules (core)

| Module | What it does |
|---|---|
| `add_client.py` | httpx client: login/logout, session, CSRF, silent re-login, client-side login throttle, typed errors. TLS uses `truststore` when available, otherwise certifi. Server `https://autoattn.slichealth.com`, overridable only with `--server` for tests. |
| `connectivity.py` | Internet check: `http://www.msftconnecttest.com/connecttest.txt` → "Microsoft Connect Test". Server check: `GET /health/live`. Runs every 15 s. |
| `usb.py` | pyserial enumeration; VID/PID classes (303A:1001 native; 10C4, 1A86, 0403 bridges; other 303A PIDs: not in setup mode, never read or written; each presence change is logged with the ports' USB IDs, for help files); MAC from the USB serial when it is a whole MAC, with a Windows cfgmgr32 parent-device fallback; presence timeline and restart-loop detection. A lone board without an ID is found only for reading (console, replug). |
| `esptool_runner.py` | Runs bundled esptool/espefuse as child processes (`<exe> --tool esptool …` when frozen, `python -m esptool` in dev). Parses progress, applies timeouts and cancellation, captures logs with redaction, maps known failures to codes. |
| `board.py` | `get-security-info` (works in SDM); for non-SDM boards also `chip-id`, `flash-id`, and `espefuse summary --format json`; classifies `SecurityState` and `HardwareVerdict`. Reads espefuse 5 JSON fields `value`/`readable`/`writeable` (there is no `raw_value`). |
| `bundles.py` | Catalogue, download with progress into `%LOCALAPPDATA%\StateLife\DeviceInstaller\bundles`, full verification on every use. |
| `envelope.py` | Vendored decrypt from `firmware/zone_lite/tools/provisioning_envelope.py`, with all its checks. |
| `flashplan.py` / `installer.py` | Plans for NEW_BOARD, RESUME and CHANGE_SETTINGS that follow §4, run step by step with progress; ADD events at each milestone. |
| `console.py` | Opens the port without toggling DTR/RTS (115200), turns lines into `ConsoleEvent`s, verifies the first boot and the restart check, and handles replug detection. Keeps each watched start's lines, redacted like the log, in one small file per install (`logs/console-<board>-<request>.log`, at most 256 KB, the newest 20), which the help file carries. |
| `diagnosis.py` | Rules that turn board, presence, ADD record and console events into an ordered list of `Advice`. |
| `texts.py` | Every Advice text, in one place. |
| `records.py` | Install record text files (both watched starts) and the help ZIP (redacted logs and console logs). |
| `fakes.py` | Scripted `Services` for UI tests and screenshots. |
| `factory.py` | Builds the real `Services`. |

## 9. Packaging and release

- **PyInstaller onedir:** windowed `State Life Device Installer.exe`, with esptool 5.1.0 inside, called with `--tool`. Only the Qt modules and plugins the app uses (Widgets, Svg, qwindows, imageformats svg/ico/png). Version resource, and an icon generated from `apps/add_frontend/public/state-life-logo.png`.
- **Inno Setup 6** (`ISCC.exe` on windows-2022):
  - per-user install (`PrivilegesRequired=lowest`, `{localappdata}\Programs\State Life Device Installer`), x64, MinVersion 10.0.17763;
  - EULA page; branded wizard images; Start menu and optional desktop shortcut; launch after install;
  - an uninstaller that removes the app and the bundle cache, and asks before deleting saved records and logs;
  - `AppMutex=StateLifeDeviceInstaller`, the named mutex the open window holds: Setup and the uninstaller ask for the app to be closed and wait, so no file is deleted under a running setup. `CloseApplications=yes` stays, never forced: a forced close could stop esptool or espefuse halfway.
- **Workflow** `.github/workflows/esp-installer-release.yml` (windows-2022):
  - tests, build and installer, then smoke tests: silent install, `--self-test`, `--screenshots`, silent uninstall, then a check that the files are gone;
  - artifacts: Setup exe, SHA256SUMS and screenshots;
  - a `workflow_dispatch` on main with `environment: firmware-signing` injects `field_defaults.json` from secret `ESP_INSTALLER_FIELD_DEFAULTS_JSON` plus `ZONE_LITE_SETUP_PASSWORD`;
  - PR builds are test builds, with empty secret defaults and a "TEST BUILD" banner.
- `scripts/check_repository_contract.py` adds app `esp_installer` and the new workflow.

## 10. Testing

- **Core:** pytest with a fake esptool (scripted outputs), fake serial, and an `httpx.MockTransport` ADD. Covers:
  - the exact command order and flags of each plan;
  - the redo area, pinned to `SR_REDO_OFFSET` and `SR_REDO_BYTES` in `storage_repair.c` and cleared after the settings and the storage in every mode;
  - no hard reset ever;
  - classification from espefuse JSON samples;
  - console parsing of firmware log lines;
  - restart-loop detection;
  - redaction.
- **UI:** pytest-qt offscreen: every page renders with fake services; navigation; `--screenshots` produces PNGs.
- **Backend:** pytest for installer_api and provisioner v2.
- **PowerShell:** tests for bundle publishing in windows-publication.
- **Windows CI:** install, self-test, screenshots, uninstall.
- **Bench:** a real blank board, on the Mac first, then a Windows 11 PC with the built installer. The console never shows the redo area, so after a new board and after a settings change in Secure Download Mode the bench checks a ZKT board's heartbeat in ADD: `storage.repair.reclaim.area` `READY`, no `area_foreign_sectors`, `reclaim.ota_floor` false.

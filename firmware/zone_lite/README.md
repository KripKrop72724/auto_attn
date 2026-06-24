# Zone Lite ESP32 Firmware

Firmware for an ESP32-S3 Zone Lite gateway.

Current milestone:

- Connects to Wi-Fi.
- Scans the DHCP subnet for the first host accepting ZKT TCP port `4370`.
- Confirms the host by running ZKT `CMD_CONNECT` and Comm Key auth.
- Reads basic identity/time/count information from the selected device.
- Repeats discovery so DHCP IP changes are handled without reflashing.
- Optionally recovers a stuck ZKT device through OS telnet reboot.
- Optionally performs one daily preventive ZKT telnet reboot during a 03:00
  local maintenance window.
- Shows production status on the ESP32-S3 onboard RGB LED.
- Can POST a full current-month ZKT truth snapshot to ORDS reconcile so Oracle
  raw rows converge back to the ZKT machine.

Create local secrets before building:

```bash
cp main/zone_lite_config.example.h main/zone_lite_config.h
```

`main/zone_lite_config.h` is ignored by git.

ZKT recovery:

Some ZKT devices can enter a state where TCP `4370` accepts connections but the
ZKT application service no longer responds to protocol commands. For that stuck
state, recovery must be done through the device OS telnet service.

Enable `ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED` only after confirming the device
telnet account. The firmware logs into telnet, confirms a shell with `id`, sends
`sync`, sends `ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND`, waits
`ZONE_LITE_ZKT_REBOOT_WAIT_MS`, and then resumes normal discovery and capture.
The recovery path is cooldown protected to avoid reboot loops.

Daily preventive reboot:

Enable `ZONE_LITE_DAILY_ZKT_REBOOT_ENABLED` only after the same telnet account is
confirmed. By default the maintenance window is 03:00-03:30 Pakistan time
(`ZONE_LITE_DAILY_ZKT_REBOOT_UTC_OFFSET_MINUTES=300`). When a live ZKT session is
active, the firmware first performs a final `LIVE_POLL` dump and ORDS drain
attempt, then sends the telnet reboot command. If no live session is active, it
uses the preferred or last successfully authenticated ZKT IP. A successful daily
reboot is recorded in RAM for the current local day; failed attempts are retried
no faster than `ZONE_LITE_DAILY_ZKT_REBOOT_RETRY_DELAY_MS` while the window is
still open.

LED status:

The ESP32-S3 DevKitC-1 onboard RGB LED defaults to GPIO `48`. The LED is
operator-facing and always shows the highest-priority current state or latched
fault.

- White pulse: booting or initializing local storage.
- Blue blink: Wi-Fi connecting or reconnecting.
- Cyan pulse/solid: discovering ZKT, then ZKT authenticated.
- Purple pulse: startup dump, reconcile, or ORDS drain in progress.
- Green solid: healthy live capture registered.
- Green flash: live punch captured and queued.
- Amber heartbeat: durable outbox backlog remains.
- Orange blink: ORDS/Oracle send failure.
- Yellow blink: ZKT protocol/auth failure before recovery.
- Amber/yellow warning blink: Oracle raw rows were repaired from ZKT truth.
- Red fast blink: telnet reboot in progress for recovery or scheduled
  maintenance.
- Red solid: fatal local failure.
- Magenta blink: identity row blocked locally.

Build:

```bash
. ~/esp/esp-idf/export.sh
idf.py -DIDF_TARGET=esp32s3 -DPROJECT_VER=0.1.0 build
```

Flash:

```bash
idf.py -p /dev/cu.usbmodem1234561 flash
```

If flashing reports `No serial data received`, put the ESP32-S3 in ROM
download mode manually:

1. Hold `BOOT`.
2. Tap/release `RESET` or `EN`.
3. Keep holding `BOOT` for about two seconds.
4. Release `BOOT`.
5. Run the flash command again.

Some DevKitC-1 boards also work by holding `BOOT` during the flash command and
releasing it once esptool connects.

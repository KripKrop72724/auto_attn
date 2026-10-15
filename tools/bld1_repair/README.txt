BLD1 ESP LOGIN REPAIR — Windows 10/11 x64

VERSION 1.0.7: attendance recovery, automatic IP recovery and permanent writes.
History checkpoints now compare event serial, major/minor code, employee number
and timestamp, excluding mutable display names and picture URLs. Serial searches
overlap one predecessor to handle inclusive and exclusive device behavior.
The original BLD1 checkpoint can migrate only when its exact saved raw hash and
the current record's immutable facts match independently verified ADD evidence.
The old checkpoint is backed up before migration. Changed facts remain blocked.
If retained-history verification blocks polling, the exact BLD1 image opens an
independent, serial-authenticated read-only live stream. Incoming events enter
the same durable queue and ADD delivery path. The original history checkpoint is
preserved; history verification remains pending. ADD still validates event codes,
identity and time before releasing attendance. A connected stream alone does not
prove that a punch reached ADD: make one successful punch and have IT verify it.
Capture and reconnection retries have no automatic expiry or trial time limit.
When identity checks repeatedly fail, the ESP sends a read-only Hikvision SADP
inquiry on its active local subnet. A fresh serial-matching reply must pass
authenticated ISAPI serial verification using the saved credentials and port.
Only then is the new IP committed to encrypted NVS and shown in ADD. A different
terminal, stale reply or failed verification never changes the active address.
The ESP and terminal must remain on the same local subnet, with UDP multicast
239.255.255.250:37020 allowed. This does not cross routers/VLANs or fix a terminal
that is powered off, disconnected, or has changed credentials.
No automatic expiry. ADD must separately approve the permanent device grant.
The grant remains revocable by an authorized administrator.
The terminal's true unverified model profile is retained; this is NOT qualification.
No terminal user is created, edited or deleted by running this EXE. Administrator
role changes are excluded. Normal serial/fingerprint/readback guards remain.

Utility version 1.0.7 replaces earlier EXEs. This version skips unsupported
read-mac in Secure Download Mode and contains SDK errors without popup crashes.
It reports the specific failed configuration checks. A mismatch refuses the
username change without aborting startup on the verified, provisioned ESP.
The onsite 1.0.2 diagnostic confirmed that only the stored username differed.
This revision sets that single field to admin after all identity guards pass;
it does not require the incorrect old username to equal Admin.
Check that the window title says BLD1 ESP Repair 1.0.7.

For ONE device only: AC:27:6E:A3:0A:58 / LF-ZONE-BLD1-01.
Terminal: DS-K1T341AMF, serial G04600476; its verified IP can now change.

1. Connect only this ESP to the Windows PC using a USB DATA cable, in the ESP's
   socket labelled USB, NOT its COM socket. Unplug the other socket.
2. Close other programs using the ESP's COM port.
3. Run BLD1-ESP-Repair.exe and click Repair this ESP.
4. Keep it connected. Follow BOOT/RESET instructions only if displayed.
5. Wait for the final result and send a screenshot to IT.

No Python, terminal commands, ADD login, or administrative password is required.
Native Espressif USB is required for identification in Secure Download Mode.
This EXE is not Authenticode-signed; verify the supplied
SHA256 with IT. Do not disable antivirus or other security protection.

RESULTS
REPAIR VERIFIED: exact ESP reported persisted username admin and a successful
Hikvision poll. IT must still confirm ADD attendance ingestion/delivery.
USERNAME REPAIR VERIFIED — SITE CHECK PENDING: local correction succeeded but
the terminal/network/password still needs checking. This is NOT site completion.
Error or missing boot evidence: do not report success; contact IT. Same-device
retries are idempotent. Keep the same EXE if a power/USB interruption occurs.
CONFIGURATION CHECK REQUIRED: no username change was made. Send the screenshot
including "Configuration checks that do NOT match" to IT. Do not keep retrying.
This result diagnoses the mismatch; it does NOT mean terminal login is repaired.

PRESERVED: encrypted NVS (except hik_user corrected to admin and hik_host updated
only after authenticated rediscovery), site Wi-Fi,
terminal password and serial, connector identity, eFuses/security keys, bootloader,
partition table and attendance storage. The signed factory application and OTA
boot selector are replaced. Previous OTA application bytes are left untouched.
No HMAC key, signing private key, ADD administrator secret, or terminal password
is embedded in the utility. Secure Boot is not disabled or re-burned.

The firmware is a targeted derivative of HIK 3.1.1: 3.1.1-bld1fix7. It is not a
general production release and must not be used for other ESPs. The device checks
its hardware MAC, provisioning flag, family, zone, device ID and expected serial
before touching the single username key. Mismatched identities cannot be
modified. On the verified physical ESP, existing provisioned configuration can
still run unchanged; an unverified physical target stays inert.

Build and packaged Windows smoke tests do not replace a physical site test.
This repair is not claimed to have been physically tested until the onsite run.

Sources: supplied BLD1-ESP-Repair-source.zip. Bundled Espressif esptool 5.1.0 is
GPL-2.0-or-later (https://github.com/espressif/esptool/tree/v5.1.0).
The utility bundles Python, PyInstaller runtime and pyserial; no install is needed.

# Lahore USB Wi-Fi recovery

Status: the bounded `SLIC-Lahore2-WiFi-Repair-v1.0.exe` is qualified against
the actual fleet-signed candidate on the secured spare. The earlier
`SLIC-USB-Security-Check-v1.3.exe` remains read-only.

Measured evidence is in
`docs/evidence/2026-10-23-usb-wifi-rescue-locked-spare.json`. Source commit
`701f2de9c3a64e8bdfda7b1f8d529c25c75dd08e` passed all eight exact-source ADD
CI gates and protected signing run `39825492611`. The Windows EXE bundles
esptool 5.1.0 and the signed rescue; it needs no Python installation or download.

The original Lahore ESP has not been flashed here. Actual setup AP visibility
and access on the original remain field checks: the GUI captures the connected
device's portal-enable message and a full Windows Wi-Fi scan. A nearby SLIC
network alone cannot produce a positive AP result. The lab qualification does
not claim independent radio visibility or factory acceptance.

The original ESP `ac:27:6e:a3:16:d4` reports Secure Download Mode, Secure Boot
enabled, no flash encryption, HMAC_UP in key block 0, three secure-boot digests,
and permanently disabled JTAG (security flags `0xc5`). ROM flash reads and
ordinary host MD5 verification are unavailable. The ordinary provisioning
installer cannot preserve this device by backing up and replacing its NVS.

ADD's last observed active application is `ota_1`, zone-lite-2.6.15, image digest
`832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6`.
The repair must check this on the device before changing settings. A different
application, partition layout, security profile, zone identity or MAC must fail
closed. The source accepts the named Lahore device and the exact spare only;
the unsigned build accepts only the spare.

## Intended bounded operation

1. The installer identifies the original native USB parent serial and its exact
   security profile. No guessed COM-port identity is sufficient.
2. It writes the same verified, fleet-signed one-shot rescue to factory
   `0x20000` and inactive `ota_0` `0x2a0000`. Each image stays within its 2.5 MiB
   slot. Active `ota_1`, NVS, storage, bootloader, partition table and eFuses are
   not host write targets.
3. Only after both application writes complete, it invalidates the two OTA
   metadata sectors separately to boot factory. Both fallback application slots
   now contain the bounded rescue. A failed application write must stop before
   the OTA selection changes.
4. On the ESP, rescue validates the retained original application's signature
   and exact image digest. It hashes the complete 8 MiB attendance storage.
5. It selects that verified original as VALID in both OTA metadata sectors,
   with on-device readback, before attempting any NVS changes.
6. It requires the existing HMAC key, initializes encrypted NVS without erase,
   checks the provisioned zone/device, and hashes every other logical NVS value.
   Only an empty/missing `zone_cfg/wifi_ssid` is repaired: password first, SSID
   `RE7` last, commit, then readback. Nonempty operator settings remain intact.
7. It rechecks all other NVS values and the complete attendance storage, emits
   a result without secret values, and restarts the original application.

The wrong Wi-Fi credentials are intentional: existing 2.6.15 disconnect
handling should enter setup AP mode after failed association. The separate
empty-SSID timer fix prevents this failure on future firmware builds.

## Build and signing boundaries

`scripts/build_usb_wifi_rescue.py` builds distinct `zone_wifi_rescue` and
synthetic `wifi_rescue_seed` roles. It requires an explicit password through
the environment, writes the generated header outside the repository, and
redacts compiler output. It never signs or flashes. Public CI uses only
`HIL-Recovery-Only`; that password is not the requested deployment setting.

The existing protected bootstrap workflow has a separate main-only HIL path,
requiring every exact-main ADD CI gate. It reuses the existing three-key vault,
refuses replacement or new keys, and refuses the unsigned test variant before
decrypting any key. Outputs remain HIL-only and are not factory releases.
This does not satisfy or bypass the quarantined factory-bundle promotion gate.

## Required physical evidence

The unsigned spare experiment uses MAC `14:c1:9f:46:ae:1c`, public synthetic
settings of all NVS value types, an 8 MiB raw attendance sentinel, and a retained
predecessor in `ota_1`. Its runner verifies preservation, return to the original
application, unchanged security, and full restoration of the separately
verified original 16 MiB spare backup. These tests do not prove operation in
Secure Download Mode or encrypted NVS.

Before issuing a repair EXE, test the actual signed candidate with encrypted
NVS and the original security profile on the spare. Enrolling that spare's
HMAC/Secure Boot/Secure Download fuses is irreversible and needs explicit human
authorization. No eFuse burn belongs in the customer installer. Test failure,
power interruption and successful return to the original application; retain
raw evidence. Check independent AP visibility and setup access on the original
in the field; the requested USB EXE is the available field inspection method.
Never represent an unsigned spare test as passing secured-device qualification.

## Completed secured-spare qualification

The human explicitly approved irreversible spare-only security enrollment.
The spare was enrolled with the existing ADD keys before the repair tests;
the customer installer contains no enrollment operation. Its security profile
remained `0xc5`, HMAC block 0, three secure-boot digests, flash encryption off.

The actual signed rescue repaired the empty SSID, preserved every other
logical NVS entry and the full 8 MiB synthetic attendance fixture, and returned
to the retained signed application. Repeating the repair made no settings
change. A physical USB power cycle between the two OTA selection-sector
writes booted the backup rescue at `0x2a0000`, preserved the same data and
returned to the retained application at `0x520000`. NVS write failures are
covered by compiled C tests; this is not a claim of physical interruption at
every possible NVS write boundary. Raw transcripts and hashes are retained.

An earlier signed test predecessor overflowed its default main-task stack
while creating encrypted fixtures. That failed run was retained separately;
the corrected 16 KiB test predecessor and actual rebuilt signed pair passed
the complete qualification. Signing validation now rejects insufficient stacks.

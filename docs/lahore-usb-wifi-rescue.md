# Lahore USB Wi-Fi recovery experiment

Status: experimental source and spare tests; **no qualified repair executable**.
The delivered `SLIC-USB-Security-Check-v1.3.exe` remains read-only.

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
raw evidence. Independent AP visibility and setup access must also be checked.
Never represent an unsigned spare test as passing that qualification.

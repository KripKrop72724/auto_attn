# Zone Lite 2.8.1: the rollback-slot storage gate

[2.8.2](zone-lite-2.8.2-release.md) is 2.8.1 with the 49-byte attendance
records of MB40-VL/ID terminals (LHRCENTRAL); its gate is 2.8.1's, with 2.8.1
added to the predecessor list.

2.8.1 is 2.8.0 (secure-pull terminals, [2.8.0 release](zone-lite-2.8.0-release.md))
with one change: the direct role's storage gate also accepts an other OTA
slot that the bootloader can never boot. A rollback or an interrupted
download then no longer leaves the image it returns to degraded. Everything
else is 2.8.0's.

## Why

Every direct-role image (2.6.1 to 2.8.0) checks on every boot, in
`storage_upgrade_init()`, that the other OTA slot holds an exact older
predecessor from its compiled list: version and application digest,
`ug_direct_predecessor_matches()`. Otherwise storage refuses with
`STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED`: the LED shows `LOCAL_FAILURE`
(orange), `storage.upgrade_ready` is false, the persistence probe never runs
and ADD shows the ESP DEGRADED. An unconfirmed image in that state can never
confirm (`SX_H_STORAGE_UPGRADE` blocks boot health).

No image lists a newer one, and every download writes the other slot. So:

- **An interrupted download** leaves part of the new image there. From its
  next boot the running image refuses storage, until an install succeeds.
- **A rollback** (the new image never confirmed) leaves the new image there,
  `INVALID` or `ABORTED`. The image the bootloader returns to refuses it on
  every boot.

MULTAN-02 showed it on 2.6.15 (21 October 2026): a 2.7.x download wrote its
other slot, and every later 2.6.15 boot reported
`STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED` and DEGRADED until a 2.7.x
install succeeded. ADD labels that case `OTA_ROLLBACK_SLOT_RESIDUE`
([device health](add-device-health.md#interrupted-ota-attempts-21-october-2026-multan-02)).
2.7.8's list ends at 2.7.7, so a 2.8.0 that rolls back leaves 2.7.8 the same
way. ADD (with 2.8.1) also labels a stuck 2.7.8 (`b8d0ca7e...`, LED source
`zone_lite.c:3573`) and a stuck 2.8.0 (`21ac81b2...`, `zone_lite.c:3875`):
the label is on `ESP_LOCAL_FAILURE` (these images report durability
UNKNOWN, not DEGRADED, without an I/O error), the tier stays DEGRADED, and
the remedy reads "offer 2.8.1". It needs ADD's own record that it installed
the running image, so a board set up by USB shows a plain
`ESP_LOCAL_FAILURE`; ADD still offers it 2.8.1.

## The rule

2.8.1 judges the other slot by its bytes first, as before, then by its
otadata entry:

| Other slot | Running image | 2.6.1-2.8.0 | 2.8.1 | `rollback_slot` |
|---|---|---|---|---|
| An exact listed predecessor | any state | ready | ready | `PREDECESSOR` |
| Anything else, no otadata entry | `VALID` | refused | ready | `NONE_NO_ENTRY` |
| Anything else, `INVALID` | `VALID` | refused | ready | `NONE_INVALID` |
| Anything else, `ABORTED` | `VALID` | refused | ready | `NONE_ABORTED` |
| Anything else, `PENDING_VERIFY` | `VALID` | refused | ready | `NONE_PENDING_VERIFY` |
| Anything else, `NEW`, `VALID`, `UNDEFINED` or unreadable | `VALID` | refused | refused | `UNQUALIFIED` |
| Anything else | not `VALID` | refused | refused | `UNQUALIFIED` |

ESP-IDF 5.5.3 reports an unreadable otadata as no entry for both slots, so
the running image is then not `VALID` and the boot is refused as on 2.8.0.

- Everything 2.8.0 accepted, 2.8.1 accepts. Its list is 2.8.0's plus 2.8.0
  (application `21ac81b292b463cf7fa1bf0d6ce364a49493951836ad962a19dcae049f40ed76`,
  published HIL_ONLY on 30 October): 2.4.12, 2.5.2, 2.6.6-2.6.10,
  2.6.12-2.6.15, 2.7.2-2.7.8 (2.7.8:
  `b8d0ca7e20d28d4114cff56fc69676e681a37bf9f7e831a243a65f9a1a972a3e`) and
  2.8.0.
- An unconfirmed image keeps the old rule. Until it is `VALID` it can still
  roll back to the other slot, so that slot must hold a qualified image.
- Only the direct role changes. Its earlier refusals (another version,
  Secure Boot off, an image outside `ota_0`/`ota_1`) are unchanged.

The compiled marker is 172 characters; ADD's heartbeat field keeps 256:

```
ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10,2.6.12,2.6.13,2.6.14,2.6.15,2.7.2,2.7.3,2.7.4,2.7.5,2.7.6,2.7.7,2.7.8,2.8.0
```

## otadata facts

ESP-IDF 5.5.3 with `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`:

- otadata has two 4 KiB sectors (`0x17000`, `0x18000`) with one entry each:
  `ota_seq`, state and CRC. An entry selects slot `(ota_seq - 1) % 2`.
- Before it selects, the bootloader marks every `PENDING_VERIFY` entry
  `ABORTED` (an image that reset before it confirmed). It then boots the
  entry with the highest `ota_seq` whose CRC is good and whose state is not
  `INVALID` or `ABORTED`, and turns a selected `NEW` entry into
  `PENDING_VERIFY`. With no such entry it boots the factory image.
- `esp_ota_begin()` erases the inactive entry before the first write, so an
  interrupted download leaves the other slot with no entry.
  `esp_https_ota_finish()` verifies the image and selects its slot as `NEW`.
- `esp_ota_mark_app_valid_cancel_rollback()` makes the running entry `VALID`.
  `esp_ota_mark_app_invalid_rollback_and_reboot()` makes it `INVALID` and
  boots the other entry.
- A production bootstrap (`flash_prepared_zone_lite.py`) writes one `VALID`
  entry for `ota_0` and none for `ota_1`.

So once 2.8.1 is `VALID`, a slot without a selectable entry is never booted,
and 2.8.1 needs nothing in it. Two facts keep that true, and
`tests/firmware/test_rollback_slot_contract.py` pins both:

- only an unconfirmed image, or a role never built as 2.8.x (the recovery
  image, the factory trial, the 2.7.0 writer), marks itself invalid;
- Zone Lite never calls `esp_ota_set_boot_partition()`. Only a complete,
  verified download (`esp_https_ota_finish()`) selects a slot.

## What the heartbeat shows

The direct role adds two fields to `diagnostics.storage`, beside
`upgrade_contract` (the marker above), `upgrade_ready` and `upgrade_error`:

- `rollback_slot`: the verdict in the table: `PREDECESSOR`, `NONE_NO_ENTRY`,
  `NONE_INVALID`, `NONE_ABORTED`, `NONE_PENDING_VERIFY` or `UNQUALIFIED`.
- `rollback_slot_state`: the other slot's otadata state: `NO_ENTRY`, `NEW`,
  `PENDING_VERIFY`, `VALID`, `INVALID`, `ABORTED`, `UNDEFINED` or `UNKNOWN`
  (an unrecognised state or another read error).

Other roles never send them, and neither does a direct boot refused before
the slot check (another version, Secure Boot off, not in an OTA slot).
Typical values:

| Situation | `rollback_slot` | `rollback_slot_state` | `upgrade_ready` |
|---|---|---|---|
| Upgraded from 2.7.8 by OTA (2.7.8 kept) | `PREDECESSOR` | `VALID` | true |
| New board, 2.5.2 in `ota_1` | `PREDECESSOR` | `NO_ENTRY` | true |
| A later image's download was interrupted | `NONE_NO_ENTRY` | `NO_ENTRY` | true |
| A later image rolled back | `NONE_INVALID` | `INVALID` | true |
| A later image reset before it confirmed | `NONE_ABORTED` | `ABORTED` | true |

## Release plumbing

- 2.8.1 accepts 2.8.0 as a qualified rollback predecessor beside 2.7.2 to
  2.7.8: device guard, compiled marker, ADD's baselines, the signing
  contract (`deploy/add/firmware-storage-contract.ps1`) and CI (the
  direct-legacy compile step checks the exact 172-character marker).
- **ADD must know 2.8.1, and be deployed, before 2.8.1 is published.** ADD
  refuses the signed storage contract of a direct version it does not know:
  once a 2.8.1 package is in the store, every release list, campaign list
  and preflight fails.
- Publication is as for 2.8.0. `firmware-hil-candidate.yml` publishes
  `zone-lite-2.8.1` HIL_ONLY to the signed 17-target nationwide scope
  (`deploy/add/hil-targets-zkt-270.json`), the owner runs campaigns, and
  `firmware-fleet-promote.yml` (exactly 2.8.0 or 2.8.1 besides 2.7.x)
  promotes it to AVAILABLE on field evidence. New boards then get it from
  `firmware-key-bootstrap.yml` (`production_release` 2.8.1, with 2.5.2 or
  2.7.8 in `ota_1`).

## Rollout

1. Merge; wait for the ADD deploy to succeed. Publish nothing before it.
2. Dispatch the HIL candidate build for 2.8.1 at the merge commit with the
   17 targets of `deploy/add/hil-targets-zkt-270.json`; it is published
   HIL_ONLY to those 17.
3. Campaign 2.8.1 straight from 2.7.8, or from whichever older listed image
   an ESP runs. Do not campaign 2.8.0: it has the old gate, and no device
   has run it.
4. After the soak (`firmware-fleet-promote.yml`: at least 3 healthy
   devices, at least 24 h), promote 2.8.1 to AVAILABLE. As for 2.8.0, an
   AVAILABLE image also reaches ESPs outside the 17 and those whose terminal
   is not confirmed (Gilgit's SenseFace site); HIL_ONLY reaches only the 17.

## Rollback

A 2.8.1 that never confirms rolls back to the image it came from. On 2.7.8
or 2.8.0 the old gate then applies: the other slot holds 2.8.1, which
neither lists, so the ESP boots with `STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED`,
the orange LED and DEGRADED. It stays online and still downloads (the OTA
path has no storage gate). Gilgit's ESP (2.7.8 behind a SenseFace) is the
likeliest case: 2.8.1 confirms only with its terminal online and the session
stable. Rescue it one of two ways:

- **Re-offer 2.8.1** once the cause is understood. When it installs and
  confirms, its own gate passes: the other slot then holds 2.7.8 or 2.8.0,
  both listed.
- **USB**: write the exact signed 2.5.2 (application
  `4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b`; the
  store's `zone-lite-2.5.2` or a key bootstrap's `rollback-predecessor.bin`)
  into the **inactive** slot only, as in steps 1 and 2 at the ESP in the
  [2.7.7 runbook](zone-lite-2.7.7-hil.md). Do not touch otadata or the
  running slot. The old gate reads only the slot's bytes, so the next boot
  is ready, and the slot's `INVALID` or `ABORTED` entry still keeps the
  bootloader off it.

While any ESP runs 2.7.8 or 2.8.0:

- **Never revoke 2.7.8 or 2.8.0.** A 2.8.1 rollback lands on them, ADD
  offers an upgrade only from a published release whose digest matches the
  running image, and a revocation is permanent.
- **Do not cancel a campaign mid-download.** On 2.7.8 or 2.8.0 a stopped
  download leaves part of 2.8.1 in the other slot, and from the next boot
  the old gate refuses storage until 2.8.1 installs (MULTAN-02's case). Let
  the download resume, or retry it.

From 2.8.1 on, a later image's rollback or interrupted download leaves 2.8.1
ready (`NONE_INVALID`, `NONE_ABORTED`, `NONE_NO_ENTRY`).

## Verification

- `tests/firmware/test_storage_upgrade_host.py` runs `storage_upgrade.c` and
  `upgrade_guard.c` on the host: every running and other-slot state, with
  and without a qualified predecessor, intact, partial or missing bytes in
  the other slot, and every earlier acceptance and refusal.
- `tests/firmware/test_rollback_slot_contract.py` (static): today's six
  `esp_ota_mark_app_invalid_rollback*` calls, each needing an unconfirmed
  image or a role never built as 2.8.x; no `esp_ota_set_boot_partition()`;
  one `esp_https_ota_finish()`.
- CI compiles the direct role as 2.8.1 and checks the exact marker; the
  publication and signing tests take 2.8.1 with the nationwide scope and
  2.8.0 as its predecessor.
- **Not verified on an ESP**: no ESP32-S3 has run 2.8.1, and no device has
  rolled back from it.

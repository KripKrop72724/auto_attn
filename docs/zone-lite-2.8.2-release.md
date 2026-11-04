# Zone Lite 2.8.2: 49-byte attendance records (LHRCENTRAL)

2.8.2 is 2.8.1 ([2.8.1 release](zone-lite-2.8.1-release.md)) with one
change: the firmware also reads the 49-byte attendance records of the ZKTeco
MB40-VL/ID terminals on platform ZAM180_TFT. ADD reads them too. Everything
else is 2.8.1's, including the rollback-slot storage gate.

It is published HIL_ONLY first to the three LHRCENTRAL ESPs, which cannot
run a full reconcile on any earlier image. Updating them to 2.8.1 does not
help: 2.8.1 refuses these records exactly as 2.7.8 does.

## Why

On 4 November 2026 every full reconcile (`FULL_HISTORY_BASELINE`) of
LF-ZONE-LHRCENTRAL-01, -02 and -03 (ZKT 2.7.8) ended NEEDS_ATTENTION with
`SOURCE_RANGE_LAYOUT_INVALID`, having scanned nothing. The ESP log on
LHRCENTRAL-01 read:

```
Source layout rejected (read=complete prepared=651361 payload=651357 count_before=13293 count_after=13293)
```

651357 is 13293 × 49. Every image up to 2.8.1 accepts only 8-, 16- and
40-byte records, in `zone_lite.c` and in ADD, so it refuses the whole
download. Live capture is not affected. A full reconcile is what carries the
history these terminals kept from before the ESPs were installed to ADD and
Oracle, so without it that history never arrives.

| Zone | ESP hardware ID | Terminal serial |
|---|---|---|
| LF-ZONE-LHRCENTRAL-01 | `ac:27:6e:a3:44:68` | `COVP232060159` |
| LF-ZONE-LHRCENTRAL-02 | `14:c1:9f:46:97:d0` | `COVP232060174` |
| LF-ZONE-LHRCENTRAL-03 | `a4:cb:8f:d4:55:28` | `COVP232060177` |

All three are new installer boards: 2.7.8 in `ota_0` and the signed 2.5.2 in
`ota_1`. None of them is one of the nationwide 17.

## The record

The three terminals' complete attendance logs were downloaded read-only on
4 November 2026 (`CMD_ATTLOG_RRQ`). The download has a 4-byte size header,
and the payload is exactly the record count × 49 on all three. Each record:

| Bytes | Field |
|---|---|
| 0-1 | uid, u16 little-endian (the record's sequence number) |
| 2-25 | user ID, ASCII, NUL-padded (24 bytes) |
| 26 | status (15 face, 1 finger, 16 seen once) |
| 27-30 | time, ZK packed, u32 little-endian |
| 31 | punch (always 0) |
| 32-39 | 8 zero bytes |
| 40-48 | tail: `ff 32 35 35 00 00 00 00 00` (`0xff`, ASCII "255", zeros); 6 records start with `00` instead |

Bytes 0-39 are byte for byte the 40-byte layout the firmware already parses.
A 49-byte record is that record plus a 9-byte tail attendance does not need,
so 2.8.2 reads the first 40 bytes as before and does not use the tail.
Every user ID was numeric, every time decoded, and the uids were sequential.
The firmware and ADD parts of that change are described with the change
itself (`zone_lite.c`; ADD's attendance recovery, force release, identity
evidence and reconciliation record-size checks).

## Release plumbing

- **Version.** `firmware/zone_lite/CMakeLists.txt` and `UG_DIRECT_VERSION`
  are 2.8.2.
- **Rollback predecessors.** 2.8.2 also accepts 2.8.1 (application
  `21238fa36df844642545e8c2f6bcfec03be37470890b776a600c39c0cdd1f5f0`,
  AVAILABLE since 4 November) in its other slot, beside 2.8.1's list:
  2.4.12, 2.5.2, 2.6.6-2.6.10, 2.6.12-2.6.15, 2.7.2-2.7.8 and 2.8.0. The
  device guard (`upgrade_guard.c`), the compiled marker, ADD's baselines
  (`storage_contract.py`), the signing contract
  (`deploy/add/firmware-storage-contract.ps1`) and CI (the direct-legacy
  compile step) all carry it. The compiled marker is 178 characters; ADD's
  heartbeat field keeps 256:

  ```
  ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10,2.6.12,2.6.13,2.6.14,2.6.15,2.7.2,2.7.3,2.7.4,2.7.5,2.7.6,2.7.7,2.7.8,2.8.0,2.8.1
  ```

- **ADD must know 2.8.2, and be deployed, before 2.8.2 is published.** ADD
  refuses the signed storage contract of a direct version it does not know:
  once a 2.8.2 package is in the store, every release list, campaign list and
  preflight fails. ADD also needs 2.8.2 as a 2.7 platform version for the
  reconcile itself: like 2.6.15 and every 2.7.x, 2.8.2 sends a source epoch
  only when ADD supplied one, and an unknown 2.7-or-later version would be
  refused as `SOURCE_EPOCH_MISMATCH`.
- **Fleet promotion.** `firmware-fleet-promote.yml` accepts exactly 2.8.2
  besides 2.7.x, 2.8.0 and 2.8.1.
- **New boards.** `firmware-key-bootstrap.yml` can carry 2.8.2 once it is
  AVAILABLE, with an older AVAILABLE image it qualifies in `ota_1` (2.5.2,
  2.7.8 or 2.8.1). The State Life
  Device Installer's `QUALIFIED_PREDECESSORS` now lists 2.8.1 as well, as the
  newest image qualifies it; an installer bundle with 2.8.1 in `ota_1` needs
  an app built from this change.

## HIL scope

2.8.2 keeps the rule of every 2.7 platform image (an exact prefix of the
signed nationwide 17 in `deploy/add/hil-targets-zkt-270.json`). It may
instead be published to an exact ordered prefix of the three LHRCENTRAL
ESPs, in zone order:

- `deploy/add/hil-pins-2.8.2-lhrcentral.json` pins each ESP's hardware ID
  and terminal serial. ADD holds the same pins
  (`apps/add_backend/zk_add/zkt282_lhrcentral.py`), and a test keeps the two
  equal.
- The connector ID of each target is the one ADD shows for that ESP, a
  lowercase UUID. Onboarding keys a connector by hardware ID and never
  rebinds it, so a wrong connector ID cannot widen the scope: ADD then finds
  no connector with that ID and that hardware ID, and offers nothing.
- `firmware-hil-candidate.yml`, `sign-firmware-release.ps1` and
  `publish-firmware.ps1` (through `firmware-2-8-2-hil-scope.ps1`) refuse any
  other list: another order, a gap, a fourth entry, another serial or
  hardware ID, an uppercase or non-UUID connector ID, a repeated connector,
  extra fields, or LHRCENTRAL after the 17.
- ADD's release parser and its fleet offer guard admit an LHRCENTRAL ESP
  only for the HIL_ONLY `zone-lite-2.8.2`, and only while the ESP is active,
  not a spare, on its own connector and hardware ID, with its terminal
  binding CONFIRMED to the pinned serial. No other version, HIL_ONLY 2.8.1
  included, can reach them as HIL. Every other HIL_ONLY image still reaches
  only the 17.

## Rollout

1. Merge the 49-byte change and this release plumbing together, and wait for
   the ADD deploy to succeed. Publish nothing before it.
2. Check each LHRCENTRAL ESP in ADD: online, `zone-lite-2.7.8` with
   application digest
   `b8d0ca7e20d28d4114cff56fc69676e681a37bf9f7e831a243a65f9a1a972a3e`,
   running `ota_0`, and its terminal binding CONFIRMED to the serial in the
   table above. Note each ESP's connector ID. 2.7.8 must still be published.
3. Dispatch the HIL candidate build for 2.8.2 at the merge commit with the
   three targets, in zone order:

   ```
   gh workflow run firmware-hil-candidate.yml --ref main \
     -f firmware_family=zkt -f commit_sha=<merge commit> -f version=2.8.2 -f device_mac= \
     -f targets_json='[{"connector_id":"<LHRCENTRAL-01 connector>","mac":"ac:27:6e:a3:44:68","terminal_serial":"COVP232060159"},{"connector_id":"<LHRCENTRAL-02 connector>","mac":"14:c1:9f:46:97:d0","terminal_serial":"COVP232060174"},{"connector_id":"<LHRCENTRAL-03 connector>","mac":"a4:cb:8f:d4:55:28","terminal_serial":"COVP232060177"}]'
   ```

   It is published HIL_ONLY to those three. A shorter prefix (LHRCENTRAL-01
   alone) is also accepted, but today's tooling cannot widen a published
   2.8.2's scope later, so publish all three at once.
4. Campaign 2.8.2 in each LHRCENTRAL zone, straight from 2.7.8. The three
   one-zone campaigns may run at the same time. Do not campaign 2.8.1 there
   first: it does not read these records.
5. Verify (below). If all three pass, run a full reconcile on each, if one
   is not already queued.
6. After the soak (`firmware-fleet-promote.yml`: at least 3 healthy devices,
   at least 24 h; the three LHRCENTRAL ESPs are enough), the owner may
   promote 2.8.2 to AVAILABLE. ADD then offers it, through the exact
   predecessor check, to every ESP that may take an AVAILABLE image.

## What the heartbeat shows

2.8.2 is written over 2.5.2 in `ota_1` and boots there, with 2.7.8 in `ota_0`:

| Field | Value |
|---|---|
| `firmware_version` | `zone-lite-2.8.2` |
| `ota.running_partition` | `ota_1` |
| `storage.upgrade_contract` | the 178-character marker above |
| `storage.upgrade_ready` | true |
| `storage.rollback_slot` / `rollback_slot_state` | `PREDECESSOR` / `VALID` |

## Rollback

A 2.8.2 that never confirms rolls back to 2.7.8 in `ota_0`. 2.7.8's storage
gate does not list 2.8.2, so with 2.8.2 left in its other slot it boots with
`STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED`, the orange LED and DEGRADED in
ADD; a download stopped part way does the same. ADD labels it
`OTA_ROLLBACK_SLOT_RESIDUE` with the remedy "offer 2.8.1"
([device health](add-device-health.md)). The ESP stays online and still
downloads. On LHRCENTRAL:

- **Re-offer 2.8.2** once the cause is understood. When it installs and
  confirms, its own gate passes (the other slot then holds 2.7.8). 2.8.1
  would clear the gate too, but not the reconcile.
- **USB**: write the exact signed 2.5.2 into the inactive slot only, as in
  the [2.8.1 runbook](zone-lite-2.8.1-release.md#rollback).

While any ESP runs 2.7.8, 2.8.0 or 2.8.1, never revoke them, and do not
cancel a campaign mid-download (2.7.8's and 2.8.0's gates).

## Verification

On each of LHRCENTRAL-01, -02 and -03:

- ADD shows the heartbeat above, and live capture continues as on 2.7.8.
- A full reconcile (`FULL_HISTORY_BASELINE`) completes instead of
  NEEDS_ATTENTION. Its scanned count matches the terminal's record count
  (about 13.3 thousand, 8.4 thousand and 4.1 thousand records on 4 November,
  growing with every punch), with no `SOURCE_RANGE_LAYOUT_INVALID` in the
  ESP log.
- The recovered history reaches Oracle like any reconciled punch.

In the repository:

- `tests/firmware/test_storage_upgrade_host.py` runs the device guard on the
  host as 2.8.2: 2.8.1 on its exact bytes is accepted, on other bytes it is
  refused, and a 2.8.2 in the other slot is refused while it is selectable.
- CI compiles the direct role as 2.8.2 and checks the exact marker.
- `tests/unit/test_zkt282_lhrcentral_scope.py` and
  `tests/unit/test_hil_candidate_282_scope.py` pin the LHRCENTRAL scope in
  ADD and in the candidate workflow; `tests/firmware/test_hil_publication.ps1`
  in the signer's contract and the publisher.
- `tests/unit/test_platform_27_release.py` covers 2.8.2's predecessors in
  ADD and the source-epoch rule.

**Not verified on an ESP**: no ESP has run 2.8.2, and no device has rolled
back from it.

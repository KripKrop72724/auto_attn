# Zone Lite 2.7.1: ZKT platform HIL

## What 2.7.1 is

2.7.1 is the 2.7 platform image in phase L: it keeps the legacy delivery
authority of the 2.6.15 fleet. No journal writer, reader role or separate
bridge is built in. It replaces the frozen 2.6.22, 2.6.23 and 2.7.0 packages,
which must never be installed (the 2.7.0 writer refuses every OTA, and a valid
2.6.23 accepts only 2.7.0).

The image combines:

- platform memory management: large CPU-only buffers in PSRAM, allocation-failure
  counters and per-task stack watermarks (#375);
- a boot report: reset reason, panic task/address/exception, watchdog and
  brownout counts, the last `ESP_ERROR_CHECK` call site, and the stage the
  previous boot reached (#376);
- recoverable faults: a queue-store fault, legacy write fault or LED
  `LOCAL_FAILURE` clears only after verified recovery, never on a timer (#377);
- boot confirmation that counts only accountable time: the 900 s budget pauses
  while a healthy-looking image waits on the site, with a 4 h cap (#379);
- the failing file name of local storage errors, reported as `error_file` and
  `recovered_error_file` (#380).

## Who can install it

The storage contract admits a direct upgrade only from these signed images,
running in an OTA slot with the exact application digest:
2.4.12, 2.5.2, 2.6.6, 2.6.7, 2.6.8, 2.6.9, 2.6.10, 2.6.12, 2.6.13, 2.6.14 and
2.6.15. The compiled marker is
`ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=...,2.6.14,2.6.15`. The
firmware rechecks the rollback slot's digest before it opens storage. A device
running from the factory partition, or any other digest, is excluded by the
campaign preview and needs its signed bridge first.

## Publication

Build, sign and publish with `firmware-hil-candidate.yml` from the exact green
main commit:

- `firmware_family`: `zkt`
- `version`: `2.7.1`
- `targets_json`: the ordered nationwide list in
  `deploy/add/hil-targets-zkt-270.json` (17 targets, SLICTOWER 3FL first). Only
  an exact prefix of that list is accepted; no MAC and no ad hoc list.

The release is published `HIL_ONLY`. The `firmware-signing` and
`firmware-production` environment approvals remain with the owner. ADD imports
it as `zone-lite-2.7.1`.

## Campaigns

ADD treats each of the 17 identities as an independent HIL target; no wave has
to finish first. The pace comes from:

- one campaign per zone, with exactly one eligible connector, a password
  step-up and the typed version `2.7.1`;
- the fleet reservation guard: at most two unsettled ZKT upgrades, and at most
  one per location.

The agreed order is SLICTOWER 3FL, then Swat 01, Peshawar 06 and Peshawar 02,
then the other eligible devices. Start the next target only after the previous
one is accepted below, or its failure is understood.

On 12 October 2026, Lahore 02's 2.6.15 deployment had been `RECONCILING` since
2 October. An active deployment holds a reservation, so it uses one of the
two slots: only one other upgrade can be in flight at a time. Lahore 01 and
Lahore 02 also wait until that deployment settles. The older Swat 01 and
Quetta 01 reservations from August are settled by their later verified
installations. Cancelling a campaign never frees a slot whose bytes were
offered; only a later verified installation does.

## Acceptance evidence per target

OTA success alone is not acceptance. For each target, record:

1. `SUCCEEDED` with the exact signed 2.7.1 application digest, reported from
   an OTA slot, with the qualified predecessor in the other slot.
2. Boot confirmation within budget: `ota.boot_confirm_active_seconds` under
   900 and no `BOOT_HEALTH_TIMEOUT` or `BOOT_CONFIRM_CAP`.
3. `diagnostics.boot_report`: a clean reset reason after the OTA restart, with
   no panic, watchdog or brownout counts rising during the observation.
4. `diagnostics.memory`: stable `internal_largest_block_bytes` and
   `dma_free_bytes`, `alloc_failures` not increasing, and positive stack
   watermarks.
5. Storage: durability healthy. Any fault must show as
   `recovered_error_code` and `recoveries` without a reboot, and the LED must
   leave `LOCAL_FAILURE` after recovery.
6. Delivery: fresh attendance acknowledged by Oracle through the legacy path,
   with no persistent delivery-worker fault.
7. A device health of `ONLINE` or `ONLINE_WITH_WARNINGS`. Any `DEGRADED` must
   have a real cause.

Wave A and C targets are observed for 72 hours, and wave B and D targets for
168 hours (`zkt270_scope.py`).

## Leftover journal evidence on 3FL

Like every legacy image built since the journal bridges, 2.7.1 refuses any
later OTA download with `JOURNAL_LEGACY_EVIDENCE_PRESENT` once a
`zkt_journal` NVS namespace or a `zktj*` storage file exists
(`zkt_ota_guard.c`). The refusal happens before anything is written.

2.7.1 never creates that evidence: its journal runtime stays disabled, because
its version is neither the 2.6.17 reader nor the 2.7.0 writer. So devices
that never ran a bridge can always take the next image.

SLICTOWER 3FL is the exception. It ran bridges 2.6.18 to 2.6.21, which may
have left that evidence behind. 2.6.15 has no such guard, so this does not
affect installing 2.7.1. It may refuse 3FL's next upgrade after 2.7.1. That
case needs a custody decision on the leftover evidence before the next phase
image; USB access is available at 3FL.

## Failure and rollback

A new image that cannot prove health within its accountable budget marks
itself invalid and returns to the predecessor in the other slot. ADD records
the rollback. Do not cancel a campaign while the device is `OFFERED` or
`DOWNLOADING`, and never erase connector storage outside the approved custody
paths. Keep the release `HIL_ONLY`; HIL results do not authorize nationwide
promotion on their own.

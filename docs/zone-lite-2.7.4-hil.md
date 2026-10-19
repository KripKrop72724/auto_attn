# Zone Lite 2.7.4: ZKT platform HIL

2.7.4 is 2.7.3 plus storage diagnosis: it says which file a device's store
cannot read back, repairs the one file the persistence probe owns, and
accepts 2.7.3 as a rollback predecessor. Everything in the
[2.7.3 page](zone-lite-2.7.3-hil.md) still applies.

## Why

On 13 October 2026, 2.7.3 confirmed on Swat 01, Quetta 01 and Peshawar 06
within about two minutes: storage healthy, terminal online, and the stuck
storage warnings of 2.6.15 cleared. On SLICTOWER 13FL (2.4.12) it booted
cleanly but never became healthy, and after its 900 s confirmation budget
it rolled back to 2.4.12 (`BOOT_HEALTH_TIMEOUT`) as designed:

- the persistence probe failed every attempt at `persistence_open_write`
  with ENOENT: `/storage/persistence-probe` could not be reopened for
  writing;
- reading the legacy blocked-identity queue failed on every pass, which
  raised `LOCAL_FAILURE` (103, then 146 times) without saying why;
- the store was 61% used, and 2.4.12 reported it as full after the
  rollback.

The 2.7.3 report could not say which file was damaged or where, and secure
download mode keeps the flash unreadable over USB, so the device itself has
to report it. Peshawar 02 had the same class of damage in its blocked file
(unreadable regions, 9 October 2026; see the storage recovery images).

## What 2.7.4 changes

**Store inventory.** A minute after boot a low-priority task reads every
file of `/storage` back, one SPIFFS page at a time and unbuffered, within a
180 s budget. A page that cannot be read is counted and skipped, so one
damaged page never hides the rest of a file. The heartbeat then reports
`storage.inventory`: the files examined, their bytes, how many files and
bytes are unreadable, and up to 16 files, unreadable ones first, each with
its size, unreadable bytes, first failing offset and errno. Each unreadable
file is also logged once (`STORAGE_FILE_UNREADABLE`), with a summary
(`STORAGE_INVENTORY`). The scan never writes.

**Probe file repair.** When the probe file cannot be reopened for writing,
it is removed and created again; it holds nothing. Only a store that cannot
create a fresh file now fails the probe. `storage.persistence_probe_repairs`
counts the replacements.

**Queue read failures named.** A failed read of the legacy blocked-identity
queue reports its step (restore, open, peek, reclaim), result, errno, cursor
and file size once per boot (`LEGACY_QUEUE_READ_FAILED`); so does a failed
read of a legacy quarantine queue. 2.7.3 raised only `LOCAL_FAILURE`.

**2.7.3 as a rollback predecessor.** 2.7.4 accepts the exact signed 2.7.3
(application SHA-256 `98855248bb4325103b5253ab4b15f4e939adf3ace89c208d463b09ec9ca21a6a`)
as well as 2.7.2 in its rollback slot, in the device guard and in ADD's
storage contract. The compiled marker ends `,2.6.15,2.7.2,2.7.3`. Swat 01,
Quetta 01 and Peshawar 06 can move to 2.7.4 in place.

2.7.4 does not repair queue files. Moving readable rows into ADD custody and
releasing unreadable regions changes attendance storage; it follows in 2.7.5,
built against what 2.7.4 reports, with owner sign-off.

## Release and HIL

Publish 2.7.4 like 2.7.3: `firmware-hil-candidate.yml` with family `zkt`, the
green main SHA, version `2.7.4` and the ordered nationwide targets. It is
`HIL_ONLY`, with 13 qualified predecessors. Keep 2.7.2 and 2.7.3 published:
ADD offers 2.7.4 to a 2.7.2 or 2.7.3 device only while that exact release is
published.

Start on SLICTOWER 13FL. Expect it to roll back again if the blocked queue
is damaged; within the first minutes ADD should show:

- `storage.inventory` with the damaged file, its first failing offset and
  errno, and the unreadable byte count;
- `LEGACY_QUEUE_READ_FAILED` with the step that fails;
- whether the probe passes after `persistence_probe_repairs` reaches 1.

That decides the 2.7.5 repair. Devices whose 2.7.3 confirmed may take 2.7.4
at any time; the inventory runs on them too.

## ZONE-BLD9-01 K50, 19 October 2026

The owner asked to OTA-test 2.7.4 on ZONE-BLD9-01 as well. That device is
outside the nationwide 17:

- connector `9d8c589f-9735-4b58-9277-a385a60d0c9b`, ESP MAC
  `ac:27:6e:a3:33:28`, K50 terminal serial `ANM4261301077`, zone
  `ZONE-BLD9-01`;
- it is not the nationwide "BLD9 01" (connector `2f5cedd8…`, a G3 in
  `LF-ZONE-BLD9-01`);
- it runs 2.5.2 from its factory partition, application SHA-256
  `c8c4356bb6a7fd4dc781e893ad5290593599b2d167be30c271bd1d5db61934f1` (as ADD
  reported it on 19 October).

### What the change does

It makes the published, signed 2.7.4 (source `87e14fe6…`, artifact
`9a3e2226…`, application `3e4bb613…`) eligible for this one device. Nothing is
rebuilt or re-signed; only the HIL marker changes.

- **Exposure.** ADD accepts an 18-target marker only for those exact bytes,
  and only as the signed 17 in order with this K50 last
  (`hil-targets-2.7.4-bld9-k50.json`, `zk_add/zkt274_bld9_k50.py`). Every other
  release keeps its exact nationwide prefix. The nationwide inventory, its
  waves and the denominator stay 17; `hil-targets-zkt-270.json` and every
  signed manifest are unchanged.
- **Factory bridge.** 2.7.4 still refuses a device on its factory partition
  (`DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED`), and so does the device itself. The
  device first takes the published signed 2.4.12 into an OTA slot, as BLD5
  and Lahore did. ADD pins that bridge to this zone, this device, its factory
  2.5.2 digest and the exact signed 2.4.12; the legacy 2.4.12 progress adapter
  covers it. After the bridge, 2.4.12 in `ota_0` is an ordinary 2.7.4
  predecessor.
- **Fleet offer guard.** Only those two exact releases may queue this device.
  It counts as location BLD9, shared with the nationwide BLD9 01 and 02, and
  the two-slot, offline and scan limits still apply. Any other device outside
  the 17 still waits with `NATIONWIDE_EXACT_TARGET_REQUIRED`. ADD offers bytes
  only while the device is in `ZONE-BLD9-01`.

An installation here is not HIL acceptance and does not count towards the 17.

### Operator steps

1. Merge, and wait for `add-deploy` to finish green.
2. **Bridge.** In ADD, start a campaign for `zone-lite-2.4.12` on zone
   `ZONE-BLD9-01`. The preview must show exactly one eligible device, the K50.
   Enter a reason, type `2.4.12` and your password.
   - Wait for the deployment to reach `SUCCEEDED`, with the device on 2.4.12
     from `ota_0`, application `cf9e6e2d…`.
   - An error code starting `NATIONWIDE_` means it is waiting for a slot.
     Lahore 02 holds one; BLD9 01 and 02 share the location.
   - Then cancel the finished bridge campaign so the zone can take the next
     one. The deployment stays `SUCCEEDED`.
3. **Exposure** (any time after step 1). Run Actions → "Extend reviewed signed
   ZKT HIL scope" from `main`:
   - scope `2.7.4-bld9-k50-preview`, confirmation
     `PREVIEW-2.7.4-BLD9-K50-HIL`. It checks that the deployed ADD holds the
     published 17 as current, runs the extension preflight ("17 to 18 ordered
     targets"), and has the deployed ADD parse the 18-target marker
     (`CATALOG_REFRESH_PENDING`). Nothing changes.
   - scope `2.7.4-bld9-k50`, confirmation `EXTEND-2.7.4-BLD9-K50-HIL`. It
     repeats those checks, replaces only `.hil-only.json`, keeps the old one as
     `.hil-scope-before-*.json`, and re-checks the deployed ADD and the
     unchanged manifest, signature and image. Approve `firmware-production`
     when asked.
   - Open ADD's firmware page: `zone-lite-2.7.4` should list 18 allowed
     targets.
4. **2.7.4.** Start a campaign for `zone-lite-2.7.4` on `ZONE-BLD9-01`: one
   eligible device, type `2.7.4` and your password. Watch `BOOTED_PENDING`,
   `RECONCILING` and `SUCCEEDED`, the boot-confirmation counters, and the
   `ZKT_FRAME_*`, `ZKT_TIME_SYNC_*` and `STORAGE_INVENTORY` logs.

### Rollback

- **Device.** An unconfirmed 2.7.4 rolls back by itself to 2.4.12 in `ota_0`,
  not to 2.5.2: after 900 s of accountable unhealthy time, or at the 4-hour cap
  while ADD or the terminal is unreachable. Any reset before confirmation
  also rolls back. A failed 2.4.12 bridge returns to factory 2.5.2.
- **ADD.** A `FAILED` or `ROLLED_BACK` deployment pauses its campaign; cancel
  it. Do not revoke 2.7.4: other HIL devices run it.
- **Exposure.** Cancelling the campaign is enough to stop offers. To withdraw
  the exposure as well, restore the `.hil-scope-before-*.json` backup over
  `.hil-only.json` in the store's `2.7.4` directory and open the firmware page.
  Restore it before reverting this change: an ADD without it cannot parse the
  18-target marker, and every firmware catalog read would fail.

### K50 risk

- No 2.4.12, 2.6 or 2.7 image has run on a K50; the 17 are G3, uFace800,
  MB40-VL and SilkBio terminals. Record sizes are the same as 2.5.2, but 2.7.4
  also writes the terminal clock and reads it back, which 2.5.2 never does.
- If the K50 session never comes up, the unconfirmed image can wait up to the
  4-hour cap before rolling back, with live capture impaired meanwhile. A
  flapping session that raises `LOCAL_FAILURE` rolls back after about 15
  minutes.
- Boot health needs healthy storage, a passing persistence probe and no
  `LOCAL_FAILURE`; 13FL and Peshawar 02 rolled back from 2.7.4 for this. On 19
  October the device's only warning was `DEVICE_MESSAGE_REJECTED` (a
  reconcile chunk rejected as `EVIDENCE_INVALID`); 2.5.2 reports no storage
  diagnostics.
- The known 2.7.4 issues apply: inventory sizes shown as 4294967295, a slow
  inventory scan, more reconnects on Peshawar 06 (about 24.6 an hour against
  1.8 on 2.7.3), and the fleet-wide delivery-throttle overflow after about 24.9
  days of uptime.
- 2.7.5 must accept 2.7.4 or 2.4.12 as this device's predecessor and keep its
  exposure explicit.

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

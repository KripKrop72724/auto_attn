# Zone Lite 2.7.3: ZKT platform HIL

2.7.3 replaces 2.7.2. It is the same 2.7 platform image in phase L (legacy
delivery authority) with the same terminal framing. It records the panic
trace that 2.7.2 could not, closes the defects found while reviewing 3FL's
2.7.2 crash, and accepts 2.7.2 as a rollback predecessor so that 3FL upgrades
in place. The [2.7.1 runbook](zone-lite-2.7.1-hil.md) still applies to
publication, campaigns, acceptance evidence and rollback, and the
[2.7.2 page](zone-lite-2.7.2-hil.md) to terminal framing.

## Why 2.7.2 is replaced

On 12 October 2026, 2.7.2 on SLICTOWER 3FL panicked at about 19:04 UTC after
2 h 35 m (9,290 s) of uptime. Its boot report said:

```
reset_reason PANIC, previous_stage ADD_CONNECTED
panic: exception FAULT, address 0x00000000, core 4294967295, task add_heartbeat
```

Read against the ESP-IDF 5.5.3 panic code, this is a **cache error**, not a
jump to address 0:

- ESP-IDF fills the panic address only for CPU exceptions. For a cache error
  (a pseudo exception) it stays 0.
- The core is -1 only when ESP-IDF cannot tell which CPU caused a cache
  error. On the ESP32-S3 that rules out the "cache disabled but accessed"
  class, which always names its CPU. What remains is the illegal-access
  class: an MMU entry fault (a load, store or fetch at an unmapped flash or
  PSRAM address, for example through a stale or wild pointer), a dcache
  write-back to flash (a store into flash-resident constants, which faults
  later, in whatever task evicts the line), or a cache sync or preload
  operation on a bad address.
- With the CPU unknown, both cores enter the panic handler, core 1 waits and
  core 0 reports. `add_heartbeat` is the task core 0 was running. Core 1's
  task was not recorded, so the faulting code may be on either core.

The crash came 2 to 12 seconds after the hourly terminal session restart read
the user table (`ZKT_CREDENTIAL_POLICY_OK` at 19:04:12 UTC), while the gateway
published the full user snapshot to ADD and the heartbeat ran. The next boot
has run normally since.

The 2.7.2 record has no cache-error type, fault address, PC or backtrace, and
no UART capture was running, so the faulting code cannot be identified from
it. Read-only reviews of the code active in that window found no defect in
the terminal session path or the ORDS, outbox and storage tasks, and no store
into flash-resident data in the ZKT image. They found one mechanism that fits
the signature, in the ADD WebSocket transport: the client can free its TLS
session while another task is still writing to it. The corrupted PSRAM heap
then faults in whichever task allocates next, and the heartbeat allocates
heavily. In the crashed boot the ADD channel closed and recovered five times
in 2.5 hours, so that window opened repeatedly. The 2.7.2 record cannot
confirm it; 2.7.3 closes it and records enough to tell next time.

## What 2.7.3 changes

**Panic trace.** A panic now also records what ESP-IDF prints on the console,
and the next boot's heartbeat reports it in `diagnostics.boot_report.panic`:

- `reason`: ESP-IDF's panic reason, for example `Cache error` or
  `LoadProhibited` (omitted for `abort()`, whose reason describes abort
  itself);
- `cache_error`: the first line of ESP-IDF's cache error, for example
  `MMU entry fault error`;
- `fault_address`: the address a failed load, store or fetch named
  (EXCVADDR), or the cache error's address when it has one;
- `cores`: for each core, its task, and when the core entered the panic
  handler itself, its PC and up to 8 backtrace entries exactly as ESP-IDF
  prints them, with `corrupted` when the walk stopped at an insane frame.

The trace follows the 2.7.1 record in RTC memory and is attributed by boot
count to the boot that panicked. The 2.7.1 record keeps its layout and its
address (0x50000000 in both builds), so if the bootloader rolls back to 2.7.2
after an early 2.7.3 panic, 2.7.2 still reports the exception, task and
uptime. Only the HIL reboot witness moves, and it is bound to one image. ADD
drops an unreadable part of a trace instead of rejecting the heartbeat.

To read a trace, build the same source with the same ESP-IDF image (builds
are reproducible) and resolve the addresses:

```bash
xtensa-esp32s3-elf-addr2line -pfiaC -e build/zone_lite.elf 0x42012342 0x42034564
```

**ADD transport lifecycle.** esp_websocket_client 1.7.0 closes its TLS
transport without its client lock when its task exits, and frees the
transport when it starts again. 2.7.2 exposed both:

- When ADD closed the socket (for example `1013 Controlled ADD interruption`
  or a delivery failure), the client task ended and closed TLS while a send
  could still be writing; the channel then stayed down until the heartbeat
  restarted the client. The client now reconnects after a server close
  itself (`enable_close_reconnect`), closing under its lock.
- When the channel stays down for 45 s, the heartbeat restarts the client.
  The restart now holds the same lock as every send, so no send is in flight;
  a busy sender defers it to a later heartbeat.
- The disconnect timestamp is no longer taken while the channel is up, which
  let a later disconnect trigger that restart at once, and the connected
  event waits up to 1 s for the state lock instead of 100 ms, so a live
  channel is not left marked down.

**Heartbeat task names.** The stack report read each task's name after the
scheduler snapshot. A task deleted in between leaves that pointer in a freed
control block. At most a name's length is now read and only printable text is
sent; ADD rejects an empty name, so an empty one becomes `?`.

**ORDS response capture.** If its 8 KiB buffer cannot be allocated, the HTTPS
client now captures nothing instead of writing near address 0.

**2.7.2 as a rollback predecessor.** 2.7.3 accepts the exact signed 2.7.2
(application SHA-256 `e908496e0695d8c29e96a6f2db3840060eeab526c4012e29a3deef826f7c8eea`)
in its rollback slot, in both the device guard and ADD's storage contract.
The compiled marker ends `,2.6.15,2.7.2`. 2.7.1 and 2.7.2 keep their own
contracts.

## Release and HIL

Publish 2.7.3 like 2.7.2: `firmware-hil-candidate.yml` with family `zkt`, the
green main SHA, version `2.7.3` and the ordered nationwide targets. It is
`HIL_ONLY`, with 12 qualified predecessors.

Revoke 2.7.1 in ADD; it is still `HIL_ONLY`. Keep 2.7.2 `HIL_ONLY` until 3FL
runs 2.7.3: ADD offers 2.7.3 to a 2.7.2 device only while the exact 2.7.2
release is published.

Start again on SLICTOWER 3FL, which runs 2.7.2. Before downloading, 2.7.2
refuses any update while journal evidence from an earlier bridge image is on
the device (the `zkt_journal` NVS namespace or a `zktj*` file); 3FL ran the
2.6.18 to 2.6.21 bridges. The deployment then fails with
`JOURNAL_LEGACY_EVIDENCE_PRESENT` before anything is written, and 2.7.2 keeps
running. If that happens, start 2.7.3 HIL at Swat 01 instead (2.6.15 has no
such check) and decide separately how 3FL is brought forward.

In addition to the 2.7.2 acceptance evidence, check that:

- 3FL's 2.7.3 boot report shows reset reason `SOFTWARE` after the update and
  no panic;
- any later panic reports `reason`, and `cores` with backtraces; resolve them
  against the 2.7.3 build before going further.

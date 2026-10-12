# Zone Lite 2.7.2: ZKT platform HIL

2.7.2 replaces 2.7.1. It is the same 2.7 platform image in phase L (legacy
delivery authority), with the terminal framing that 2.6.15 accepted. The
[2.7.1 runbook](zone-lite-2.7.1-hil.md) still applies to publication,
campaigns, acceptance evidence and rollback; this page covers only what
changed.

## Why 2.7.1 failed on SLICTOWER 3FL

On 12 October 2026, 2.7.1 installed on SLICTOWER 3FL and booted cleanly. Its
storage was healthy, its boot report was clean, and it had no allocation
failures. But its terminal session ended about a second after every
`ZKT_TIME_SYNC_OK`, so the link stayed `FLAPPING` and live capture stopped.
Boot confirmation paused as designed, so the image could never confirm.

The UART capture of the 2.6.21 bridge from 8 October, which shares this
terminal code, shows the failing step on the same G3 (ZMM220_TFT):

```
ZKT read_with_buffer command=9 fct=5 response code=2000 data_len=13
Failed reading ZKT chunk at 0 code=1500 len=0 want=12172
Could not read ZKT users
ZKT session ended after 789 ms
```

2.6.15 reads the same table (`Read ZKT buffered data 12172/12172`). On
3 October, the journal work made the prepared-read receiver strict: a frame
whose session ID differs, or that carries more data than requested, now fails
the read. 2.6.15 accepted both. One of them is what the G3 sends. That is
also consistent with every bridge failing to keep a terminal session on 3FL.

The device returned to 2.6.15 when the ESP was power-cycled: 2.7.1 had not
confirmed itself, so the bootloader rolled back, and ADD recorded the
deployment as `ROLLED_BACK`. Within minutes 2.6.15 read the same user table
(`ZKT_CREDENTIAL_POLICY_OK`), so the terminal itself was fine. The terminal
keeps every punch, so none are lost.

## What 2.7.2 changes

While the image has legacy authority (always, in phase L), terminal framing
follows 2.6.15:

- a reply, data frame, interleaved event or live packet whose session ID
  differs is accepted;
- a data frame larger than the requested range is truncated to it;
- a direct reply to an attendance tail range read that is longer than the
  range uses the requested prefix, as 2.6.15 did; 2.7.1 required an exact
  length, which would have stopped tail reconciliation on such a terminal;
- a command reply too large for its buffer is drained within the frame limit,
  so the session stays framed;
- a table whose size does not match the reported count exactly falls back to
  the 2.6.15 record-size choice.

The journal roles keep every strict check. Starting a terminal session also
waits up to 1 s for a busy lock instead of 100 ms, so contention no longer
sends the gateway into a full network discovery.

ADD applies 2.6.15's source-epoch rule to the 2.7 platform: a tail without an
epoch is accepted unless the device is under custody, and an epoch the
device supplies is still checked. Before this, ADD required an epoch from
every ZKT firmware from 2.7.0 onwards, a rule meant for the journal writer.

Each kind of deviation is reported to ADD the first time it is seen in a boot,
with the command and the expected and observed values:
`ZKT_FRAME_SESSION_MISMATCH`, `ZKT_FRAME_OVERSIZE` and
`ZKT_RECORD_SIZE_FALLBACK`. A prepared or tail range read that still fails
reports `ZKT_PREPARED_READ_FAILED` with its exact cause, once per distinct
cause per boot. On 2.7.1 these failures were visible only on the UART.

## Known differences from 2.6.15 that remain

A review of the 2.6.15 to 2.7.1 terminal code found these, which 2.7.2 does
not change:

- A live terminal session still ends if the session lock cannot be taken
  within 100 ms, by design, so that an OTA restart can never be overtaken.
  The lock's critical sections are very short.
- Attendance records are decoded more strictly: 8-byte records, and 40-byte
  records with an empty or unprintable user ID, are skipped by the full-dump
  reconciler instead of being queued. 3FL uses 40-byte records. Before
  offering 2.7.2 to another device, check the record size its tail chunks
  report in ADD.
- During an ADD source assignment, a buffer release that is not acknowledged
  now closes the terminal socket, and a layout mismatch holds the assignment
  until its lease expires.

## Release and HIL

Publish 2.7.2 exactly like 2.7.1: `firmware-hil-candidate.yml` with family
`zkt`, the green main SHA, version `2.7.2` and the ordered nationwide targets.
It is `HIL_ONLY`, with the same 11 qualified predecessors and the same
compiled storage marker.

Revoke 2.7.1 in ADD so that it can never be offered again.

Start again on SLICTOWER 3FL. In addition to the 2.7.1 acceptance evidence,
check that:

- the terminal session reaches `ZKT_STABLE` and live attendance flows;
- boot confirmation reports accountable seconds and confirms;
- the `ZKT_FRAME_*` logs are recorded. They say which deviation the G3 sends,
  which the journal phase must handle before it can keep strict framing.

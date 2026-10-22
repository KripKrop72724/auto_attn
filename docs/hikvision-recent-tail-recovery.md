# Hikvision recent-tail recovery

ADD can read a Hikvision terminal's retained event log after the last punch it
holds, then keep following the terminal tail. It is for a terminal whose own ESP
poll is blocked, for example by `HIK_ANCHOR_MISMATCH`. It needs no firmware
change. It uses the ESP's existing `hikvision_history_assignment` worker, which
every 3.1.x and bld1fix image runs, including while the poll is failing.

## What it does

- It resumes 20 records before the highest serial ADD holds through gap-free
  custody: the ESP's checkpointed `POLL` channel, or an earlier recent-tail
  job. Live-stream records do not count, because a stream can drop events while
  it is disconnected. Employee-filtered history does not count either, because
  it is not contiguous.
- The poll reads every event serial in order. If a serial is missing below the
  poll's newest record, the ESP's checkpoint restarted further on, for example
  after a reinstall or a reset. ADD then resumes at the latest such break, so
  serials the poll never read are not skipped. A break that is only a gap in
  the terminal's own numbering costs a longer re-read, nothing more.
- Each page asks for 20 records from the last serial already seen, up to the
  maximum serial. The terminal may treat `beginSerialNo` as inclusive or as
  exclusive; either way no record is skipped.
- Records go through the normal evidence and attendance path. Each source event
  has `event_uid = (terminal serial, source epoch, serialNo)`. That means:
  - a punch already in ADD, from the poll, the stream replay or a later
    re-anchored poll, is never delivered twice;
  - a serial whose facts changed is held as `SOURCE_FACT_CONFLICT`, together
    with the record ADD already had. It is never merged and never attributed
    to another person.
- Once it has caught up, it checks every 15 seconds. It ends with outcome
  `LIVE_POLL_RESTORED` when the ESP reports `poll_error=0` with a durable
  cursor at or below the job's last serial. An administrator can also cancel
  it.
- It never certifies coverage. It has no capture or Oracle certificate, and
  the job card says so.
- It holds for review (`TAIL_RESUME_POINT_NOT_RETAINED`) if the terminal no
  longer returns the serials it resumed from. That happens when the log was
  cleared, the terminal was replaced, or serials were renumbered.
- It is exempt from the history-backlog intake gate, because it adds at most one
  20-record page per ESP poll cycle.
- Between its 15-second checks it holds no device scan slot. Following one
  terminal for days therefore never delays another device's reconciliation.

The Hikvision preflight now treats the terminal as reachable when the ESP's last
check reached the event search with HTTP 200 (`poll_stage=events`). Firmware
reports such a terminal as `OFFLINE` while its anchor mismatches.

Start it from **Reconciliation → Reconcile attendance → History scope → Recent
tail**. It needs an audited reason, the administrator password, and the typed
confirmation `RECONCILE <device_id> RECENT TAIL`.

## LF-ZONE-BLD1-01, October 2026

ESP `ac:27:6e:a3:0a:58`, firmware `3.1.1-bld1fix7`, terminal DS-K1T341AMF
G04600476.

- **18 Oct, 18:59 PKT:** the last poll committed cursor 597801. The terminal
  then stopped answering until 07:08 PKT the next day.
- **From 19 Oct, 07:08 PKT:** every poll reports `HIK_ANCHOR_MISMATCH` at
  cursor 597801. The terminal now returns a different record at the saved
  cursor, so the ESP keeps its checkpoint and never advances.
- **The bld1fix7 fallback stream** delivered only the retained-log
  retransmission (`currentEvent=false`): 49,514 replayed records and zero
  current ones, with serials rising from about 466,000 to 580,000 between 19 and
  21 Oct. This is the same behaviour recorded for the DS-K1T342EFWX in
  `hikvision-ds-k1t342efwx-v3.3.5-observations.md`. The replay is what added
  July to September punches as `FULL_HISTORY`.
- **Every replayed record is written to the ESP's source queue.** The ESP has
  1,042 failed writes. Each one aborts the stream with `durable_storage_failed`,
  and the stream then reconnects. Its `capacity_admission` error 28 latched 14
  seconds after the ESP rebooted at 08:01 PKT on 20 Oct, while storage was only
  4.2% used.
- **The ESP clock is 4,628 seconds slow.** The terminal clock is correct. The
  apparent "terminal +77 minutes" and "no stream since 16:47 PKT" both come from
  the ESP clock. The last stream message really arrived at 18:04 PKT on 21 Oct.
  ADD now measures this offset from the heartbeat envelope and raises
  `ESP_CLOCK_SKEW`. Do not change the terminal clock.
- **The ESP showed OFFLINE although its heartbeats were current.** When an ESP
  reconnects, the old socket's cleanup could still record the connector as
  disconnected. Only the current socket now records a disconnection.
- **22 Oct, 17:24 PKT:** the ESP came back (boot `f9dc7485`) after about 15.5
  hours of silence. Its clock is now within 1 second of ADD, and its storage
  reports healthy with no error code. The anchor mismatch is unchanged: all 110
  reports between 21 Oct 16:20 and 22 Oct 17:47 PKT, before and after the
  reboot, carry the same expected and observed digests. The terminal therefore
  keeps returning one fixed, different record at serial 597801; the mismatch is
  not transient. The stream is replaying late-September history again. On each
  reconnect, about every 40 seconds, the connector reads connected for about
  0.5 seconds and then disconnected, which is the race fixed here.

Recovery: run **Recent tail** once. It resumes at serial 597782. If the terminal
reused serials 597794 to 597801 after the power loss, those serials are held for
review, and every later punch is ingested.

## Firmware follow-up (targeted USB build, not yet built)

- Treat a verified anchor mismatch as a source exception, not a permanent stop.
  Report the observed stable facts digest, and accept an ADD-authorized
  re-anchor command behind step-up.
- Do not open the alert stream on these terminals, or never queue
  `currentEvent=false` records. ADD can already read retained history.
- Clear a latched Hikvision admission fault once a measured append succeeds.
- Check the ESP clock against ADD's authenticated time when SNTP disagrees.

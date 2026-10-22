# Hikvision recent-tail recovery

ADD can read a Hikvision terminal's retained event log after the last punch it
holds, then keep following the terminal tail. It is for a terminal whose own ESP
poll is blocked, for example by `HIK_ANCHOR_MISMATCH`. It needs no firmware
change. It uses the ESP's existing `hikvision_history_assignment` worker, which
every 3.1.x and bld1fix image runs, including while the poll is failing.

## What it does

- **Resume point.** It resumes 20 records before the highest serial ADD holds
  through gap-free custody: the ESP's checkpointed `POLL` channel, or an
  earlier recent-tail job that read contiguously from at or before that point.
  Live-stream records do not count, because a stream can drop events while it
  is disconnected. Employee-filtered history does not count either, because it
  is not contiguous.
- **Poll breaks.** The poll reads every event serial in order. If a serial is
  missing below the poll's newest record, the ESP's checkpoint restarted
  further on, for example after a reinstall or a reset. ADD resumes at the
  latest such break, so serials the poll never read after it are not skipped.
  The dialog lists up to five earlier breaks; the tail does not cover those,
  only a full terminal audit does. A break that is only a gap in the
  terminal's own numbering costs a longer re-read, nothing more.
- **Proving continuity.** The earliest record of the first page must be one ADD
  already held with the same facts. If it changed, or ADD never held it, the
  terminal may have reused serials from below the resume point. The job then
  steps back one 20-record page, up to 10 times, until a record matches, and
  shows where it found continuity. If none matches it holds for review with
  `TAIL_REUSE_BEFORE_RESUME_POINT`. If the terminal no longer returns the
  resume serials at all (log cleared, terminal replaced, serials renumbered) it
  holds with `TAIL_RESUME_POINT_NOT_RETAINED`.
- **Paging.** Each page asks for 20 records from the last serial already seen,
  up to the maximum serial. The terminal may treat `beginSerialNo` as inclusive
  or as exclusive; either way no record is skipped. A page that says more
  records follow but brings no new serial holds the job
  (`TAIL_PAGE_NO_PROGRESS`).
- **Counts.** "New records" counts only serials after the resume point. The
  re-read records up to it are reported separately as "held records re-read".
- **Evidence and attribution.** Records go through the normal evidence and
  attendance path. Each source event has
  `event_uid = (terminal serial, source epoch, serialNo)`. That means:
  - a punch already in ADD, from the poll, the stream replay or a later
    re-anchored poll, is never delivered twice;
  - a serial whose facts changed is held as `SOURCE_FACT_CONFLICT`, together
    with the record ADD already had. It is never merged and never attributed
    to another person. The new punch at that serial creates no attendance row;
    it stays as evidence for the owner's review.
  - an earlier version that Oracle already confirmed keeps its status, because
    ADD cannot withdraw it from Oracle; its evidence row is still marked
    `SOURCE_FACT_CONFLICT`, and the job counts it. Unconfirmed earlier versions
    are held as `QUARANTINED_SOURCE_CONFLICT`.
- **Delivery.** Records after the resume point use the `CURRENT_RECONCILE` ORDS
  lane, not the background history lane, so they do not queue behind the
  device's older history. Their source stays `FULL_HISTORY` (Oracle capture
  type `MANUAL_REPROCESS`) and their clock quality is `UNKNOWN`, because no
  contemporaneous clock sample travels with a history page. Punches whose
  identity resolves are sent normally. Punches held as `BLOCKED_IDENTITY` need
  the manual force-release run: the standard identity release refuses any
  clock other than `OK`.
- **Following.** Once it has caught up, it checks every 15 seconds. A check
  that finds nothing new adds no page row and does not count as progress; the
  job card's last check time shows that it is still checking.
- **Ending.** It ends with outcome `LIVE_POLL_RESTORED` only on positive proof
  that the ESP's own poll captures again: a fresh heartbeat with `poll_error=0`,
  `poll_stage` `events` or `checkpoint_commit`, HTTP 200 and no consecutive
  failures; a `last_successful_poll_epoch` that advanced between two checks; a
  durable cursor at or below the job's last serial; and a `POLL` record beyond
  anything the stalled poll could have read. The heartbeat an ESP sends right
  after a boot (`poll_error` still 0, cursor still the mismatching one) proves
  nothing. Firmware that does not report `consecutive_poll_failures` never
  ends it automatically. An administrator can also cancel it.
- **No certification.** It never certifies coverage. It has no capture or
  Oracle certificate, its assurance outcome is `NOT_CERTIFIED`, it reports no
  cutoff serial, and the job card says so.
- **Backlog.** It is not paused by the history-backlog intake gate, because it
  is the device's only capture path while its poll is blocked. While that
  backlog is over its pause limit, a catch-up reads at most one page every 15
  seconds.
- **Scan slots.** Between its checks it holds no device scan slot. Following
  one terminal for days therefore never delays another device's
  reconciliation.
- **One job per device.** While it runs, no other reconciliation can start on
  that device, and an attendance repair that needs a full-device source
  certificate stops with `SOURCE_DEPENDENCY_UNAVAILABLE` instead of waiting on
  it. Cancelling the tail to run a full audit stops capture until the audit or
  a new tail runs.

Only the recent tail may start against a terminal the ESP reaches at the event
search with HTTP 200 (`poll_stage=events`), which firmware reports as `OFFLINE`
while its anchor mismatches. A full terminal audit or active-user history still
waits until the terminal is `ONLINE`.

Start it from **Reconciliation → Reconcile attendance → History scope → Recent
tail**. It needs an audited reason, the administrator password, and the typed
confirmation `RECONCILE <device_id> RECENT TAIL`.

## LF-ZONE-BLD1-01, October 2026

ESP `ac:27:6e:a3:0a:58`, firmware `3.1.1-bld1fix7`, terminal DS-K1T341AMF
G04600476.

- **18 Oct, 18:59 PKT:** the last poll committed cursor 597801. The terminal
  then stopped answering until 07:08 PKT the next day.
- **From 19 Oct, 07:08 PKT:** every poll reports `HIK_ANCHOR_MISMATCH` at
  cursor 597801. The first record the terminal now returns from the saved
  cursor has a different digest, so the ESP keeps its checkpoint and never
  advances. Either serial 597801 now carries different facts, or the saved
  serials are missing and a later serial is returned first; the logs cannot
  tell these apart, and the tail handles both.
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
  The "+77 minutes" figure came from the firmware's own `HIK_CLOCK_SAMPLE`
  log; ADD discarded those samples as stale. ADD now measures the ESP offset
  from heartbeat envelopes. It acts only when the two smallest of the last
  three measurements agree within 30 seconds, because a heartbeat that waited
  in a queue only adds to its measured offset. It then raises `ESP_CLOCK_SKEW`
  and re-bases the terminal clock sample. Do not change the terminal clock.
  Per-punch clock evidence still uses the ESP clock (the 18 Oct `LIVE_POLL`
  rows show `clock_drift_seconds=1969`, `DRIFTED`); that is not corrected here.
- **The ESP showed OFFLINE although its heartbeats were current.** When an ESP
  reconnects, the old socket's cleanup could still record the connector as
  disconnected. Only the current socket now records a disconnection, and if
  the newer socket registers while that write runs, ADD records the connector
  as connected again.
- **22 Oct, 17:24 PKT:** the ESP came back (boot `f9dc7485`) after about 15.5
  hours of silence. Its clock is now within 1 second of ADD, and its storage
  reports healthy with no error code. The anchor mismatch is unchanged: all 110
  reports between 21 Oct 16:20 and 22 Oct 17:47 PKT, before and after the
  reboot, carry the same expected and observed digests. The terminal therefore
  keeps returning one fixed, different record at serial 597801; the mismatch is
  not transient. The stream is replaying late-September history again. On each
  reconnect, about every 40 seconds, the connector reads connected for about
  0.5 seconds and then disconnected, which is the race fixed here.
- **22 Oct, 19:27 PKT:** unchanged. The poll still fails at cursor 597801 with
  HTTP 200 at the events stage. The stream has delivered 716
  replayed and zero current records since the reboot. On this boot the ESP has
  already logged `durable_storage_failed` again (17:30 and 17:32 PKT, three
  failures), and its source queue reached 2,486 records. Storage still reports
  `HEALTHY` with 0.77 of 7.7 MB used. The replay, not the punches, is what fills
  the queue.

Recovery: run **Recent tail** once. The dialog shows the resume point. It is
597782 only if ADD's `POLL` records for BLD1 have no break near 597801; this
has not been checked against the production database. If the terminal reused
serials after the power loss, those serials are held for review, the job steps
back until it finds a record ADD held unchanged, and every later punch is
ingested. Check that the job reports new records from 19 Oct onwards. A tail
that catches up with zero new records while the site was punching can mean the
terminal numbered its new events below the resume point. Do not treat that as
"nothing missing": escalate it for a full terminal audit (cancel the tail
first; capture stops until the audit or a new tail runs).

On 22 Oct the last 500 `LIVE_POLL` punches from BLD1 (15 to 18 Oct) were 497
`BLOCKED_IDENTITY`. Recovered punches will mostly be held the same way and
need the manual force-release run to reach Oracle; they keep their original
punch times.

## Firmware follow-up (targeted USB build, not yet built)

- Treat a verified anchor mismatch as a source exception, not a permanent stop.
  Report the observed stable facts digest, and accept an ADD-authorized
  re-anchor command behind step-up.
- Do not open the alert stream on these terminals, or never queue
  `currentEvent=false` records. ADD can already read retained history.
- Clear a latched Hikvision admission fault once a measured append succeeds.
- Check the ESP clock against ADD's authenticated time when SNTP disagrees.

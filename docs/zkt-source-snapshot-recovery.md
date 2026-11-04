# Bounded historical source snapshot recovery

A failed prepared-buffer read does not establish a changed terminal source. Historical reconciliation now distinguishes incomplete transport from successfully observed malformed framing, count regression, or changed anchor bytes.

The source header must still match the count exactly before any record or anchor can proceed. This includes zero: a nonempty payload cannot certify an empty terminal. Length arithmetic rejects truncated or overflowing header declarations.

If a complete, valid envelope disagrees with the count sampled before preparation, firmware releases the prepared buffer and performs one fresh count request. It recognizes possible append growth only when exactly one supported record width puts the prepared count within the nonregressing before/after interval and at or beyond both the immutable cutoff and committed cursor. For example, counts 68,788 before preparation and 68,790 afterward can bracket a prepared snapshot containing 68,789 40-byte records.

That result permits a retry only. It sends no records, anchor or custody claim, and never changes the source epoch, cutoff, ordinal or chain. The next offered assignment must prepare again and verify the same first anchor and committed predecessor. Ambiguous widths, malformed framing, observed count regression and successfully read digest mismatches retain their existing safety holds.

## 49-byte records

ZKTeco MB40-VL/ID terminals on platform ZAM180_TFT (LHRCENTRAL-01/02/03, 4 Nov 2026) return 49-byte attendance records: the standard 40-byte record followed by a 9-byte tail, `ff 32 35 35 00 00 00 00 00` (0xff, ASCII "255", zeros; six of 25,854 observed records start the tail with 00). Firmware before 2.8.2 rejected those logs with `SOURCE_RANGE_LAYOUT_INVALID` (for example `prepared=651361 payload=651357 count_before=13293`, 13,293 x 49).

Firmware and ADD accept 49 wherever they accept 40. Identity, time, status and record UID come only from the 40-byte prefix. Raw digests, anchors, terminal record keys, base64 uploads and ADD's length checks keep all 49 bytes. The width is chosen only when the payload is exactly the sampled count times 49, so it is never a guess between widths. A 49-byte match is ambiguous only when the payload is also a whole number of 8-byte units, which an 8/16/40-byte log that grew after the count could produce (1,225 40-byte records counted as 1,000 is 1,000 x 49). Then the first record read at the 49-byte stride must carry the observed tail, otherwise the attempt is retried with `SOURCE_RANGE_SNAPSHOT_RETRY`. The append-tail read and the source probe apply the same check. During append recovery, 8/16/40 decide exactly as before: a 49-byte width alone explains growth, and one beside a single 8/16/40 width is still retried, naming no width.

Incomplete header, first-anchor or predecessor reads emit `SOURCE_RANGE_READ_RETRY`; explained append growth emits `SOURCE_RANGE_SNAPSHOT_RETRY`. Failure to confirm `FREE_DATA` emits `SOURCE_RANGE_RELEASE_RETRY` and shuts down the terminal socket, so uncertain prepared state is not reused. Anchor, chunk and probe delivery also require confirmed buffer release.

ADD's existing `TRANSIENT_STEP_FAILED` release retains its committed checkpoint and applies 5-, 15-, 30- then 60-second retry delays, capped at 60 seconds. A replayed release does not consume another retry. These diagnostics cannot clear an existing hold. There is no new total-attempt cap: each offered attempt contains at most one recount, while the existing scheduler, transport deadlines and assignment credit bound work and pace retries.

The actual packet transport still preserves interleaved live observations before acknowledging them. A source-read retry cannot substitute for that obligation. Failed local preservation makes the transport read fail without acknowledging the interleaved punch.

The field `SOURCE_RANGE_LAYOUT_INVALID` observation which prompted this change contained no header, length or read-result context. The recorded model is MB40-VL/ID and earlier committed source ranges used 40-byte records. Those facts do not establish which failed predicate caused that field event. Synthetic control-flow tests reproduce the failure modes; they are not independently checked model fixtures or a field qualification claim. No existing held job is automatically retried by this change.

Validation exercises the production C precheck and dispatch under ASan/UBSan: transport loss at each evidence read, append races at 40/16/8 bytes, a second append before recount, repeated growth, ambiguous widths, regression, malformed/truncated/overflowing headers, zero-count growth, failed recount/release, genuine anchor mismatch and unchanged state with no sends. The production prepared transport tests cover interleaved live-punch preservation during header reads. Backend tests verify retry pacing, lost-response replay, unchanged checkpoints, and persistent holds.

# Protected ZKT bench fixture comparison

The [DBA/bench handoff](zkt-2.7.0-qualification-handoff.md) requires independent
expected punch facts for all six installed models. This checker makes the
record-layout and clock portion repeatable. It runs the actual production C
record, live-record and clock functions under AddressSanitizer/UndefinedBehaviorSanitizer,
and the actual ADD decoder, against the same retained bytes. Expected results
come from a separate supplied file; neither decoder generates them.

This is a local command, with no ADD/Oracle connection, terminal commands,
signing, qualification import or activation route. `comparison=PASSED` means
the submitted cases agree. It does not establish independent collection,
authenticate a reviewer, qualify a model, prove identity, or authorize a release.
The report always retains `profile_qualification=NOT_ASSERTED` and
`activation_authority=NONE`.

## Run and preserve evidence

Use the repository Python development environment and a host C compiler with
ASan/UBSan support on Linux or macOS. Keep the bundle in a protected directory
outside the public repository. All paths in a bundle must be relative to its
manifest directory; referenced regular files must match their SHA-256. Symlinks,
special files, absolute/traversing paths, duplicate JSON keys, extra fields,
missing/duplicate cases, invalid coordinate types and oversized files fail.

```sh
.venv/bin/python scripts/check_zkt_bench_bundle.py \
  /protected/zkt-bench/g3/bundle.json \
  --output /protected/zkt-bench/results/g3-comparison-001.json
```

Choose a new output filename for every run. It is created with mode `0600` and
never overwrites an existing report, input or symlink. Console output contains
only overall status. Reports include case identifiers, hashes, comparison
failures, tool/runtime versions and exact decoder/adapter source hashes, but no
raw bytes, decoded user references, punch times, terminal serial or people names.
Keep reports protected too: digests and identifiers are still evidence metadata.

Exit codes: `0` all submitted comparisons passed, `1` comparison mismatch, `2`
invalid/inaccessible evidence, compiler/probe failure or report failure. A crash,
timeout or sanitizer failure is never an expected decoder rejection. Preserve
failed reports and the unchanged bundle. Review all exit codes; a partial report
is not a successful result.

## Bundle contract

The full strict schemas are `Bundle` and `Expected` in
`scripts/check_zkt_bench_bundle.py`. Hash placeholders below must be replaced by
the digest of each actual file. Do not fabricate raw bytes or expected facts to
make a bench comparison pass. Public regression fixtures remain synthetic and
are labelled `SYNTHETIC` / `SYNTHETIC_SPECIFICATION`.

```json
{
  "schema_version": 1,
  "bundle_id": "g3-bench-001",
  "evidence_kind": "BENCH_CAPTURE",
  "model": "G3",
  "terminal_serial": "BENCH-EXAMPLE",
  "terminal_firmware": "actual terminal firmware identifier",
  "captured_at": "2026-10-03T04:14:23Z",
  "collector_id": "collector-reference",
  "reviewer_id": "independent-reviewer-reference",
  "terminal_evidence": {"path": "terminal-evidence.txt", "sha256": "<actual sha256>"},
  "expected": {"path": "expected.json", "sha256": "<actual sha256>"},
  "cases": [
    {
      "case_id": "source-001",
      "kind": "SOURCE_RECORD",
      "record_size": 40,
      "source_epoch": "c5722c99-f5b0-49ea-afd1-2b814cd71a17",
      "ordinal": 0,
      "raw": {"path": "source-001.bin", "sha256": "<actual sha256>"}
    },
    {
      "case_id": "live-001",
      "kind": "LIVE_PACKET",
      "record_size": 32,
      "expected_session": 23,
      "raw": {"path": "live-001.bin", "sha256": "<actual sha256>"}
    }
  ]
}
```

Use the exact installed model name: `G3`, `SilkBio-101TC/ID`, `MB40-VL/ID`,
`uFace800`, `uFace800/ID`, or `uFace800 Plus/ID`. The name selects the comparison
profile only. The retained terminal evidence must identify model, serial,
firmware, capture procedure, source coordinates, live session and clock evidence
at collection time. The checker hashes that document; an external reviewer
must verify its contents and origin. Editable current ADD labels are insufficient.

Source cases contain exactly one 8-, 16- or 40-byte record with its source epoch
and ordinal. Live cases contain a complete stored packet, including the 8-byte
ZK protocol header, and declare an explicit 12-, 32-, 36- or 52-byte record
layout and expected session. The supplied layout is a hypothesis, not a trusted
selection. Header command/session and complete record boundaries are compared;
the adapter does not qualify TCP assembly, live session establishment, checksum
rules, prepared-buffer transfers or the embedded socket driver. Those remain
separate protocol tests. If the same bytes pass multiple supplied live layouts,
the report explicitly lists `multiple_passing_live_layouts`; it never chooses
one. Unsubmitted alternative layouts have not been ruled out by this command.

`expected.json` uses the exact case set. An independent reference document
records how expected facts were obtained; existing ADD interpretation alone
cannot be that source. `recorded_by` must match the declared reviewer and differ
from the collector. These names are provenance declarations, not signatures.

```json
{
  "schema_version": 1,
  "method": "INDEPENDENT_OBSERVATION",
  "recorded_by": "independent-reviewer-reference",
  "recorded_at": "2026-10-03T05:00:00Z",
  "reference": {"path": "independent-observations.txt", "sha256": "<actual sha256>"},
  "cases": [
    {
      "case_id": "source-001",
      "outcome": "ACCEPT",
      "facts": [{
        "user_id": "000123",
        "attendance_uid": 456,
        "encoded_time": 859972462,
        "local_time": "2026-10-03T09:14:22+05:00",
        "utc_time": "2026-10-03T04:14:22Z",
        "status": 7,
        "punch": 2,
        "offset": 0,
        "length": 40
      }]
    },
    {"case_id": "live-001", "outcome": "REJECT", "facts": []}
  ]
}
```

These illustrative expected facts are not a real terminal capture. For an
accepted live packet, supply every expected record in byte order, including
equal same-second records. Its first offset is 8. Source offset is 0. Empty
expected arrays are only valid for rejected inputs. Status, punch, original
encoded time, exact user reference, historical attendance UID, offsets, lengths,
Pakistan local time and UTC time are compared independently. No user identity
or duplicate is inferred from a historical attendance UID. Eight-byte source
records retain `user_id: null`; 16-byte source and all live records retain
`attendance_uid: null`. Leading zeros in textual references remain significant.

Each manifest/metadata file is at most 4 MiB, each raw input at most 65,536
bytes, with at most 128 cases and 16 MiB of total input. Compilation and each
probe have separate time limits. The checker pins source hashes before and after
the run and refuses a changed checkout. Do not modify the checkout during a run.

## Handoff result

Return the unchanged protected input bundle, every comparison report, original
capture/independent-observation evidence and the reviewer's findings. Exercise
ordinary and repeated punches, leading-zero references where supported, roster
changes, partial final ranges, malformed inputs and every applicable explicit
layout. Record unsupported features rather than inventing them. Run each of the
six models separately. A small passing case set is not complete model coverage.

Successful independent review, remaining framing/session qualification and a
trusted activation mechanism are still prerequisites for consuming these facts
as canonical attendance. Identity evidence, live/source one-to-one matching,
Oracle delivery, migration, capacity, soak, signed firmware and field HIL remain
separate release obligations. Physical power/endurance stay `NOT_PERFORMED`.

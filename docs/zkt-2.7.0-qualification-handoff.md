# ZKT 2.7.0 external qualification procedures

These procedures define how to obtain missing evidence. They are not passing
test results and do not replace unfinished implementation. The
[implementation register](zkt-2.7.0-implementation.md) remains the source of
software status. Production Oracle access stays read-only under the user's
instruction: no production data changes. No field connector is activated by
this document.

## Evidence needed from outside the development environment

| Requirement | External input | Work the implementation agent can perform once supplied | Completion evidence |
|---|---|---|---|
| Matching Oracle execution and HTTP delivery | A DBA-created isolated Oracle 19c/ORDS environment with synthetic data, identified versions and approved test credentials | Compile the reviewed reader, exercise HTTP authorization, concurrent delivery and lost-response recovery; compare immutable raw content and daily results | Exact source/image hashes, engine and ORDS versions, test report, raw/result digests and verified isolation from production writes |
| Installed terminal layouts | An identified terminal for each installed model and independently recorded expected punch facts; protected raw source/live captures | Reproduce every fixture, compare firmware/ADD decoders, exercise ambiguity and framing faults, derive correction proposals | Fixture hashes, capture-time terminal/model/firmware/session evidence, independent expected results and reviewer identity |
| Per-zone capacity and field recovery | Complete source coverage and load observations, actual partition/retained-data measurements, eligible reachable connectors | Calculate capacity, run approved remote tests, trace ordinary punches and collect HIL results | Per-device capacity report, exact boot/image evidence, custody/source/Oracle traces and observed recovery timings |
| Physical power and endurance | A bench operator, spare or test hardware and controlled measurement equipment | Provide the fault matrix and analyze the resulting storage, reboot and trace evidence | Measured power-cut/brownout/endurance report; otherwise `NOT_PERFORMED` |

Access to production APEX supplies none of the permissions or isolation in the
first row. A signed image, a working network link or a successful short test
supplies none of the independent evidence in the other rows.

## Oracle 19c and ORDS

1. The DBA identifies the non-production database/PDB, engine patch level,
   character set, time zone, ORDS version and network boundary. Use synthetic
   employees and attendance only. Do not clone personal attendance into an
   unrestricted test system. Test credentials must have no production access;
   test network routes must not reach the production writer.
2. Preserve the reviewed reader and fixture hashes before adapting any test
   setup to that PDB. The current Docker runner accepts only its labelled,
   internally networked disposable container. It must not be modified to
   accept the production APEX connection. Its existing Oracle Free result is
   recorded separately from a 19c result.
3. Install the reader and separately configured test ORDS route only in that
   isolated environment. Run the synthetic projection cases, then test the
   actual HTTP route with correct, missing and incorrect credentials. Installation
   is a schema change even when a package's business logic is read-only: Oracle
   DDL implicitly commits. A surrounding `ROLLBACK` is not a production-safe
   installation strategy. [Oracle transaction documentation](https://docs.oracle.com/en/database/oracle/oracle-database/19/cncpt/transactions.html)
4. Exercise the real test writer and verifier together: missing row, matching
   row, immutable mismatch, duplicate identity, daily processing pending,
   same-second distinct occurrences, raw-only data, clock/locale differences,
   connection failure, commit followed by lost response and concurrent claims.
   Verify that a retry keeps the same payload and key, and that only one
   canonical occurrence and one committed receipt survive.
5. Verify raw content and daily results within one SQL statement snapshot.
   Separate successful queries can observe different committed moments under
   read committed isolation. Preserve the proof's observation time and scope;
   do not present it as a guarantee that later writers cannot change a row.
   [Oracle read consistency documentation](https://docs.oracle.com/en/database/oracle/oracle-database/19/cncpt/data-concurrency-and-consistency.html)
6. Preserve database/route diagnostics, source hashes, input/output counts and
   failed cases. Keep leave, holiday, roster, payroll and effective-status
   business-policy review separate from the implemented raw/daily-time proof.
   No production package repair or route installation is part of these tests.

The existing files and known scope are documented in
[ZKT Oracle delivery](zkt-oracle-delivery.md). A blank APEX output, successful
anonymous compilation or Oracle Free pass cannot be recorded as a complete
19c/ORDS delivery pass.

## Terminal profile and source interpretation

Use G3, SilkBio-101TC/ID, MB40-VL/ID, uFace800, uFace800/ID and uFace800 Plus/ID
as distinct qualification subjects. Record capture-time model/firmware evidence;
an editable current ADD label alone does not identify a historical capture.

On an isolated terminal, record expected user reference, local time, status and
punch independently before comparing source bytes and live packets. Include
ordinary punches, repeated same-second punches, leading-zero user references,
roster changes and partial final ranges. Do not create synthetic attendance on
a production terminal to obtain a fixture. Ordinary production attendance may
be observed without changing it.

Keep original bytes, serial, source epoch/ordinal, packet/session boundaries,
time-zone evidence and SHA-256 together in protected storage. Preserve field
exceptions with their original classification. Commit only sanitized synthetic
vectors to the public repository; document any transformation so the original
failure remains reproducible. Existing ADD interpretations are comparison
evidence, not independent ground truth.

A profile passes only when the expected facts and both decoders agree for its
explicit layouts, framing and time conversion, including negative cases. An
ambiguous layout remains held. Historical attendance UID fields never become
current enrollment identity by assumption. Decoder changes add derived
correction evidence and retain original bytes and classifications. Employee
attribution still needs its own historical identity evidence.

The [protected bench bundle checker](zkt-bench-bundle.md) supplies the repeatable
record-layout/clock comparison and evidence format. It runs both production
decoders against independently supplied expectations, preserves hashes and
reports failures without printing punch facts. Its passing comparison does not
authenticate provenance, qualify transport/framing or activate a profile.

## Capacity, migration and elapsed observation

Measure the approved 30-day per-zone source-occurrence baseline, including
coverage gaps and same-second multiplicity. Retry attempts do not count as new
punches. An unknown or incomplete baseline cannot produce zero demand or a
capacity pass. Use twice the measured peak daily volume for seven days, actual
encoded/encryption cost, retained legacy data, checkpoints and recovery reserve.
Use the existing capacity evaluator with measured physical partition inputs;
do not substitute an already adjusted filesystem total for partition size.

SPIFFS has a constrained usable capacity and garbage collection can take
seconds. Keep the approved approximately 75% partition budget and measure
latency under retained-data pressure on the actual hardware.
[Espressif SPIFFS documentation](https://docs.espressif.com/projects/esp-idf/en/v5.5.3/esp32s3/api-reference/storage/spiffs.html)

Before the writer is enabled, every retired legacy item needs a matching
durable destination receipt, a restartable migration position and a compatible
bridge in the other OTA slot. Bind the proof to the exact image digest,
partition, security configuration, terminal and retained journal. Exercise
interruption at each migration/selection boundary. ESP-IDF first-boot validation
and automatic rollback are distinct from application data-format compatibility;
the application must establish both.
[Espressif OTA documentation](https://docs.espressif.com/projects/esp-idf/en/v5.5.3/esp32s3/api-reference/system/ota.html)

Run the complete seven-day automated soak on the final applicable software and
preserve its time series, restart history and input-to-disposition accounting.
The repository's legacy host queue soak is a component test with simulated
ports; repeating it or changing its label does not supply a seven-day full
2.7.0 integration result. A changed candidate gets a new artifact identity and
repeats affected gates.

After the software and compatibility gates, use the approved A–D waves with
at most two simultaneous upgrades and one per location. An online ESP with an
offline terminal has not passed capture readiness. Retain all 17 active ZKT
connectors in the status register, even while offline or blocked. Per device,
collect at least 20 ordinary source-to-Oracle traces over at least two working
days, plus the wave's minimum observation period. Fourteen-day fleet observation
starts after the final successful wave. Elapsed evidence cannot be replaced by
a shorter scripted run.

## Physical tests and final record

Remote ESP reboot and a bounded, automatically expiring ADD interruption can
test software recovery. Physical supply loss during writes, brownout behavior
and flash endurance need a bench operator and hardware measurements. Keep their
status `NOT_PERFORMED` until that evidence exists. The approved remote HIL and
full production qualification are separate outcomes.

For each gate retain: exact scope and artifact hashes; environment and tool
versions; expected input population; start/end times; observed outcomes and
failures; evidence hashes; reviewer; limitations; and `PASSED`, `FAILED`,
`BLOCKED` or `NOT_PERFORMED`. Never convert missing evidence into `PASSED`.
No exception in this procedure authorizes production attendance modification,
invented identity, force-send, deletion or an incompatible rollback.

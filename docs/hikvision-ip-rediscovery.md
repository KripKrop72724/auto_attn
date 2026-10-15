# Hikvision IP recovery

After three consecutive identity failures, Hikvision firmware issues a read-only
SADP inquiry on the ESP's active Wi-Fi interface. Inquiries run at most once every
30 seconds, with a two-second receive window and bounded packet sizes.

A reply must echo the fresh request UUID, advertise the saved serial (SADP may
prefix it with model/build information), and originate from its advertised IP
and SADP port. The IP must be a usable private address on the ESP's current
subnet, excluding the ESP, gateway, network and broadcast addresses.

Before changing configuration, the ESP sends an ISAPI device-info GET using the
saved protocol, port, credentials and TLS trust. It requires a Digest challenge
and authenticated success with an exact match to the original ISAPI serial.
Discovery cannot change serial bindings, credentials, ports, roles or terminal
users. A failed candidate leaves the active host unchanged.

The new host becomes active only after encrypted NVS commit and readback. The
existing terminal heartbeat reports this address, so ADD's existing device views
update without a new API object format. `HIK_IP_REDISCOVERED` records the change;
`HIK_IP_SAVE_PENDING` records a persistence failure. No credentials are logged.

SADP requires the ESP and Hikvision terminal on the same subnet, with multicast
239.255.255.250 UDP 37020 permitted. It does not cross routers or VLANs. It cannot
recover a disconnected terminal, changed credentials or a changed serial binding.

BLD1 receives this feature in targeted USB updater 1.0.6, firmware
`3.1.1-bld1fix6`. Its existing permanent, revocable write grant remains bound to
the same connector, ESP MAC, zone and serial. Administrator role writes remain
disabled. Generic Hikvision images do not receive the BLD1 exception.

The supervisor reserves 8 KiB instead of the ZKT parser's 24 KiB stack, starts
evidence delivery before scans, and retries failed mutex/queue/task allocations.
`HIK_WORKER_START_PENDING` reports a deferred task and available internal heap.

Validation includes sanitized native C tests for malformed/spoofed packets,
authenticated candidate requests, simulated IP changes, failed NVS operations,
rate limiting and socket cleanup. The packaged Windows executable runs signature,
payload and GUI smoke tests. Physical recovery must be verified after USB install.

## BLD1 attendance recovery 1.0.7

The exact BLD1 repair image (`3.1.1-bld1fix7`) can open an independent, read-only
ISAPI alert stream when its retained-history anchor blocks polling. It verifies
the saved serial with Digest authentication before subscribing. The stream owns
its client, does not overwrite poll HTTP diagnostics, and allows 45 seconds for
idle heartbeats. It reconnects indefinitely without a trial expiry.

Live metadata enters the existing durable source queue and ADD receipt path.
Original history checkpoints, source bindings and all attendance storage remain
intact. New anchor diagnostics log only cursor and hashes, never event identities.
ADD still requires an operator-approved event mapping, valid identity/time and
durable custody before attendance can be released. A source conflict remains held.

ADD reports a live connection while history requires review only when recent
durably captured stream metadata, an open error-free stream, and healthy source
storage provide evidence. The history warning stays visible. A stream connection
is not proof of an attendance row or Oracle delivery; verify one real punch after
installation. This fallback is compiled only into the targeted BLD1 repair image.

## Stable history anchors

Checkpoint version 2 hashes event serial, major/minor code, exact employee number
and offset-normalized time. Changes to display names, pictures and JSON field
order do not change this hash; changes to attendance facts still block the source.
One predecessor is requested and excluded from delivery to accommodate inclusive
and exclusive `beginSerialNo` implementations without dropping the saved anchor.

A legacy checkpoint migrates only when its original raw hash still matches the
returned record, or an exact BLD1 proof matches both the saved raw hash and the
returned immutable facts. That proof is bound to the physical ESP MAC, zone,
device, terminal, source epoch and cursor 593441. It was derived from independently
read ADD evidence 35557; it contains hashes, never employee text. The original
checkpoint is backed up in encrypted NVS before committing its replacement. A
failed backup, append or commit leaves the original poll checkpoint active.

Native checks use the pinned cJSON and mbedTLS implementation against independent
Python hash vectors. They cover metadata changes, changed facts, timezone
equivalence, leading zeros, malformed fields, exact proof scope, failed backups
and both serial-search conventions across 150,000 records each.

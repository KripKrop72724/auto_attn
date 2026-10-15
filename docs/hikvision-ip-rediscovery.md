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

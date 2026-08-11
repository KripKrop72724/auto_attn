# Zone Lite 2.4.8 captive portal request authorization fix (rejected canary)

Zone Lite 2.4.8 supersedes the unreleased 2.4.7 canary candidate. Physical
acceptance testing found that every portal route returned `404 Not found` even
though the protected setup AP, DHCP server, DNS responder, and HTTP listener
were running. The request guard relied on `getsockname()` returning
`192.168.254.1` for an accepted HTTP connection. ESP-IDF's HTTP server listens
on `INADDR_ANY`, and lwIP can retain that wildcard local address on an accepted
socket, causing the legitimate setup client to fail closed.

The guard attempted to verify both that the peer is in the dedicated
`192.168.254.0/24` setup subnet and that its address belongs to a current DHCP
lease for a station associated with the ESP's protected, one-client SoftAP.
Physical testing subsequently proved that the live lease lookup races DHCP
bookkeeping: phones could sometimes load the page only after repeated refreshes,
while a computer continued to receive `404 Not found`. The candidate therefore
failed acceptance, remained exact-MAC `HIL_ONLY`, and must never be promoted to
`AVAILABLE` or rolled out to the fleet.

The 2.4.7 socket-capacity, captive-probe, deterministic scan, branded portal,
CSRF, test-before-save, encrypted atomic NVS, and rollback protections remain
unchanged. Zone Lite 2.4.9 supersedes this rejected immutable candidate.

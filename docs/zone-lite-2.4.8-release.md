# Zone Lite 2.4.8 captive portal request authorization fix

Zone Lite 2.4.8 supersedes the unreleased 2.4.7 canary candidate. Physical
acceptance testing found that every portal route returned `404 Not found` even
though the protected setup AP, DHCP server, DNS responder, and HTTP listener
were running. The request guard relied on `getsockname()` returning
`192.168.254.1` for an accepted HTTP connection. ESP-IDF's HTTP server listens
on `INADDR_ANY`, and lwIP can retain that wildcard local address on an accepted
socket, causing the legitimate setup client to fail closed.

The guard now verifies both that the peer is in the dedicated
`192.168.254.0/24` setup subnet and that its address belongs to a current DHCP
lease for a station associated with the ESP's protected, one-client SoftAP.
This accepts the real captive client while continuing to reject requests that
arrive through the station-side network, including the unusual case where that
network uses the same private subnet.

The 2.4.7 socket-capacity, captive-probe, deterministic scan, branded portal,
CSRF, test-before-save, encrypted atomic NVS, and rollback protections remain
unchanged. Publish 2.4.8 as exact-MAC `HIL_ONLY`, repeat physical portal and
rollback acceptance on the attached board, then pass the production canary
gate before marking the immutable release `AVAILABLE`.

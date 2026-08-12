# Zone Lite 2.4.11 dual-stack captive portal correction

Zone Lite 2.4.11 fixes the physical-HIL failure in the 2.4.10 captive portal
candidate. ESP-IDF starts the HTTP server as an IPv6 dual-stack listener when
IPv6 support is enabled. IPv4 setup clients are consequently returned by
`getpeername()` and `getsockname()` as IPv4-mapped IPv6 socket addresses. The
old request guard supplied an IPv4-sized buffer and interpreted that dual-stack
address as `sockaddr_in`, turning valid `192.168.254.x` clients into zero or
garbage addresses and rejecting their requests with `404 Not found`.

The request guard now uses `sockaddr_storage`, validates the reported address
family and length, and normalizes both native IPv4 and IPv4-mapped IPv6
endpoints. A request must terminate on the exact SoftAP address
`192.168.254.1`, originate in the dedicated setup subnet, and match the current
event-driven DHCP lease for the associated one-client WPA2 SoftAP. Native IPv6,
truncated addresses, STA-interface destinations, off-subnet clients, stale
leases, and disconnected clients remain fail-closed.

Release 2.4.11 only as an exact-MAC `HIL_ONLY` candidate first. Promotion remains
blocked until the attached board passes repeated root and asset loads without a
single 404 or raw resource response, captive probes, concurrent requests,
failed-credential rollback, reboot, and normal attendance-operation validation.

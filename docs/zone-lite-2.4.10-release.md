# Zone Lite 2.4.10 captive portal event-order hardening

Zone Lite 2.4.10 supersedes the reviewed-but-unpublished 2.4.9 candidate. It
retains event-driven client authorization while closing both possible ordering
edges between the Wi-Fi association callbacks and the DHCP assignment callback.

When DHCP is delivered before the queued connect callback, the handler confirms
the exact MAC against the Wi-Fi driver's current SoftAP association list and
caches the lease. The later same-MAC connect callback preserves that lease.
When DHCP is delivered after a disconnect, the same driver check finds no
current association and the event is rejected without mutating authorization.
A queued connect delivered after departure is ignored, and a queued disconnect
delivered after a same-MAC reconnect cannot erase the live lease. A replacement
client clears the prior lease immediately.

The association lookup occurs only once for a DHCP event. No associated-station
or DHCP table is queried by an HTTP request, so the intermittent 2.4.8 request
race is not reintroduced. Requests still require the dedicated setup subnet and
the exact current DHCP address, with a bounded 1.5-second wait when the first
captive request reaches the HTTP task just ahead of event delivery.

Release 2.4.10 only as an exact-MAC `HIL_ONLY` candidate first. Promotion remains
blocked until the attached board passes repeated page loads, assets and API
requests, concurrent captive probes, failed-credential rollback, reboot, and
normal attendance-operation validation.

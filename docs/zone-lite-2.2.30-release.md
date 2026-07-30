# Zone Lite 2.2.30 guarded release

Zone Lite 2.2.30 fixes boot-health confirmation on large terminals whose
initial verified user-table refresh can occupy the gateway loop for longer
than the normal two-minute recovery window.

The boot gate remains fail closed. It still requires:

- an authenticated ADD connection;
- a freshly persisted ADD identity catalog;
- an authenticated and online ZKT session;
- valid terminal user and attendance counts; and
- the complete recovery-stability interval to have elapsed.

The correction permits the elapsed stability interval to be evaluated while
the gateway state is still `RECOVERING`; it does not accept a merely
authenticated new session, an offline session, missing counts, or a missing
identity catalog. The existing 15-minute rollback deadline remains unchanged.

Rollout order remains SWAT HIL first, then one production zone at a time. A
zone is accepted only after its deployment succeeds, the terminal returns to
normal capture, counts are preserved, and its authoritative truth sweep is
observed.

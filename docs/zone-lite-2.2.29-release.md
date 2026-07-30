# Zone Lite 2.2.29 guarded release

Zone Lite 2.2.29 carries forward the catalog activation correction from
2.2.28 and requires both production preflight gates before an immutable HIL
candidate can be built:

- the 731-user catalog activation and rollback simulation;
- the Oracle reconcile-load guard, including replayed-window no-op behavior,
  ADD truth delegation, and the self-restoring DDL-only Oracle migration.

The Oracle package now skips daily-flag recomputation when a reconcile window
inserts, corrects, or deletes no events. Its `DATASYNC = 0` repair is also
limited to rows whose value would actually change.

Rollout remains SWAT-only until boot health, catalog generation, truth
delegation, attendance preservation, and Oracle delivery are all proven.

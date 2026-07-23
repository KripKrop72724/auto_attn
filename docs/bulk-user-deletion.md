# Durable bulk user deletion

ADD bulk deletion is a server-side job, not a browser loop. The operator selects up to
500 regular users from one certified terminal, records an audit reason, re-enters the
administrator password, and types `DELETE <count> USERS FROM <device_id>`.

## Safety and durability

- One job and one immutable item row are stored for every request and selected user.
- A connector accepts only one mutating command at a time. While a deletion job is
  active, unrelated terminal mutations are rejected.
- Each item revalidates the UID, user ID, privilege, lifecycle state, and terminal
  fingerprints immediately before dispatch.
- ADD sends one existing `DELETE_USER` command at a time. The next item is not started
  until the connector has reread the terminal and proved both user absence and an
  unchanged attendance count.
- Successful deletion retains the encrypted identity tombstone, command history, audit
  history, and all attendance events.
- Jobs survive API or worker restarts, expire after 24 hours, continue past isolated
  per-user failures, and expose exact succeeded, failed, canceled, expired, and pending
  counts.
- Cancellation skips only untouched items. An already-running deletion finishes its
  terminal verification before the job becomes terminal.
- Reusing an idempotency key with the identical request returns the original job;
  reusing it with different targets or reason is rejected.

Terminal administrators cannot be bulk deleted. They must first be deliberately
demoted through the existing verified update workflow.

## Operations

Incomplete jobs open a high-severity `USER_DELETION_JOB_INCOMPLETE` alert. Operators
should inspect each failed item, refresh the complete terminal snapshot, resolve the
reported precondition or device condition, and create a new job for only the remaining
users.

This feature reuses the already-certified Zone Lite `DELETE_USER` protocol. It does not
require a firmware release or OTA update.

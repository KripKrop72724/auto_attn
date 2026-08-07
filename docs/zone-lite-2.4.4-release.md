# Zone Lite 2.4.4 release

Zone Lite 2.4.4 makes ADD-owned reconciliation self-healing without weakening
its append-only evidence contract.

- Final partial ranges of 1–99 records receive exact durable credit.
- Expired or explicitly released assignments resume from ADD's checkpoint.
- A changed raw ordinal is confirmed through fresh bounded terminal buffers.
- Stable history changes create a preserved source epoch; parser drift does not
  rewrite immutable source evidence or create retroactive attendance.
- Operator state, backlog credit, automatic recovery and source-change evidence
  are visible in ADD without exposing raw bytes.

The release is coordinated: deploy migration/backend/frontend first with the
recovery feature gated, publish the exact signed firmware as `HIL_ONLY`, update
Karachi, resume its preserved 5,100 / 5,131 checkpoint, and require the full
canary and 24-hour soak before promotion.

# Staged HIL plans (one approval)

Owner decision, 21 October 2026: replace "the owner creates every HIL
campaign by hand" with one approval of an ordered, staged plan. ADD then
starts each stage itself.

## What the owner approves

Firmware → Campaigns → **Staged HIL plans** → **New staged plan**:

1. A HIL_ONLY ZKT release. Only exact targets in the release's signed
   `hil_targets` exposure (`_hil_targets`) can be chosen.
2. Ordered stages of one or two zones. A stage must fit the existing fleet
   guard: at most two unsettled upgrades nationwide and one per location
   (SLICTOWER 13FL and 3FL never share a stage).
3. **Preview exact plan**. ADD resolves each zone to its one exact device
   (connector, ESP MAC, terminal serial), runs the normal campaign
   preflight for every device, and refuses the plan if any zone already has
   an active or paused campaign or an unsettled installation.
4. The audited reason, the typed version and the administrator password.

The approval pins the release identity (release id, version, artifact and
application digests) and the device list. The pins are digested and sealed
with the fleet root secret; ADD re-verifies them on every step. Approvals
expire after 72 hours by default (1–72), stage timeouts default to 6 hours
(30 minutes – 24 hours). One plan is open at a time.

## What ADD does

Every ten seconds, under a PostgreSQL row lock (two API workers and
restarts are safe; campaign creation is idempotent):

- **Stage start.** If another installation holds the slots or the location,
  or a stage device is offline, the stage waits (shown as the waiting
  reason) up to the stage timeout. Otherwise ADD runs
  `preview_campaign_scope` and `create_campaign` for each device, exactly
  as an operator request does. Only the interactive password prompt is
  replaced by the approval. The stage is atomic: if any device is refused,
  no campaign of that stage remains.
- **Offers** are unchanged: `assignment_for_connector` and the fleet guard
  admit each device (RECONCILING still holds a slot).
- **Advance** only when every device of the stage reports SUCCEEDED and no
  new post-install hold is proven (the nationwide schedule's expansion
  check).

Campaigns are created with actor `hil-plan:<plan id>`; their reason names
the plan, the stage and the approving owner. Every automatic action is an
audit row by `system:hil-plan` carrying the plan id and `approved_by`.

## Halts

The plan halts (no further campaigns) on any of:

| Code | Meaning |
| --- | --- |
| `DEPLOYMENT_FAILED`, `DEPLOYMENT_ROLLED_BACK`, `DEPLOYMENT_CANCELLED`, … | A stage deployment ended without success |
| `CAMPAIGN_PAUSED`, `CAMPAIGN_CANCELLED` | A stage campaign was paused or cancelled (by anyone) |
| `ADMISSION_REFUSED:<code>` | The fleet guard recorded a refusal on a stage deployment |
| `PREFLIGHT_REFUSED`, `PREFLIGHT_SCOPE_MISMATCH`, `PREFLIGHT_HOLD`, `PREFLIGHT_WARNING` | The stage preflight refused, held or warned (including ESP stability warnings) |
| `CAMPAIGN_REFUSED` | `create_campaign` refused a stage device |
| `POST_INSTALL_HOLD:<code>` | A defect proven after SUCCEEDED |
| `DEVICE_INSTALLATION_UNSETTLED` | A stage device holds an unsettled installation outside the plan |
| `RELEASE_UNAVAILABLE`, `RELEASE_CHANGED`, `PLAN_PIN_CHANGED` | The pinned release or plan changed |
| `STAGE_START_TIMEOUT`, `STAGE_TIMEOUT` | The stage could not start, or did not succeed, in time |
| `PLAN_EXPIRED` (status EXPIRED) | The approval expired first |

On a halt or expiry ADD pauses every plan campaign that never offered bytes,
raises the `FIRMWARE_HIL_PLAN_HALTED` alert on the affected device (no health
effect), writes the audit row and publishes a firmware event. A deployment
that already received bytes continues under its own safety states.

## Owner controls (password step-up)

- **Pause**: no stage starts; monitoring and halts continue.
- **Resume**: re-verifies the pins; the current phase's timeout restarts.
- **Cancel**: refused while any plan deployment is OFFERED or DOWNLOADING.
  Otherwise it cancels only the plan campaigns that never offered bytes.

A halted, expired or cancelled plan is finished; approve a new plan to
continue. Paused campaigns it leaves behind are resumed or cancelled with
the existing campaign controls.

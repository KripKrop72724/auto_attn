# Staged HIL plans (one approval)

Owner decision, 21 October 2026: replace "the owner creates every HIL
campaign by hand" with one approval of an ordered, staged plan. ADD then
starts each stage itself.

## What the owner approves

Firmware → Campaigns → **Staged HIL plans** → **New staged plan**:

1. A HIL_ONLY ZKT **2.7 platform** release (`zone-lite-2.7.x`). Only exact
   targets in the release's signed `hil_targets` exposure (`_hil_targets`)
   can be chosen. Storage recovery images (2.6.24–2.6.27), the journal
   bridges and writer (2.6.16–2.7.0) and older releases stay manual, one
   campaign at a time.
2. Ordered stages of one or two zones. A stage must fit the existing fleet
   guard: at most two unsettled upgrades nationwide and one per location
   (SLICTOWER 13FL and 3FL never share a stage).
3. The soak after each install (default 30 minutes, 10–720) and the
   install window in Pakistan time (default 10:00–16:00: after the morning
   check-in peak, while people are at the office, before check-out).
4. **Preview exact plan**. ADD resolves each zone to its one exact device
   (connector, ESP MAC, terminal serial), runs the normal campaign
   preflight for every device and shows:
   - the device's existing conditions the approval would accept (see
     *Device holds*);
   - the devices that hold upgrade slots now.
   It refuses the plan if any zone has an active or paused campaign, a
   device has an unsettled installation or a hold that needs a person, or a
   stage cannot fit beside the current slot holders.
5. The audited reason, the typed version and the administrator password.

The approval pins the release identity (release id, version, artifact and
application digests), the device list with each device's accepted holds,
the timeouts, the soak and the window. The pins are digested and sealed
with the fleet root secret; ADD re-verifies them on every step. A preview
that changed (a hold appeared or cleared, a setting changed) must be run
again before approval. Approvals expire after 72 hours by default (1–72);
stage timeouts default to 6 hours (30 minutes – 24 hours) and cover the
install and the soak. One plan is open at a time.

## Device holds

Before each manual campaign the owner looked at the device. With nobody at
the keyboard, ADD checks the same things at preview and again before every
stage starts:

- terminal identity hold (`writes_disabled_reason`), each source or
  identity review (reconciliation job in NEEDS_ATTENTION), unverified OTA
  security capability;
- every gating device-health reason and the stored device error, except an
  earlier failed install of this device (owner decision: history, not a
  hold);
- the last reported storage and journal state (preservation not verified,
  journal reader not ready, a reboot-persistence incident).

At preview, a hold that needs a person (identity, duplicate serial,
restart loop, fatal, capability, an unknown code, more than 20 source
reviews) refuses the plan. The storage, delivery, terminal-link and message
faults a 2.7 install is meant to repair, and source reviews that already
exist, are listed and accepted by the approval (13FL and P02 carry such
faults today). Any hold not accepted at approval halts the plan with
`DEVICE_HOLD:<code>` before that stage creates a campaign.

## What ADD does

Every ten seconds, under a PostgreSQL row lock (two API workers and
restarts are safe; campaign creation is idempotent):

- **Stage start.** Outside the install window the stage waits. A new
  device hold halts. If other installations hold the slots or the location,
  or a stage device is offline, the stage waits (shown as the waiting
  reason) up to the stage timeout, counted from when the window opened. A
  stage starts only if its timeout ends before the approval does. Otherwise
  ADD runs `preview_campaign_scope` and `create_campaign` for each device,
  exactly as an operator request does. Only the interactive password prompt
  is replaced by the approval. The stage is atomic: if any device is
  refused, no campaign of that stage remains. A database error (deadlock,
  lock timeout) rolls the step back and retries on the next tick.
- **Offers** are unchanged: `assignment_for_connector` and the fleet guard
  admit each device (RECONCILING still holds a slot). A guard refusal that
  clears by itself (two upgrades busy, location busy, device offline)
  waits; any other refusal halts.
- **Settle.** SUCCEEDED is not enough. Each installed device must prove
  itself on its new boot, re-checked on every tick (so late telemetry is
  never missed, and a defect halts at once even while the stage partner
  still installs):
  - no post-install hold: a new source review or identity hold, a security
    capability regression, a persistence fault, or any gating health alert
    raised after SUCCEEDED that was not accepted, or that is a storage,
    delivery, restart-loop, fatal or serial fault even if accepted (the
    install did not repair it);
  - heartbeats report the release version and application digest;
  - storage diagnostics sampled after the install on the new boot:
    HEALTHY, persistence verified, recovery complete, no fault counters
    (a storage fault halts), and the journal reader ready where the image
    has one;
  - the terminal link is connected;
  - the soak: heartbeats on one boot with no gap longer than ADD's offline
    threshold for the whole soak window after the install;
  - attendance delivery: nothing waiting on the ESP, or ADD committed an
    attendance batch from it since the install.
- **Advance** when every stage device has settled. The stage's campaigns
  are marked COMPLETED, which frees their zones for the next plan or
  campaign (HIL runs, observations and acceptance accept COMPLETED). A
  stage campaign the owner cancels after its device SUCCEEDED does not halt
  the plan.
- **Expiry** only stops new stages. A started stage is supervised to its
  end, so a final stage can still complete.

Campaigns are created with actor `hil-plan:<plan id>`; their reason names
the plan, the stage and the approving owner. Every automatic action is an
audit row by `system:hil-plan` carrying the plan id and `approved_by`.

## Halts

The plan halts (no further campaigns) on any of:

| Code | Meaning |
| --- | --- |
| `DEPLOYMENT_FAILED`, `DEPLOYMENT_ROLLED_BACK`, `DEPLOYMENT_CANCELLED`, … | A stage deployment ended without success |
| `CAMPAIGN_PAUSED`, `CAMPAIGN_CANCELLED` | A stage campaign was paused or cancelled (by anyone) before its device succeeded |
| `ADMISSION_REFUSED:<code>` | The fleet guard recorded a refusal that does not clear by itself |
| `DEVICE_HOLD:<code>` | A hold not accepted at approval appeared before the stage started |
| `PREFLIGHT_REFUSED`, `PREFLIGHT_SCOPE_MISMATCH`, `PREFLIGHT_HOLD` | The stage preflight refused or held a device |
| `CAMPAIGN_REFUSED` | `create_campaign` refused a stage device |
| `POST_INSTALL_HOLD:<code>` | A defect proven after SUCCEEDED (see *Settle*) |
| `DEVICE_INSTALLATION_UNSETTLED` | A stage device holds an unsettled installation outside the plan |
| `RELEASE_UNAVAILABLE`, `RELEASE_CHANGED`, `RELEASE_STORE_UNVERIFIED`, `PLAN_PIN_CHANGED` | The pinned release, the release store or the plan changed |
| `STAGE_START_TIMEOUT`, `STAGE_TIMEOUT` | The stage could not start, or did not succeed and settle, in time |
| `PLAN_EXPIRED` (status EXPIRED) | The approval ends before the next stage could finish |

`PREFLIGHT_WARNING` is reserved for the ESP stability warnings that
`fix/ota-interrupted-boot-retry` adds to the preflight: a plan never
overrides them. On main today the restart-loop hold covers that case.

On a halt or expiry ADD pauses every plan campaign that never offered bytes
and completes those whose devices all succeeded, writes the audit row and
publishes a firmware event. A halt caused by a device raises
`FIRMWARE_HIL_PLAN_HALTED` (WARNING, no health effect, never a hold) on that
device; it keeps the earlier halts in its details and an operator resolves
it after review. Timeouts of slot waits, expiry and release or plan changes
are plan-level: the plan, its audit row and the event carry them. A
deployment that already received bytes continues under its own safety
states.

## Owner controls (password step-up)

- **Pause**: no stage starts, and stage campaigns that never offered bytes
  are paused with the plan. A device already receiving bytes continues;
  monitoring and halts continue.
- **Resume**: re-verifies the pins, releases the campaigns the pause held,
  and restarts the current stage timer.
- **Cancel**: never refused. It cancels the plan campaigns that never
  offered bytes; a device already receiving bytes is never interrupted and
  continues under its own campaign, which the owner manages with the
  existing campaign controls.

A halted, expired or cancelled plan is finished; approve a new plan to
continue. Paused campaigns it leaves behind are resumed or cancelled with
the existing campaign controls.

## Production notes (22 October 2026)

- 13FL (2.7.4) and P02 (2.7.6) each still have a PAUSED campaign. Cancel
  them (and check 3FL) before approving a plan on those zones.
- Lahore-02's 2.6.15 deployment has been RECONCILING since 2 October with
  its campaign cancelled, so it holds one of the two upgrade slots
  nationwide. While it does, every stage is one device, and no stage may
  use a Lahore device; the preview refuses anything else.

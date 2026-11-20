// Status words by domain and code (02-words WORDS-3: Appendices A1-A6, C2-C5,
// D1-D4, E1-E4, F1-F5, G1-G6, H1, H5 and the 08-advanced setup states).
//
// The same code means different things in different places (PENDING is
// "Waiting to send" for a check-in, "Waiting its turn" for an update), so the
// dictionary is keyed by domain first. Tone comes only from here: nothing is
// guessed from words inside a code.

import type { Domain, PlainStatus, StateWord, Tone } from './types'

const w = (label: string, tone: Tone, explain: string, extra: Partial<StateWord> = {}): StateWord => ({
  label,
  tone,
  explain,
  ...extra,
})

/** Shown for a code the dictionary does not know. Never the raw code. */
export const UNKNOWN_STATUS: StateWord = w(
  'Other status',
  'neutral',
  'This status is new to the dashboard. Its code is under Technical details.',
)

/** A missing value (null or empty) is plain text, not a chip. */
export const NOT_REPORTED = 'Not reported yet'

const same = (codes: string[], word: StateWord): Record<string, StateWord> =>
  Object.fromEntries(codes.map((code) => [code, word]))

export const STATE_WORDS: Record<Domain, Record<string, StateWord>> = {
  // A1. Device state (Connector.lifecycle_state, health.derived_lifecycle)
  device: {
    OFFLINE: w('Offline', 'problem', 'The dashboard has not heard from this device for more than 45 seconds, so its check-ins are not arriving.', { rank: 0 }),
    QUARANTINED_DUPLICATE_SERIAL: w('Blocked: duplicate device', 'problem', 'Two link boxes say they are connected to the same machine, so this one is not used until that is sorted out.', { rank: 1 }),
    DEGRADED: w('Needs attention', 'warning', 'Check-ins may still arrive, but something on this device needs checking.', { rank: 2 }),
    FLAPPING: w('Needs attention', 'warning', 'The connection to the machine keeps dropping and coming back.', { rank: 2 }),
    ONBOARDING: w('Setting up', 'neutral', 'This device is being set up and is not counted yet.', { rank: 3 }),
    ONLINE_WITH_WARNINGS: w('Working', 'good', 'Check-ins are arriving; there is a note to read when you have time.', { rank: 4 }),
    ONLINE: w('Working', 'good', 'Check-ins from this machine are reaching the dashboard.', { rank: 4 }),
    SPARE: w('Spare (not in use)', 'neutral', 'Kept in reserve; not counted in totals or problems.', { rank: 5 }),
  },
  // A2. Health tier of one reason
  tier: {
    DEGRADED: w('Needs attention', 'warning', "This changes the device's status to Needs attention."),
    WARNING: w('Note', 'neutral', 'Worth a look; the device still counts as working.'),
    NONE: w('For information', 'neutral', "Recorded for information; it doesn't change the device's status."),
  },
  // A4. Machine connection (terminal_link.state)
  machineLink: {
    CONNECTED: w('Machine connected', 'good', 'The link box can read the attendance machine.'),
    STABILIZING: w('Machine just reconnected', 'neutral', 'The machine came back; the link box is making sure the connection stays steady.'),
    MAINTENANCE: w('Planned machine restart', 'neutral', 'The machine is refreshing or restarting on schedule; this is normal.'),
    STARTING: w('Starting up', 'neutral', 'The link box is starting its connection to the machine.'),
    RECONNECTING: w('Reconnecting to the machine', 'warning', 'The link box lost the machine for a moment and is trying again.'),
    DISCONNECTED: w('Machine not responding', 'problem', 'The link box can’t reach the machine; it may be switched off or unplugged.'),
    FLAPPING: w('Connection keeps dropping', 'warning', 'The machine keeps connecting and disconnecting, so the link box waits between tries.'),
    ERROR: w('Machine problem', 'problem', 'The machine answers but something is wrong; the reason is below.'),
    UNKNOWN: w('Machine status not known', 'neutral', "The link box is offline or hasn't reported, so the dashboard can't tell."),
  },
  // A6. Connection history state (connection_state)
  machineRaw: {
    BOOTING: w('Link box starting up', 'neutral', 'The link box is starting up.'),
    DISCOVERING: w('Looking for the machine', 'neutral', 'Looking for the machine on the network.'),
    CONNECTING: w('Connecting to the machine', 'neutral', 'Connecting to the machine.'),
    RECOVERING: w('Checking the connection', 'neutral', 'Connected, checking the connection stays steady.'),
    ONLINE: w('Connected', 'good', 'Connected to the machine.'),
    STABLE: w('Connected', 'good', 'Connected to the machine.'),
    SUSPECT: w('Lost the machine', 'warning', 'Lost the machine, trying again.'),
    RETRY_WAIT: w("Can't reach the machine", 'warning', "Can't reach the machine, waiting to try again."),
    FLAPPING: w('Connection keeps dropping', 'warning', 'The connection keeps dropping, pausing before trying again.'),
    SESSION_REFRESH: w('Reconnecting on purpose', 'neutral', 'Reconnecting on purpose (routine).'),
    RESTARTING: w('Machine restarting', 'neutral', 'The machine is restarting.'),
    OFFLINE: w('Machine not reachable', 'problem', 'The machine is not reachable.'),
    UNKNOWN: w('First record', 'neutral', 'The first connection record for this device.'),
  },
  // A3. Health detail words (Technical details only)
  currency: {
    CURRENT: w('In the latest report', 'neutral', 'In the latest status report.'),
    HELD: w('Waiting for proof', 'neutral', "Waiting for the link box to show it's fixed."),
    PREVIOUS_BOOT: w('Before the last restart', 'neutral', 'From before the last restart.'),
    LATCHED: w('Stays until closed', 'neutral', 'Stays until a person or a later update closes it.'),
  },
  binding: {
    OBSERVED: w('Seen since restart', 'neutral', 'Seen since the last restart.'),
    INFERRED_CURRENT: w('Probably since restart', 'neutral', 'Probably since the last restart.'),
    INFERRED_PREVIOUS: w('Probably before restart', 'neutral', 'Probably before the last restart.'),
  },
  latch: {
    LED_LATCH_NO_IO_ERRORS: w('Light stays on', 'neutral', 'Warning light stays on until restart; no memory errors were found.'),
    LED_LATCH_STORAGE_VERIFIED: w('Light stays on', 'neutral', 'Warning light stays on until restart; memory was checked and is fine.'),
  },
  coverage: {
    REPORTED: w('Reported', 'good', 'Reported.'),
    NOT_REPORTED_BY_FIRMWARE: w("Can't report it", 'neutral', "This software version can't report it."),
    MISSING: w('Not reported', 'warning', 'Not in the latest status report.'),
    ACTIVE: w('On', 'neutral', 'On.'),
  },
  coverageKey: {
    storage: w('Saved check-ins', 'neutral', 'Whether the link box can save check-ins safely.'),
    workers: w('Sending check-ins', 'neutral', 'Whether the link box is sending check-ins.'),
    preservation: w('Safe to update', 'neutral', 'Whether saved check-ins stay safe during an update.'),
    led: w('Warning light', 'neutral', "The link box's warning light."),
  },
  // C2. Severity, problem state and queues
  severity: {
    CRITICAL: w('Urgent', 'problem', 'Needs action now.'),
    HIGH: w('Important', 'problem', 'Needs action soon.'),
    WARNING: w('Low', 'warning', 'Worth a look when you have time.'),
    MEDIUM: w('Low', 'warning', 'Worth a look when you have time.'),
    INFO: w('Low', 'neutral', 'For information.'),
  },
  alertState: {
    NEW: w('New', 'warning', 'Nobody has marked this as seen yet.'),
    OPEN: w('New', 'warning', 'Nobody has marked this as seen yet.'),
    ACKNOWLEDGED: w('Seen', 'neutral', 'Someone has seen this; it stays open until it is fixed.'),
    RESOLVED: w('Fixed', 'good', 'This problem is fixed.'),
  },
  alertQueue: {
    NEEDS_ACTION: w('New', 'warning', 'Problems nobody has seen yet.'),
    ACKNOWLEDGED: w('Seen', 'neutral', 'Seen and still open.'),
    ACTIVE: w('Open', 'neutral', 'Every open problem.'),
    RESOLVED: w('Fixed', 'good', 'Problems that are fixed.'),
    ALL: w('All', 'neutral', 'Every problem.'),
  },
  // C5. Old-problems clean-up classes
  cleanupClass: {
    STRANDED_DIAGNOSTICS: w('Left over from a restart', 'neutral', 'Left over from a restart of the link box.'),
    CONDITION_CLEARED: w('Already cleared', 'good', 'The problem has already cleared.'),
    SUPERSEDED_ACK: w("Old 'seen' mark", 'neutral', "An old 'seen' mark that a newer one replaced."),
    STALE_ERROR_CODE: w('Old link box error', 'neutral', 'An old error the link box no longer reports.'),
    REVIEW: w('Check before closing', 'warning', 'Check this one before closing it.'),
  },
  // D1. Sending a check-in to the HR system
  delivery: {
    ACKED: w('Sent to HR system', 'good', 'The HR system has this check-in.'),
    ACKED_CHECK: w('Sent to HR system', 'good', 'The HR system has this check-in, and the dashboard double-checked it.'),
    ACKED_FIRMWARE: w('Checking with the HR system', 'neutral', 'The link box says the HR system has it; the dashboard is checking.'),
    FIRMWARE_RECEIPT_UNVERIFIED: w('Checking with the HR system', 'neutral', 'The dashboard is checking with the HR system that it really has this check-in.'),
    FIRMWARE_RECEIPT_VERIFYING: w('Checking with the HR system', 'neutral', 'The dashboard is checking with the HR system now.'),
    MEMBERSHIP_REVERIFYING: w('Checking with the HR system', 'neutral', 'The dashboard is checking again that the HR system still has it.'),
    MEMBERSHIP_REVERIFY_RETRY: w('Checking with the HR system', 'warning', "The HR system didn't answer the check; the dashboard will try again."),
    PENDING: w('Waiting to send', 'neutral', 'Saved and queued for the HR system.'),
    IN_FLIGHT: w('Sending now', 'neutral', 'Being sent to the HR system right now.'),
    FAILED_RETRYABLE: w('Trying again', 'warning', "The HR system didn't accept it yet; it will be sent again automatically."),
    RETRYING: w('Trying again', 'warning', "The HR system didn't accept it yet; it will be sent again automatically."),
    BLOCKED_IDENTITY: w('Not matched to an employee', 'warning', "The dashboard can't tell which employee made this check-in (usually no CNIC)."),
    CNIC_NOT_LINKED: w('CNIC not linked', 'warning', "This person's CNIC is not linked yet, so the check-in waits."),
    QUARANTINED_IDENTITY_REUSE: w('Employee ID used before', 'warning', 'This Employee ID belonged to someone else before, so it needs a check.'),
    QUARANTINED_IDENTITY_CONFLICT: w('Two people, one number', 'warning', 'Records show more than one person with this Employee ID, so it needs a check.'),
    QUARANTINED_SOURCE_CONFLICT: w('Records disagree', 'warning', "The machine's records disagree about this check-in, so it needs a check."),
    QUARANTINED_INVALID_DEVICE_TIME: w('Impossible time', 'problem', "The check-in's date or time is impossible, so it is never sent."),
    QUARANTINED_INVALID_EVENT_UID: w('Old record number', 'problem', 'An older record number must be repaired before the HR system can take it.'),
    QUARANTINED_ORDS_REJECTED: w('Refused by HR system', 'problem', 'The HR system permanently refused this check-in.'),
    QUARANTINED_MISSING_TERMINAL_PROVENANCE: w('Machine not confirmed', 'problem', "The dashboard can't prove which machine this came from, so it isn't sent."),
    MISSING_STATUS: w('Status not known', 'neutral', 'The dashboard has no sending status for this check-in yet.'),
  },
  deliveryAssurance: {
    CONFIRMED: w('Sent', 'good', 'The HR system has these check-ins.'),
    IDENTITY_HELD: w('Not matched', 'warning', 'Some check-ins are not matched to an employee yet.'),
    PENDING: w('Waiting', 'neutral', 'Some check-ins are still on their way.'),
    REVIEW_REQUIRED: w('Needs review', 'warning', 'Some check-ins need a person to look at them.'),
  },
  confirmationPath: {
    FIRMWARE_LIVE: w('Confirmed by the link box', 'neutral', 'The link box confirmed the HR system has it.'),
    FIRMWARE_BULK: w('Confirmed by the link box', 'neutral', 'The link box confirmed the HR system has it.'),
    FIRMWARE_RECONCILE: w('Confirmed by the link box', 'neutral', 'The link box confirmed the HR system has it.'),
    ADD_DELIVERY: w('Sent by the dashboard', 'neutral', 'The dashboard sent it to the HR system.'),
  },
  // D2. Matching a check-in to an employee
  identity: {
    WAITING_FOR_SNAPSHOT: w('Waiting for the people list', 'neutral', "The dashboard is waiting for the machine's list of people to match this check-in."),
    ...same(['RESOLVED', 'RESOLVED_CURRENT_SNAPSHOT', 'RESOLVED_SYNCED_CNIC'], w('Matched', 'good', 'Matched to an employee automatically.')),
    MANUALLY_APPROVED_CURRENT_TERMINAL: w('Approved by an admin', 'good', 'An admin confirmed the employee.'),
    ...same(
      [
        'RESOLVED_RETAINED_IDENTITY', 'RESOLVED_DIRECTORY_EVIDENCE', 'RESOLVED_DIRECTORY_EVENT_GROUP',
        'RESOLVED_CURRENT_IDENTITY_EVIDENCE', 'RESOLVED_HISTORICAL_ALIAS', 'RESOLVED_TOMBSTONE',
        'RESOLVED_ATTENDANCE_REPAIR', 'RESOLVED_HIKVISION_NAME',
      ],
      w('Matched (checked)', 'good', 'Matched using HR records or an admin decision.'),
    ),
    QUARANTINED_REUSE: w('Employee ID used before', 'warning', 'This Employee ID belonged to someone else before; it is set aside for a check.'),
    BLOCKED_IDENTITY: w('Not matched to an employee', 'warning', "The dashboard can't tell which employee made this check-in (usually no CNIC)."),
    BLOCKED_CONFLICT: w('Two people, one number', 'warning', 'More than one person could have made this check-in, so it needs a check.'),
    BLOCKED_MALFORMED_IDENTITY: w('Damaged person record', 'problem', "The person's details on this record can't be read, so it needs a check."),
    BLOCKED_PROVENANCE: w('Machine not confirmed', 'problem', "The dashboard can't prove which machine this came from."),
    BLOCKED_SOURCE_CONFLICT: w('Records disagree', 'warning', 'Records disagree about who made this check-in.'),
    BLOCKED_INVALID_EVENT_UID: w('Old record number', 'problem', 'An older record number needs repair.'),
  },
  // D3. Clock of a check-in
  clock: {
    OK: w('Time correct', 'good', "The machine's clock was right for this check-in."),
    DRIFTED: w('Clock was off', 'warning', "The machine's clock was a little off; the time was corrected."),
    INVALID: w('Impossible time', 'problem', 'The check-in time is impossible, so it is never sent.'),
    UNKNOWN: w('Time not checked', 'neutral', "The machine's clock wasn't checked for this check-in."),
    OPERATOR_CORRECTED: w('Time corrected by an admin', 'neutral', 'An admin corrected the time of this check-in.'),
  },
  // D4. How a check-in arrived
  captureSource: {
    LIVE: w('Live', 'neutral', 'Arrived as it happened.'),
    LIVE_POLL: w('Live', 'neutral', 'Arrived as it happened.'),
    DUMP_STARTUP: w('After a restart', 'neutral', 'Read from the machine after the link box restarted.'),
    DUMP_RECONNECT: w('After reconnecting', 'neutral', 'Read from the machine after the link box reconnected.'),
    RECONCILE_15M: w('Regular check', 'neutral', 'Found by the regular record check.'),
    FULL_HISTORY: w("From the machine's history", 'neutral', "Read from the machine's saved history."),
    CURRENT_RECONCILE: w('From a record check', 'neutral', 'Found by a record check.'),
    MANUAL_REPROCESS: w('Processed again by an admin', 'neutral', 'An admin asked the dashboard to process it again.'),
  },
  // E1. Request type (command_type)
  requestType: {
    CREATE_USER: w('Add a person to the machine', 'neutral', 'Adds a person to the machine.'),
    UPDATE_USER: w('Change a person on the machine', 'neutral', "Changes a person's details on the machine."),
    DELETE_USER: w('Remove a person from the machine', 'neutral', 'Removes a person from the machine.'),
    REFRESH_USERS: w('Refresh people list', 'neutral', "Reads the machine's list of people again, twice, to be sure."),
    GRANT_TEMP_ADMIN: w('Give temporary admin access', 'neutral', 'Lets a person manage the machine for 10 minutes.'),
    REVOKE_TEMP_ADMIN: w('End temporary admin access', 'neutral', 'Takes temporary admin access away.'),
    RESTART_ZKT: w('Restart the machine', 'neutral', 'Restarts the attendance machine.'),
    ESP_REBOOT: w('Restart the link box', 'neutral', 'Restarts the link box.'),
    PIN_TERMINAL_SERIAL: w("Confirm the machine's serial number", 'neutral', 'Saves which machine belongs to this link box.'),
    APPLY_CONFIG: w('Change the machine password', 'neutral', 'Changes the machine password (COMM Key) between the link box and the machine.'),
  },
  // E2. Request status
  request: {
    QUEUED: w('Waiting to send', 'neutral', 'Saved; it will be sent to the link box.', { rank: 1 }),
    WAITING_FOR_DEVICE: w('Waiting for the link box', 'warning', 'The link box is offline; this runs when it reconnects.', { rank: 1 }),
    DISPATCHED: w('Sent to the link box', 'neutral', 'The link box has been sent the request.', { rank: 2 }),
    ACKNOWLEDGED: w('The link box received it', 'neutral', 'The link box received the request and will carry it out.', { rank: 2 }),
    WAITING_FOR_ZKT: w('Waiting for the machine', 'warning', "The machine isn't answering; this runs when it does.", { rank: 2 }),
    RETRYING: w('Trying again', 'warning', "The last try didn't work; it is trying again.", { rank: 2 }),
    RUNNING: w('Working on it', 'neutral', 'The change is being made on the machine.', { rank: 2 }),
    CANCEL_REQUESTED: w('Cancelling…', 'neutral', 'Stopping before it is carried out.', { rank: 2 }),
    SUCCEEDED: w('Done', 'good', 'The change was made and checked on the machine.', { rank: 3 }),
    FAILED: w('Not done', 'problem', "The change wasn't made; the reason is shown.", { rank: 3 }),
    CANCELLED: w('Cancelled', 'neutral', 'The request was stopped before it was carried out.', { rank: 3 }),
    CANCELED: w('Cancelled', 'neutral', 'The request was stopped before it was carried out.', { rank: 3 }),
    EXPIRED: w('Timed out', 'warning', "The request wasn't carried out in time and was dropped.", { rank: 3 }),
  },
  // E3. People changes
  personChange: {
    PENDING: w('Change in progress', 'neutral', 'A change for this person is still being made.'),
  },
  peopleRemoval: {
    QUEUED: w('Waiting', 'neutral', 'The removal will start shortly.'),
    RUNNING: w('Removing people', 'neutral', 'People are being removed from the machine.'),
    CANCEL_REQUESTED: w('Stopping', 'neutral', 'Stopping after the person being removed now.'),
    SUCCEEDED: w('All removed', 'good', 'Everyone chosen was removed from the machine.'),
    PARTIAL: w('Some not removed', 'warning', 'Some people were not removed. Check who is still there.'),
    FAILED: w('Not done', 'problem', 'Nobody was removed.'),
    CANCELED: w('Stopped', 'neutral', 'The removal was stopped.'),
    CANCELLED: w('Stopped', 'neutral', 'The removal was stopped.'),
    EXPIRED: w('Timed out', 'warning', "The removal didn't finish in time."),
  },
  peopleRemovalItem: {
    PENDING: w('Waiting', 'neutral', 'Waiting its turn.'),
    SUCCEEDED: w('Removed', 'good', 'Removed from the machine.'),
    FAILED: w('Not removed', 'problem', "Couldn't remove this person."),
    CANCELED: w('Stopped', 'neutral', 'Not removed because the removal was stopped.'),
    CANCELLED: w('Stopped', 'neutral', 'Not removed because the removal was stopped.'),
    EXPIRED: w('Timed out', 'warning', 'Not removed in time.'),
  },
  lease: {
    REQUESTED: w('Asked for', 'neutral', 'Temporary admin access was asked for.'),
    GRANTING: w('Giving access', 'neutral', 'Temporary admin access is being given on the machine.'),
    ACTIVE: w('Active', 'good', 'This person can manage the machine for now.'),
    REVOKING: w('Removing access', 'neutral', 'Temporary admin access is being removed.'),
    REVOKED: w('Removed', 'good', 'Temporary admin access was removed.'),
    OVERDUE: w('Not removed yet', 'problem', "Temporary admin access should have ended but couldn't be removed yet."),
    EXPIRED: w('Ended', 'neutral', 'Temporary admin access has ended.'),
    FAILED: w('Not done', 'problem', "Temporary admin access couldn't be given."),
  },
  // E4. Machine password (COMM Key)
  commKey: {
    UNKNOWN: w('Not changed from the dashboard', 'neutral', "The dashboard hasn't changed this machine's password."),
    QUEUED: w('Waiting to send', 'neutral', 'The new password will be sent to the link box.'),
    PENDING_CAPABILITY: w('Needs newer link box software', 'neutral', 'The link box must be updated before the password can change.'),
    APPLIED: w('Password in use', 'good', 'The machine password was changed and checked.'),
    RECONCILIATION_REQUIRED: w('Needs a check', 'warning', "The dashboard isn't sure which password is in use and must check."),
    INDETERMINATE: w('Result not known', 'warning', 'The change may or may not have worked.'),
    CANCEL_REQUESTED: w('Cancelling…', 'neutral', 'Stopping the password change.'),
    CANCELLED: w('Cancelled', 'neutral', 'The password change was stopped.'),
    EXPIRED: w('Timed out', 'warning', 'The password change took too long and was dropped.'),
    FAILED: w('Not done', 'problem', "The password wasn't changed."),
  },
  commKeyMode: {
    ESP_ONLY: w('The link box only', 'neutral', 'Changes the password saved in the link box only.'),
    ESP_AND_TERMINAL: w('The link box and the machine', 'neutral', 'Changes the password on the machine and in the link box.'),
  },
  // F1. Device update state (Connector.ota_state)
  softwareDevice: {
    LEGACY_MANUAL_UPDATE: w('Manual updates only', 'neutral', 'This link box can only be updated by hand.'),
    OTA_READY: w('Ready for updates', 'good', 'This link box can be updated from the dashboard.'),
    UPDATING: w('Updating', 'neutral', 'This link box is being updated.'),
    ROLLBACK_REQUIRED: w('Update undone', 'warning', 'The last update was undone; the link box went back to its old version.'),
    OTA_BLOCKED: w('Updates blocked', 'problem', 'The link box refused the last update; call IT.'),
  },
  // F2. Software version
  release: {
    AVAILABLE: w('Ready to use', 'good', 'This version can be installed on devices.'),
    HIL_ONLY: w('Test devices only', 'neutral', 'This version is only for test devices for now.'),
    REVOKED: w('Withdrawn', 'problem', 'This version was withdrawn and is not installed any more.'),
    OLDER: w('Older version', 'neutral', 'Kept so devices can report their running version; not offered.'),
  },
  // F3. Update to one device (deployment status)
  update: {
    PENDING: w('Waiting its turn', 'neutral', 'This device will be updated after the ones before it.'),
    OFFERED: w('Update offered', 'neutral', 'The link box has been told an update is ready.'),
    DOWNLOADING: w('Downloading', 'neutral', 'The link box is downloading the update.'),
    VERIFYING: w('Checking the download', 'neutral', 'The link box is checking the downloaded update.'),
    READY_TO_BOOT: w('Ready to restart', 'neutral', 'The link box will restart into the new version.'),
    BOOTED_PENDING: w('Restarted, confirming', 'neutral', 'The link box restarted and is confirming the new version works.'),
    RECONCILING: w('Final checks', 'neutral', 'The link box is finishing its checks on the new version.'),
    SUCCEEDED: w('Updated', 'good', 'The link box runs the new version.'),
    FAILED: w('Update failed', 'problem', "The update didn't work; the link box kept its old version."),
    ROLLED_BACK: w('Update undone', 'problem', "The new version didn't start properly, so the link box went back to the old one."),
    CANCELLED: w('Cancelled', 'neutral', 'The update was stopped before it started on this device.'),
    SUPERSEDED: w('Replaced by a newer update', 'neutral', 'A newer update replaced this one.'),
    RELEASE_REVOKED: w('Version withdrawn', 'warning', "This version was withdrawn, so it won't be installed."),
  },
  // The link box's own reported update state (ota.state): F3 plus four.
  otaState: {},
  // F4. Update rollout
  rollout: {
    ACTIVE: w('Rolling out', 'neutral', 'Devices are being updated one after another.'),
    PAUSED: w('Paused', 'warning', 'The rollout is paused; no new devices start updating.'),
    COMPLETED: w('Finished', 'good', 'Every device in this rollout has finished.'),
    CANCELLED: w('Stopped', 'neutral', 'The rollout was stopped.'),
    ARCHIVED: w('Cleared from history', 'neutral', 'This rollout was cleared from the history list.'),
  },
  bulkStart: {
    CREATED: w('Started', 'good', 'Started.'),
    BUSY: w('Already updating', 'warning', 'This device is already being updated.'),
    BLOCKED: w("Can't start", 'problem', "This one can't start; the reason is shown."),
  },
  updateWait: {
    NATIONWIDE_TARGET_OFFLINE: w('Waiting for the device to come online', 'neutral', 'The update starts when the device is back online.'),
    NATIONWIDE_EXACT_TARGET_REQUIRED: w('Not on the list of devices for this version', 'neutral', 'This device is not on the list of devices chosen for this version.'),
    NATIONWIDE_TWO_UPGRADE_RESERVATIONS: w('Waiting for its turn', 'neutral', 'Two devices are already updating; this one waits for its turn.'),
  },
  // F5. Update timeline events
  updateEvent: {
    STABILITY_WARNINGS_OVERRIDDEN: w('Started despite restart warnings', 'warning', 'The update was started even though the link box had restarted often.'),
    RETRY_REQUESTED: w('Try again pressed', 'neutral', 'Someone pressed Try again.'),
    DOWNLOAD_INTERRUPTED: w('Download interrupted', 'neutral', 'The download was interrupted; it continues from where it stopped.'),
    DOWNLOAD_RETRY_OFFERED: w('Download offered again', 'neutral', 'The download was offered to the link box again.'),
    DOWNLOAD_RETRIES_EXHAUSTED: w('Download stopped after several tries', 'warning', 'The download failed several times, so it stopped.'),
    DOWNLOAD_RETRY_NOT_TAKEN: w("The link box didn't take the new download", 'warning', "The link box didn't start the new download."),
    DOWNLOAD_RETRY_WITHDRAWN: w('New download withdrawn', 'neutral', 'The new download was withdrawn.'),
    BRIDGE_READY: w('Test step passed', 'good', 'The test step passed (test devices only).'),
    BRIDGE_FAILED: w('Test step failed', 'problem', 'The test step failed (test devices only).'),
    BRIDGE_INCOMPLETE: w('Test step not finished', 'warning', "The test step didn't finish (test devices only)."),
    HIL_INCOMPLETE: w('Test step not finished', 'warning', "The test step didn't finish (test devices only)."),
  },
  stability: {
    ESP_RESET_RATE_HIGH: w('Restarts often', 'warning', 'The link box has restarted often recently.'),
    ESP_UPTIME_SHORT: w('Only just restarted', 'warning', 'The link box restarted only a short time ago.'),
    TARGET_BOOT_INTERRUPTED_REPEATEDLY: w('Earlier updates were interrupted', 'warning', 'Earlier updates on this device were interrupted.'),
  },
  undoDiagnosis: {
    TARGET_BOOT_INTERRUPTED: w('Power or a reset interrupted the restart', 'warning', 'The power went off or the link box was reset while it restarted.'),
    UNCLASSIFIED: w('Cause not known', 'neutral', "The dashboard couldn't tell why the update was undone."),
  },
  testSchedule: {
    PASSED: w('Passed', 'good', 'The test passed.'),
    PENDING: w('Waiting to test', 'neutral', 'The test has not run yet.'),
    DEFERRED_OFFLINE: w('Skipped: offline', 'neutral', 'Skipped because the test device was offline.'),
    BLOCKED: w('Blocked', 'warning', "The test can't run yet."),
    NOT_APPLICABLE_TO_THIS_BRIDGE: w('Not needed', 'neutral', 'This test is not needed for this device.'),
  },
  repairUpdate: {
    EVALUATING: w('Checking', 'neutral', 'Checking whether this link box needs the memory repair.'),
    REFUSED: w('Refused', 'warning', 'This link box was refused the memory repair.'),
    ELIGIBLE: w('Allowed', 'neutral', 'This link box may get the memory repair.'),
    CONFIRMING: w('Confirming', 'neutral', 'Confirming the memory repair.'),
    CONFIRMED: w('Confirmed', 'good', 'The memory repair is confirmed.'),
    ROUND_DONE: w('Repair round done', 'good', 'A memory repair round finished.'),
    ROUND_SHORT: w('Repair round short', 'warning', 'A memory repair round ended early.'),
    UNREPAIRED: w('Not repaired', 'problem', 'The memory could not be repaired.'),
    UNHEALTHY: w('Not healthy', 'problem', 'The link box memory is still not healthy.'),
    ABANDONED: w('Given up', 'problem', 'The memory repair was given up.'),
  },
  // G1. Recover attendance job status
  recoverJob: {
    QUEUED: w('Waiting to start', 'neutral', 'Starts when the machine is free.'),
    RUNNING: w('Reading the machine', 'neutral', 'Reading records from the machine.'),
    PAUSE_REQUESTED: w('Pausing', 'neutral', 'Pausing after the current step.'),
    PAUSED: w('Paused', 'warning', 'Progress so far is kept.'),
    CANCEL_REQUESTED: w('Stopping', 'neutral', 'Stopping after the current step.'),
    NEEDS_ATTENTION: w('Waiting for you', 'warning', 'A step failed; read the message and choose Try again or Stop.'),
    WAITING_FOR_PROTOCOL: w('Needs newer link box software', 'warning', 'The link box needs a software update before this can run.'),
    COMPLETED: w('Done', 'good', 'Finished.'),
    CANCELLED: w('Stopped', 'neutral', 'Stopped by a person.'),
    FAILED: w("Couldn't finish", 'problem', "This recovery couldn't finish."),
    INVALIDATED: w('Replaced by a newer check', 'neutral', 'A newer check replaced this one.'),
  },
  // G2. What it is doing
  recoverStep: {
    CAPTURING_SOURCE: w('Reading records from the machine', 'neutral', 'Reading records from the machine.'),
    VERIFYING_ORACLE: w('Checking the HR system has them', 'neutral', 'Checking the HR system has these check-ins.'),
    VERIFYING_SOURCE_CHANGE: w('Double-checking changed records', 'neutral', 'Some records changed; reading them again.'),
    RECOVERING: w('Resuming where it stopped', 'neutral', 'Continuing from where it stopped.'),
    RECOVERING_AFTER_INTERRUPTION: w('Resuming where it stopped', 'neutral', 'Continuing from where it stopped.'),
    RECOVERING_AFTER_SOURCE_CHANGE: w('Resuming after the records changed', 'neutral', 'Continuing after the machine records changed.'),
    WAITING_FOR_DEVICE: w('Waiting for the device to come online', 'warning', 'Continues when the device is back online.'),
    WAITING_FOR_ORACLE_CAPACITY: w('Waiting for earlier check-ins to finish sending', 'neutral', 'Earlier check-ins are still being sent to the HR system.'),
    HISTORY_BACKLOG_BACKPRESSURE: w('Waiting for earlier check-ins to finish sending', 'neutral', 'Earlier check-ins are still being sent to the HR system.'),
    HISTORY_BACKLOG_CREDIT_EXHAUSTED: w('Waiting for earlier check-ins to finish sending', 'neutral', 'Earlier check-ins are still being sent to the HR system.'),
    REVIEW_REQUIRED: w('Waiting for you', 'warning', 'Some records need a person to look at them.'),
    NEEDS_ATTENTION: w('Waiting for you', 'warning', 'A step failed; read the message and choose Try again or Stop.'),
    PAUSE_REQUESTED: w('Pausing', 'neutral', 'Pausing after the current step.'),
    PAUSED: w('Paused', 'warning', 'Progress so far is kept.'),
    CANCELLED: w('Stopped', 'neutral', 'Stopped by a person.'),
    COMPLETED: w('Done — all confirmed', 'good', 'Every record is confirmed.'),
    COMPLETED_WITH_REVIEW: w('Done — some need review', 'warning', 'Finished; some records need a person to look at them.'),
    COMPLETED_WITH_IDENTITY_HOLDS: w('Done — some not matched', 'warning', 'Finished; some check-ins are not matched to an employee.'),
    COMPLETED_WITH_CORRECTIONS: w('Done — some times corrected', 'good', 'Finished; some check-in times were corrected.'),
    CERTIFIED_WITH_REVIEWED_SOURCE_EXCEPTIONS: w('Done — unreadable records reviewed', 'good', 'Finished; the unreadable records were reviewed.'),
  },
  // G3. Stage
  recoverPhase: {
    PREFLIGHT: w('Getting ready', 'neutral', 'Getting ready to read the machine.'),
    ANCHORING: w('Finding the first record', 'neutral', 'Finding the first record on the machine.'),
    SCANNING_TERMINAL: w('Reading records', 'neutral', 'Reading records from the machine.'),
    DRAINING_ORDS: w('Sending to the HR system', 'neutral', 'Sending check-ins to the HR system.'),
    FINAL_ASSURANCE: w('Final check', 'neutral', 'Checking everything reached the HR system.'),
    WAITING_FOR_SAFE_WINDOW: w('Waiting for a quiet moment', 'neutral', 'Waiting for a quiet moment at the machine.'),
    WAITING_FOR_IDENTITY: w('Waiting for employee matching', 'neutral', 'Waiting for check-ins to be matched to employees.'),
    WAITING_FOR_DEVICE: w('Waiting for the device', 'warning', 'Waiting for the device to come online.'),
    WAITING_FOR_CAPACITY: w('Waiting for its turn', 'neutral', 'Other recoveries are running first.'),
    WAITING_FOR_SCAN_SLOT: w('Waiting for its turn', 'neutral', 'Other recoveries are running first.'),
    RECOVERING_AFTER_INTERRUPTION: w('Resuming', 'neutral', 'Continuing from where it stopped.'),
    RECOVERING_AFTER_SOURCE_CHANGE: w('Resuming after changes', 'neutral', 'Continuing after the machine records changed.'),
    VERIFYING_SOURCE_CHANGE: w('Double-checking changes', 'neutral', 'Some records changed; reading them again.'),
    SOURCE_DIVERGENCE_PROBE_PENDING: w('Double-checking changed records', 'neutral', 'Some records changed; reading them again.'),
    WAITING_FOR_PROTOCOL: w('Needs newer link box software', 'warning', 'The link box needs a software update before this can run.'),
    TRANSIENT_STEP_RETRY: w('Trying a step again', 'warning', "A step didn't work; trying it again."),
    SOURCE_EPOCH_RECOVERY_LIMIT: w('Records changed too often — needs review', 'warning', 'The machine records changed too often; a person needs to look.'),
    SOURCE_DIVERGENCE_UNSTABLE: w('Records keep changing — needs review', 'warning', 'The machine records keep changing; a person needs to look.'),
    SELF_HEALING_RECOVERY_DISABLED: w('Automatic recovery is switched off', 'warning', 'Automatic recovery is switched off for now.'),
    ORACLE_TERMINAL_OUTCOME_REQUIRES_REVIEW: w("The HR system's answer needs review", 'warning', "The HR system's answer needs a person to look at it."),
  },
  // G5. History of one check
  recoverEvent: {
    QUEUED: w('Started', 'neutral', 'The recovery started.'),
    ANCHORED: w('Found the first record', 'neutral', 'Found the first record on the machine.'),
    CHUNK_COMMITTED: w('Records saved', 'neutral', 'A batch of records was saved.'),
    ORACLE_MEMBERSHIP_RETRYING: w('Checking with the HR system', 'neutral', 'Checking with the HR system again.'),
    ORACLE_MEMBERSHIP_REVIEW_REQUIRED: w('Waiting for you', 'warning', "The HR system's answer needs a person to look at it."),
    SAFETY_HOLD: w('Stopped for safety', 'warning', 'The recovery stopped to keep records safe.'),
    COVERAGE_INVALIDATED: w('Stopped for safety', 'warning', 'The recovery stopped to keep records safe.'),
    ORACLE_MEMBERSHIP_CERTIFIED: w('Done', 'good', 'Every record is confirmed in the HR system.'),
    ASSIGNMENT_RELEASED: w('Paused for another task', 'neutral', 'Paused so another task could run.'),
    SOURCE_DIVERGENCE_OBSERVED: w("Records don't match", 'warning', "Some records didn't match the saved copy."),
    SOURCE_DIVERGENCE_TRANSIENT_RECOVERED: w('Matched again on re-read', 'good', 'The records matched when read again.'),
    SOURCE_EXCEPTION_REVIEW_GATE_CLEARED: w('Unreadable records reviewed', 'good', 'The unreadable records were reviewed.'),
    SOURCE_INTERPRETATION_DRIFT: w('Record format changed', 'warning', 'The machine records are in a different format now.'),
    SOURCE_RECOVERY_EPOCH_ACTIVATED: w('Recovery started', 'neutral', 'A new recovery round started.'),
    SOURCE_REPLAY_RETAINED_ELSEWHERE: w('Already kept by another check', 'neutral', 'Another check already kept these records.'),
    STALE_ASSIGNMENT_RECOVERED: w('Resumed a stalled check', 'neutral', 'A stalled check was started again.'),
    SOURCE_GENERATION_SUPERSEDED: w('Replaced by a newer check', 'neutral', 'A newer check replaced this one.'),
    SOURCE_GENERATION_PREVIOUS_INVALIDATED: w('Older check no longer valid', 'neutral', 'An older check is no longer valid.'),
    SOURCE_NEWEST_PUNCH_ACCOUNTED: w('Newest record accounted for', 'good', 'The newest record on the machine is accounted for.'),
  },
  // G4. Time left, scope, lists, bulk start, preflight
  recoverEta: {
    MEASURING: w('Working out time left', 'neutral', 'Working out how long is left.'),
    DEVICE_WAIT: w('Waiting for the device', 'warning', 'Waiting for the device to come online.'),
    ORACLE_VERIFYING: w('Checking with the HR system', 'neutral', 'Checking with the HR system.'),
    REVIEW_NEEDED: w('Waiting for you', 'warning', 'Some records need a person to look at them.'),
    STOPPED: w('Stopped', 'neutral', 'Stopped.'),
    COMPLETE: w('Done', 'good', 'Finished.'),
  },
  recoverScope: {
    ALL_RECORDS: w('All records', 'neutral', 'Every record on the machine.'),
    ACTIVE_USERS: w('Current employees only', 'neutral', 'Only records of people on the machine now.'),
    RECENT_TAIL: w('Latest records only', 'neutral', 'Only the newest records on the machine.'),
    FULL_HISTORY_BASELINE: w('All records', 'neutral', 'Every record on the machine.'),
    HIKVISION_SERIAL_HISTORY: w('All records', 'neutral', 'Every record on the machine.'),
  },
  recoverList: {
    ACTIVE: w('Running', 'neutral', 'Running now.'),
    QUEUED_WAITING: w('Waiting', 'neutral', 'Waiting to start.'),
    PAUSED: w('Paused', 'warning', 'Paused.'),
    ATTENTION: w('Waiting for you', 'warning', 'Waiting for a person to decide.'),
    COMPLETED: w('Done', 'good', 'Finished.'),
    CANCELLED: w('Stopped', 'neutral', 'Stopped.'),
  },
  recoverBulk: {
    CREATED: w('Started', 'good', 'Started.'),
    ALREADY_ACTIVE: w('Already running', 'neutral', 'A recovery is already running on this device.'),
    BLOCKED: w("Can't start", 'problem', "This one can't start; the reason is shown."),
  },
  recoverPreflight: {
    READY: w('Ready', 'good', 'Ready to start.'),
    'WAITING FOR SAFE WINDOW': w('Starts when safe', 'neutral', 'Starts at a quiet moment at the machine.'),
    WAITING_FOR_SAFE_WINDOW: w('Starts when safe', 'neutral', 'Starts at a quiet moment at the machine.'),
    BLOCKED: w("Can't start", 'problem', "This one can't start; the reason is shown."),
  },
  // G6. Recovery tab
  recoveryAction: {
    RETRY_DELIVERY: w('Send again', 'neutral', 'Sends these check-ins to the HR system again.'),
    REBUILD_OUTBOX: w('Queue again for sending', 'neutral', 'Puts these check-ins back in the sending queue.'),
    RECOVER_STALE_IN_FLIGHT: w('Restart stuck sends', 'neutral', 'Starts again the check-ins that got stuck while sending.'),
  },
  recoveryLane: {
    SAFE_DELIVERY: w('Safe to send', 'good', 'These check-ins can be sent safely.'),
    IDENTITY_HELD: w('Not matched to an employee', 'warning', 'These check-ins are not matched to an employee yet.'),
    SOURCE_CORRECTION: w('Time needs fixing', 'warning', 'The check-in time must be corrected first.'),
    PERMANENT_REVIEW: w('Waiting for you', 'warning', 'A person needs to look at these.'),
    CONFIRMED: w('Already in the HR system', 'good', 'The HR system already has these check-ins.'),
  },
  recoveryJob: {
    QUEUED: w('Waiting', 'neutral', 'Waiting to start.'),
    RUNNING: w('Working', 'neutral', 'Working now.'),
    PAUSE_REQUESTED: w('Pausing', 'neutral', 'Pausing after the current step.'),
    PAUSED: w('Paused', 'warning', 'Paused.'),
    STOPPING: w('Stopping', 'neutral', 'Stopping after the current step.'),
    STOPPED: w('Stopped', 'neutral', 'Stopped.'),
    CANCELLED: w('Stopped', 'neutral', 'Stopped.'),
    COMPLETED: w('Done', 'good', 'Finished.'),
    COMPLETED_WITH_ATTENTION: w('Done — needs review', 'warning', 'Finished; some items need a person to look at them.'),
    COMPLETED_WITH_REVIEW: w('Done — needs review', 'warning', 'Finished; some items need a person to look at them.'),
    NEEDS_REVIEW: w('Needs review', 'warning', 'A person needs to look at this.'),
    CHECKING: w('Checking', 'neutral', 'Checking the chosen check-ins.'),
    CHECKED: w('Checked', 'neutral', 'The check is finished.'),
    READY: w('Ready', 'good', 'Ready to start.'),
    WAITING_ORACLE: w('Waiting for the HR system', 'neutral', 'Waiting for the HR system to confirm.'),
    CREATED: w('Created', 'neutral', 'Created.'),
  },
  recoveryItem: {
    PENDING: w('Waiting its turn', 'neutral', 'Waiting its turn.'),
    SUCCEEDED: w('Done', 'good', 'Done.'),
    FAILED: w('Not done', 'problem', "This one couldn't be done."),
    SKIPPED: w('Skipped', 'neutral', 'Skipped.'),
  },
  // H1. Force release statuses
  forceItem: {
    CHECKING: w('Checking check-ins', 'neutral', 'Checking the chosen check-ins.'),
    CHECKED: w('Ready to review', 'neutral', 'The check is finished; review the results.'),
    RUNNING: w('Preparing to send', 'neutral', 'Preparing the check-ins to send.'),
    RELEASING: w('Preparing to send', 'neutral', 'Preparing the check-ins to send.'),
    WAITING_ORACLE: w('Waiting for the HR system', 'neutral', 'Saved for sending. Waiting for the HR system to confirm it.'),
    PAUSED: w('Paused', 'warning', 'Paused.'),
    STOPPING: w('Stopping — finishing what was queued', 'neutral', 'Stopping; check-ins already queued are finished first.'),
    STOPPED: w('Stopped', 'neutral', 'Stopped.'),
    EXPIRED: w('Check expired', 'warning', 'This check has expired. Check again.'),
    COMPLETED: w('Finished', 'good', 'Finished.'),
    COMPLETED_WITH_REVIEW: w('Finished — some need a look', 'warning', 'Finished; some check-ins need a closer look.'),
    BASELINE: w('Waiting', 'neutral', 'Waiting to start.'),
    SYNC_PENDING: w('Waiting', 'neutral', 'Waiting to re-read the list of people.'),
    SYNCING: w('Re-reading the list of people', 'neutral', "Re-reading the machine's list of people."),
    DONE: w('Prepared', 'good', 'Prepared.'),
    UNAVAILABLE: w("Couldn't re-read the list", 'warning', "Couldn't re-read the machine's list of people."),
    SKIPPED: w('Skipped after the re-check', 'neutral', 'Skipped after the re-check.'),
    READY: w('Ready to send', 'good', 'Ready to send.'),
    NEEDS_REVIEW: w("Can't be sent", 'warning', "This check-in can't be sent as it is."),
    CONFIRMED: w('Confirmed by the HR system', 'good', 'The HR system confirmed this check-in.'),
    EXCLUDED: w('Left out', 'neutral', 'Left out of this send.'),
  },
  // H5. Area release runs
  areaRun: {
    RELEASING: w('Releasing in batches', 'neutral', 'Check-ins are being released in batches.'),
    STOPPED_FOLLOWING: w('Stopped: following sent check-ins', 'neutral', 'Stopped releasing; following sent check-ins to the HR system.'),
    COMPLETED: w('Finished', 'good', 'Finished.'),
    STOPPED: w('Stopped', 'neutral', 'Stopped.'),
  },
  // Set up a device (08-advanced W3, prepare.md §3m)
  setup: {
    WAITING_FOR_COMPANION: w('Waiting for the setup app', 'warning', 'Open the setup app on this computer.'),
    WAITING_FOR_DEVICE: w('Waiting for the box to be plugged in', 'warning', 'Plug the link box into this computer.'),
    INSPECTING: w('Checking the box', 'neutral', 'Checking the link box.'),
    CONFIGURING: w('Enter details', 'neutral', 'Enter the details for this device.'),
    PREFLIGHT_READY: w('Details checked', 'neutral', 'The details are checked.'),
    AWAITING_AUTHORIZATION: w('Waiting for your approval', 'warning', 'Approve the setup to continue.'),
    PACKAGE_PREPARING: w('Getting the settings ready', 'neutral', 'Getting the settings ready.'),
    PACKAGE_READY: w('Settings ready', 'neutral', 'The settings are ready.'),
    EFUSE_BURNING: w('Applying the one-time security lock', 'neutral', 'Applying the one-time security lock. Do not unplug the box.'),
    EFUSE_VERIFIED: w('Security lock done', 'neutral', 'The security lock is done.'),
    FLASHING: w('Copying the software', 'neutral', 'Copying the software to the box. Do not unplug it.'),
    READBACK_VERIFYING: w('Checking the copy', 'neutral', 'Checking the copied software.'),
    LOCAL_VERIFIED: w('Copy checked', 'neutral', 'The copy is checked.'),
    BOOT_VERIFYING: w('Restarting the box', 'neutral', 'Restarting the box.'),
    WAITING_FOR_ONBOARDING: w('Waiting for the box to get online', 'warning', 'Waiting for the box to connect to the internet.'),
    WAITING_FOR_TERMINAL_CONFIRMATION: w('Confirm the machine', 'warning', 'Confirm this is the right attendance machine.'),
    VERIFYING_SITE: w('Final checks', 'neutral', 'Running the final checks.'),
    VERIFIED_ONLINE: w('Working', 'good', 'The device is working.'),
    SITE_VALIDATION_PENDING: w('Will finish at its office', 'warning', 'The setup finishes once the box is switched on at its office.'),
    RECOVERY_REQUIRED: w("Can't set up this box", 'problem', "This box can't be set up. Ask IT."),
    FAILED: w("Didn't finish", 'problem', "The setup didn't finish."),
    CANCELLED: w('Cancelled', 'neutral', 'The setup was cancelled.'),
    EXPIRED: w('Timed out', 'neutral', 'The setup took too long and was stopped.'),
  },
  boxType: {
    BLANK_NEW: w('Brand-new box', 'neutral', 'A brand-new link box.'),
    KNOWN_SECURE_MANAGED: w('One of our boxes', 'good', 'A link box already set up by State Life.'),
    UNKNOWN_FOREIGN: w("Unknown box (can't use)", 'problem', "This box isn't one of ours and can't be used."),
    KNOWN_LEGACY: w('Older box', 'neutral', 'An older link box.'),
  },
}

// The link box's own reported update state: F3 words plus four extras.
STATE_WORDS.otaState = {
  ...STATE_WORDS.update,
  UPDATING: w('Updating', 'neutral', 'The link box is being updated.'),
  WAIT: w('Waiting', 'neutral', 'The link box is waiting before the next update step.'),
  RETRY_SCHEDULED: w('Will try again', 'warning', 'The link box will try the update again.'),
  DEVICE_REPORTED_FAILURE: w('Update failed', 'problem', "The update didn't work; the link box kept its old version."),
}

const ALIASES: Partial<Record<Domain, Record<string, string>>> = {
  recoverEta: { Measuring: 'MEASURING', 'Device wait': 'DEVICE_WAIT', 'Oracle verifying': 'ORACLE_VERIFYING', 'Review needed': 'REVIEW_NEEDED', Stopped: 'STOPPED', Complete: 'COMPLETE' },
}

const normalize = (domain: Domain, code: string): string => {
  const alias = ALIASES[domain]?.[code]
  if (alias) return alias
  const trimmed = code.trim()
  return domain === 'coverageKey' ? trimmed : trimmed.toUpperCase()
}

/**
 * Plain words for one code in one domain. An unknown code gets "Other status"
 * (neutral) and `unknown: true`; its code goes under Technical details.
 */
export function plainStatus(domain: Domain, code: unknown): PlainStatus {
  const raw = typeof code === 'string' ? code : code === null || code === undefined ? '' : String(code)
  const key = raw ? normalize(domain, raw) : ''
  const word = key ? STATE_WORDS[domain]?.[key] : undefined
  if (word) return { ...word, code: raw, domain, unknown: false }
  return { ...UNKNOWN_STATUS, code: raw, domain, unknown: true }
}

/** True when the dictionary has words for this code. */
export const hasStatusWords = (domain: Domain, code: string) => !plainStatus(domain, code).unknown

/** Every domain the dictionary knows. */
export const STATE_DOMAINS = Object.keys(STATE_WORDS) as Domain[]

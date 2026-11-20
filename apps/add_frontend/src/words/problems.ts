// Problem words (02-words WORDS-5 frontend, Appendix C).
//
// Titles with numbers come from the backend (`plain_title`, `plain_detail`,
// `next_step`, built by be/problem_words.py). This file keeps what is static:
// the group, "Closes …" and the plain words used when those fields are missing
// (old cached responses, mixed deploys). Never the backend `message`.

import type { ActivityGroup } from './types'

export const GROUP_LABELS: Record<ActivityGroup, string> = {
  machine: 'Machine',
  link_box: 'Link box',
  memory: 'Link box memory',
  hr_system: 'HR system',
  people: 'People and CNIC',
  software: 'Software update',
}

/** Shared "what to do" for a link box that needs a power cycle (finalizer). */
export const LINK_BOX_POWER_CYCLE =
  'Ask someone at the place to unplug the small link box next to the machine, wait 10 seconds and plug it back in, at a quiet time (not at arrival or leaving time). The Restart button on this page restarts the attendance machine, not the link box. Do not do this if the card says "Do not switch the link box off".'

export interface ProblemInfo {
  group: ActivityGroup
  /** Title without numbers (the backend title adds them). */
  title: string
  detail: string
  next: string
  closesWhen: string
  /** Does this problem stop or delay check-ins? */
  affectsAttendance: boolean
}

const p = (group: ActivityGroup, title: string, detail: string, next: string, closesWhen: string, affectsAttendance = false): ProblemInfo => ({
  group, title, detail, next, closesWhen, affectsAttendance,
})

// C1. Every POLICY code (be/device_health.py).
export const PROBLEM_INFO: Record<string, ProblemInfo> = {
  QUARANTINED_DUPLICATE_SERIAL: p('machine', 'Two link boxes claim the same machine', 'Two link boxes say they are connected to the same machine, so changes to its people are blocked to keep records safe.', 'Find which place really has this machine and ask IT to correct the other link box.', 'Closes by itself when only one link box claims the machine.', true),
  ESP_FATAL: p('link_box', 'Link box has a serious fault', 'It reported a serious start-up fault and may not be sending any check-ins.', `${LINK_BOX_POWER_CYCLE} If it stays, call IT; it may need replacing.`, 'Closes by itself when the link box works normally again.', true),
  ZKT_SERIAL_MISMATCH: p('machine', 'Wrong machine connected', 'This device is linked to a different attendance machine than expected. Its check-ins are paused.', 'If the machine was replaced on purpose, confirm the new machine in Tools › Advanced tools. If not, connect the right machine again.', 'Closes by itself when the expected machine is connected again.', true),
  ESP_DURABILITY_FAULT: p('memory', 'Link box cannot save attendance safely', 'Its memory has a problem, so check-ins could be lost if the power or internet goes off.', 'Do not switch the link box off. Call IT. Do not unplug it, even if another card says so.', 'Closes by itself when the link box confirms its memory is healthy.', true),
  ESP_PRESERVATION_UNVERIFIED: p('memory', 'Link box memory not checked yet', 'A memory problem was closed by hand, but the link box has not yet shown its memory is healthy.', 'Nothing now. It closes after the link box gets software that can check its memory.', 'Closes by itself only.'),
  ESP_RESTART_LOOP: p('link_box', 'Link box keeps restarting', 'It keeps switching itself off and on, which usually means a power or cable problem.', "Check the link box's power adapter and socket; try another socket.", 'Closes by itself when it stays on for 30 minutes.', true),
  ESP_DELIVERY_WORKER_FAULT: p('link_box', 'Link box is stuck sending attendance', 'The part of the link box that sends check-ins has stopped. Check-ins are kept on the box until it recovers.', `Wait 10 minutes. If it stays: ${LINK_BOX_POWER_CYCLE} If it still stays, call IT.`, 'Closes by itself when sending works again.', true),
  HEARTBEAT_STALE: p('link_box', 'Link box stopped reporting', 'It is connected but has not reported for a few minutes.', `Wait a few minutes. If it continues: ${LINK_BOX_POWER_CYCLE}`, 'Closes by itself when the link box reports again.', true),
  ESP_LOCAL_FAILURE: p('link_box', 'Link box reported a problem', 'Its status light shows a memory fault. Check-ins may be delayed.', `${LINK_BOX_POWER_CYCLE} If it comes back, call IT.`, 'Closes by itself when the light is normal, or after the next restart.'),
  HIK_CAPTURE_UNHEALTHY: p('machine', 'Machine not giving attendance properly', "The machine isn't giving its attendance properly.", 'Ask IT to check the machine.', 'Closes by itself when the machine answers normally.', true),
  DEVICE_MESSAGE_REJECTED: p('link_box', 'Some information from this device was refused', "Information the link box sent wasn't in the expected form, so it was refused. The box keeps it and tries again.", 'Call IT; the link box may need a software update.', 'Closes by itself when that kind of information is accepted again. If some saved copies never arrive, someone must check the device and mark it fixed.'),
  ADD_MESSAGE_PROCESSING_FAILED: p('link_box', 'Some information from this device could not be processed', "The problem is on the dashboard's side, not at the place. The link box keeps the data and sends it again.", 'No visit is needed. Tell IT if it lasts more than an hour.', 'Closes by itself when the dashboard processes the data again.'),
  HIK_LIGHT_RECONCILE_BLOCKED: p('machine', 'Routine record check is stuck', "The regular check that compares the machine's saved check-ins with the dashboard's has stopped; live check-ins still arrive.", "Ask IT to look at this device's Activity.", 'Closes by itself when the check runs again.'),
  OTA_DEVICE_ROLLED_BACK: p('software', 'Software update undone', 'After installing the new software, the link box restarted and went back to its old version. It keeps working, and the update is paused.', "Check the link box's power at the place, then try the update again on Software updates.", 'Closes by itself after a later update to this device succeeds.'),
  OTA_DEVICE_REPORTED_FAILURE: p('software', 'Software update failed', "The link box couldn't download or check the new software. It keeps working on its old software, and the update is paused.", 'Check the internet at this place, then press Try again on Software updates.', 'Closes by itself after a later update to this device succeeds.'),
  ZKT_CONNECTION_FLAPPING: p('machine', 'Machine connection keeps dropping', 'The connection between the link box and the machine keeps breaking and coming back.', 'Check the cable and the network switch between them; replace a loose or damaged cable.', 'Closes by itself after 3 good connections in a row.', true),
  TERMINAL_DISCONNECTED: p('machine', 'Machine not responding', 'The link box has not reached the machine for a while.', 'Check the machine is switched on and its network cable is plugged in at both ends.', 'Closes by itself when the machine answers again.', true),
  TERMINAL_STABILIZING_STALLED: p('machine', 'Machine connection not settling', 'The machine came back but is not steady yet.', "Check the machine's cable; if it continues, restart the machine.", 'Closes by itself when the connection is steady.'),
  DELIVERY_AUTHORITY_UNKNOWN: p('link_box', 'Device setup needs checking', "After a restart, the link box hasn't finished getting ready to send check-ins.", 'Wait 15 minutes. If it stays, call IT.', 'Closes by itself when the link box finishes starting up.', true),
  ESP_OFFLINE: p('link_box', 'No internet', "The link box at this place has stopped reporting. It may be switched off, or the internet here isn't working.", 'Ask someone at the place to check the link box has power and the internet works. Check-ins stay saved on the machine.', 'Closes by itself when the link box reports again.', true),
  TERMINAL_LINK_DOWN: p('machine', 'Machine not responding', "The link box can't reach the attendance machine. It may be switched off or its cable unplugged.", 'Check the machine is switched on and its network cable is plugged in at both ends.', 'Closes by itself when the machine answers.', true),
  ZKT_CLOCK_DRIFT: p('machine', 'Machine clock is wrong', "The machine's clock is more than 2 minutes wrong, so new check-in times will be wrong too.", 'Set the correct date and time on the machine, or ask IT.', 'Closes by itself when the clock is within 2 minutes.'),
  HIK_CLOCK_DRIFT: p('machine', 'Machine clock is wrong', "The machine's clock is more than 2 minutes wrong, so new check-in times will be wrong too.", 'Set the correct date and time on the machine, or ask IT.', 'Closes by itself when the clock is within 2 minutes.'),
  ESP_CLOCK_SKEW: p('link_box', 'Link box clock is off (check-in times are fine)', "The link box's own clock is wrong, but check-in times are corrected automatically.", "Nothing. Don't change the machine's clock because of this.", 'Closes by itself when the link box clock is right.'),
  USER_SNAPSHOT_TRUNCATED: p('people', "List of people couldn't be read in full", "Only part of the machine's list of people arrived, so adding or changing people on this machine is paused.", 'Open the device, go to People and press "Refresh people list". If it happens again, call IT.', 'Closes by itself when a complete list arrives.'),
  HISTORY_BACKFILL_BLOCKED: p('people', "Older check-ins couldn't be matched to people", "While reading this machine's older check-ins, some couldn't be matched to people, so nothing was changed in the HR system.", "Open People for this machine › Old check-ins and enter the people's details; reading continues by itself afterwards.", 'Closes by itself when older check-ins can be read again.'),
  ORDS_DELIVERY_FAILED: p('hr_system', 'Check-ins not reaching the HR system (trying again)', "Check-ins from this place are saved and are being sent again automatically; the HR system hasn't accepted them yet.", 'Nothing at the place. If it lasts more than an hour, tell IT.', 'Closes by itself when the HR system accepts the check-ins.', true),
  ORDS_EVENT_REJECTED: p('hr_system', 'HR system refused a check-in', 'The HR system refused one check-in from this place; the others continue normally.', "Open Attendance with the Problem filter, check the person's CNIC and send it again.", 'Closes when someone reviews the refused check-in.', true),
  CHECKIN_NOTIFY_DEGRADED: p('hr_system', 'Check-in phone messages delayed', "Staff phone messages about their check-ins are late or failing. Check-ins and sending to the HR system aren't affected.", 'Nothing at the place. Tell IT if it lasts for hours.', 'Closes by itself when phone messages go out again.'),
  ATTENDANCE_EVENT_QUARANTINED: p('link_box', 'Check-ins set aside for checking', 'Some check-in records arrived damaged or unreadable, so they were set aside; later check-ins went through normally.', 'Press "Check them" and mark each one as checked.', 'Closes when every set-aside check-in has been checked.', true),
  ATTENDANCE_TIMESTAMP_QUARANTINED: p('machine', 'Check-in with an impossible date or time', "A check-in had a date or time that can't be right (often the machine clock was wrong), so it was kept but not sent to the HR system.", 'Correct the machine clock, then open Recover attendance › Unreadable records and press "Fix impossible times".', "Closes when someone reviews the check-in's date and time.", true),
  ATTENDANCE_REPAIR_NEEDS_ATTENTION: p('people', 'Attendance fix needs your decision', 'An attendance fix started for this machine has stopped and is waiting for someone to decide.', 'Open the attendance fix for this machine and continue or cancel it.', 'Closes by itself when the attendance fix finishes.'),
  ATTENDANCE_REPAIR_STALLED: p('people', 'Attendance fix is stuck', "An attendance fix hasn't moved for 10 minutes. Saved check-ins are safe.", "Check that this place's machine and link box are online; the fix continues by itself.", 'Closes by itself when the attendance fix finishes.'),
  ADD_SOURCE_COVERAGE_INVALIDATED: p('machine', "Machine records no longer match the dashboard's copy", "The machine's saved check-ins changed in a way that doesn't match the dashboard's copy (for example after a reset), so automatic reading stopped to keep records safe.", 'Run Recover attendance for this machine; tell IT if it happens again.', "Closes when IT reviews this machine's records."),
  ADD_SOURCE_GENERATION_LINEAGE_DIVERGED: p('machine', 'Machine history reads differently after a reset', 'After the machine was reset or changed, its older check-ins read differently from the saved copy; nothing was changed.', 'Ask IT to review the records for this machine.', "Closes when IT reviews this machine's records."),
  ADD_SOURCE_COVERAGE_UNVERIFIED: p('machine', "Recovery result couldn't be checked", "A recovery finished but its result couldn't be checked. Nothing was changed.", "Ask IT to review it. Cancelling the run doesn't close this.", "Closes when IT reviews this machine's records."),
  ADD_SOURCE_TAIL_STALE_GENERATION: p('link_box', 'Link box is not keeping a backup copy of new check-ins', 'Link box is not keeping a backup copy of new check-ins. Live check-ins still arrive.', `${LINK_BOX_POWER_CYCLE} If it continues, call IT.`, 'Closes by itself when the link box uses the latest record settings.'),
  TERMINAL_SOURCE_EXCEPTION: p('machine', "Machine records couldn't be read", 'Some records saved on the machine were unreadable; they were kept for checking and reading continued.', 'Press "See the records" to review them.', 'Closes when someone reviews the unreadable records.'),
  TERMINAL_NEWEST_RECORD_CORRECTED: p('machine', 'A corrected check-in needs HR to confirm', 'A check-in time was corrected using a record that later proved wrong; the corrected check-in is set aside.', 'Ask HR to confirm it, then mark this problem as fixed.', 'Closes when HR has checked the check-in and someone marks it fixed.', true),
  TERMINAL_NEWEST_RECORD_CHANGED: p('machine', 'A check-in may belong to someone else', 'A record read from the machine changed later, so one check-in may be for the wrong person; it is set aside.', 'Ask HR to check this check-in with the person, then mark this problem as fixed.', 'Closes when HR has checked the check-in and someone marks it fixed.', true),
  TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED: p('machine', 'Recent check-ins may be missing', "The machine's newest record may have a check-in the dashboard hasn't received yet.", 'After the next check-in on this machine, run Recover attendance for it.', 'Closes by itself once the check-in arrives or Recover attendance reads it.', true),
  TERMINAL_NEWEST_RECORD_RELEASE_HELD: p('people', 'Check-ins set aside for safety', "These check-ins couldn't be matched to a person safely, so they are set aside until Recover attendance confirms them.", 'Run Recover attendance for this machine; they are sent by themselves afterwards.', 'Closes by itself once Recover attendance confirms these check-ins.', true),
  USER_DELETION_JOB_INCOMPLETE: p('people', 'Some people were not removed', "A removal of several people from this machine didn't finish for everyone.", 'Open People for this machine, check who is still there and remove them again.', 'Closes by itself when the removal finishes.'),
  DUPLICATE_USER_CNIC: p('people', 'People share one CNIC', 'More than one person on this machine has the same CNIC, so check-ins could be counted for the wrong person.', 'Open People for this machine and correct the CNIC, or confirm they are the same person.', 'Closes by itself when the CNIC is corrected.', true),
  USER_CNIC_FORMAT_SUSPECT: p('people', 'People have a wrongly typed CNIC', "Their CNIC is written in a form that can't be used (for example with dashes or a letter), so their check-ins are not matched.", 'Confirm each CNIC with HR and enter it in People.', 'Closes by itself at the next read of the list of people once the CNICs are fixed.', true),
  ORACLE_RECEIPT_CONNECTOR_MISMATCH: p('hr_system', "Link box may be set up with another place's details", 'This link box reported sending a check-in that belongs to a different place, so the report was ignored.', "Call IT; this link box may be set up with another place's details.", "Closes when this link box's sending reports match this place."),
  ADMIN_REVOKE_OVERDUE: p('people', 'Temporary admin access not removed', "Someone was given admin rights on the machine, and they couldn't be removed automatically.", 'Make sure the device is online so the removal can retry. If it stays, remove the admin rights on the machine itself and tell IT.', 'Closes by itself when the temporary admin access is removed.'),
}

/** Where the "What to do" link button goes (finalizer: PROBLEM_LINKS). Every other code gets [Open device]. */
export const PROBLEM_LINKS: Record<string, { label: string; to: string }> = {
  USER_SNAPSHOT_TRUNCATED: { label: 'Open People', to: '/people/{device}' },
  HISTORY_BACKFILL_BLOCKED: { label: 'Open Old check-ins', to: '/people/{device}?tab=old' },
  ATTENDANCE_TIMESTAMP_QUARANTINED: { label: 'Open Unreadable records', to: '/recover?tab=unreadable&problem=time' },
  ORDS_EVENT_REJECTED: { label: 'Open Attendance', to: '/attendance?status=problem' },
}

const GROUP_BY_PREFIX: Array<[RegExp, ActivityGroup]> = [
  [/^(ZKT|HIK|TERMINAL|ADD_SOURCE)_/, 'machine'],
  [/^(ORDS|ORACLE|CHECKIN)_/, 'hr_system'],
  [/^(USER|DUPLICATE|ADMIN|ATTENDANCE_REPAIR)_/, 'people'],
  [/^OTA_/, 'software'],
  [/^ESP_(DURABILITY|PRESERVATION)/, 'memory'],
]

/** Group of a problem code, from C1 or the code's prefix (else Link box). */
export function problemGroup(code: string | null | undefined): ActivityGroup {
  if (!code) return 'link_box'
  const known = PROBLEM_INFO[code]
  if (known) return known.group
  return GROUP_BY_PREFIX.find(([pattern]) => pattern.test(code))?.[1] ?? 'link_box'
}

/** C1 unknown row: "{Group} problem", "Open the device for details." */
export function problemFallback(group: ActivityGroup): Pick<ProblemInfo, 'title' | 'detail' | 'next' | 'closesWhen'> {
  return {
    title: `${GROUP_LABELS[group]} problem`,
    detail: 'The dashboard reported something this page has no words for yet.',
    next: 'Open the device for details.',
    closesWhen: 'Ask IT how this one closes.',
  }
}

export interface ProblemSource {
  code?: string | null
  plain_title?: string | null
  plain_detail?: string | null
  next_step?: string | null
}

export interface ProblemWords {
  code: string
  group: ActivityGroup
  groupLabel: string
  title: string
  detail: string
  next: string
  closesWhen: string
  link?: { label: string; to: string }
  unknown: boolean
}

/**
 * The words of one problem card or health reason. Uses the backend plain
 * fields when present; falls back to C1 by code, then by group. Never the
 * backend `message` (that goes to Technical details).
 */
export function problemWords(source: ProblemSource): ProblemWords {
  const code = source.code ?? ''
  const info = PROBLEM_INFO[code]
  const group = problemGroup(code)
  const fallback = info ?? problemFallback(group)
  return {
    code,
    group,
    groupLabel: GROUP_LABELS[group],
    title: source.plain_title?.trim() || fallback.title,
    detail: source.plain_detail?.trim() || fallback.detail,
    next: source.next_step?.trim() || fallback.next,
    closesWhen: fallback.closesWhen,
    link: PROBLEM_LINKS[code],
    unknown: !info,
  }
}

// C3. How a problem was fixed (details.resolution.kind).
export const RESOLUTION_WORDS: Record<string, string> = {
  VERIFIED: 'Fixed (the link box confirmed it)',
  CONDITION_CLEARED: 'Fixed by itself',
  MESSAGE_ACCEPTED: 'Fixed by itself (information accepted)',
  DEPLOYMENT_SUCCEEDED: 'Fixed by a later software update',
  BOOT_ENDED: 'Fixed by a restart',
  OPERATOR: 'Marked fixed by {name}',
  CLEANUP: 'Closed by the old-problems clean-up',
  EVIDENCE_HELD: 'Fixed by itself (copy already saved)',
  CURRENT_GENERATION_TAIL_COMMITTED: 'Fixed by itself (link box updated)',
  NEWEST_RECORD_SLOTS_ACCOUNTED: 'Fixed by itself (check-ins arrived)',
  NEWEST_RECORD_PUNCHES_NO_LONGER_HELD: 'Fixed by itself (check-ins sent)',
}
export const RESOLUTION_FALLBACK = 'Fixed by itself'

/** "Marked fixed by Sara" / "Fixed by a restart" / "Fixed by itself". */
export function resolutionWords(kind: string | null | undefined, name?: string | null): string {
  const text = (kind && RESOLUTION_WORDS[kind.toUpperCase()]) || RESOLUTION_FALLBACK
  return text.replace('{name}', name?.trim() || 'an admin')
}

// C4. Why it can't be marked as fixed by hand (operator.refusal_code).
export const REFUSAL_WORDS: Record<string, string> = {
  DERIVED_REASON: 'Closes by itself.',
  ALERT_WORKFLOW_OWNED: 'Closes when its task is finished on its own page.',
  ALERT_CONDITION_CURRENT: "Still happening, so it can't be marked as fixed yet.",
}
const REFUSAL_BY_CODE: Record<string, string> = {
  ESP_PRESERVATION_UNVERIFIED: 'Closes after a software update that can check the link box memory.',
  QUARANTINED_DUPLICATE_SERIAL: 'Another link box may still claim this machine.',
}
export const REFUSAL_HELD_SINCE_RESTART = 'Waiting for the link box to confirm it since its last restart.'
export const REFUSAL_FALLBACK = "This one can't be marked as fixed by hand."

/** C4 sentence for a refusal, with the reason code and its currency when known. */
export function refusalWords(refusalCode: string | null | undefined, reasonCode?: string | null, currency?: string | null): string {
  if (refusalCode === 'ALERT_CONDITION_CURRENT') {
    if (reasonCode && REFUSAL_BY_CODE[reasonCode]) return REFUSAL_BY_CODE[reasonCode]
    if (currency === 'HELD') return REFUSAL_HELD_SINCE_RESTART
  }
  return (refusalCode && REFUSAL_WORDS[refusalCode]) || REFUSAL_FALLBACK
}

/** "Marked as seen by {name} · {when}. It stays open until it is fixed." */
export const seenByLine = (name: string, when: string) => `Marked as seen by ${name} · ${when}. It stays open until it is fixed.`

/** The device health line that used to say "Holds HIL and factory work while active." (Technical details only). */
export const TEST_UPDATES_WAIT = 'While open, test-device updates wait.'

// ---------------------------------------------------------------------------
// Problems page words (05-problems "Words"; PRB-5 … PRB-8). Fixed text of the
// page, the card, Mark as seen / fixed, the set-aside sheet and the clean-up
// tool. Codes never appear here; they go to Technical details.

/** Type filter (`?type=`); keys are the backend's PROBLEM_TYPES (be/problem_words.py). */
export const PROBLEM_TYPE_LABELS: Record<string, string> = {
  link_box: 'Link box',
  link_box_memory: 'Link box memory',
  machine: 'Machine',
  checkins: 'Check-ins and records',
  hr_system: 'HR system',
  people: 'People and CNIC',
  software: 'Software update',
  phone: 'Phone messages',
}

export const PROBLEMS_PAGE = {
  title: 'Problems',
  description: 'Problems at your devices, most serious first. A problem closes by itself once it is fixed.',
  refresh: 'Refresh problems',
  updated: (when: string) => `Updated ${when}`,
  cleanUp: 'Clean up old problems…',
  showTechnical: 'Show technical details',
  hideTechnical: 'Hide technical details',
  tabsLabel: 'Problem status',
  filtersLabel: 'Problem filters',
  tabs: { new: 'New', seen: 'Seen', fixed: 'Fixed' },
  tabLine: {
    new: 'Nobody has looked at these yet.',
    seen: 'Someone has looked. They stay here until they are fixed.',
  },
  fixedLine: {
    any: 'Every problem that was fixed.',
    today: 'Closed today.',
    yesterday: 'Closed yesterday.',
    '7d': 'Closed in the last 7 days.',
    '30d': 'Closed in the last 30 days.',
    custom: (from: string, to: string) => from && to ? `Closed between ${from} and ${to}.` : from ? `Closed since ${from}.` : to ? `Closed until ${to}.` : 'Every problem that was fixed.',
  },
  search: 'Search device or problem',
  seriousLabel: 'How serious',
  serious: { urgent: 'Urgent', important: 'Important', low: 'Low' },
  affectsAttendance: 'Affects attendance',
  type: 'Type',
  anyType: 'Any type',
  device: 'Device',
  allDevices: 'All devices',
  date: 'Date',
  dates: { any: 'Any time', today: 'Today', yesterday: 'Yesterday', '7d': 'Last 7 days', '30d': 'Last 30 days', custom: 'Pick dates' },
  from: 'From',
  to: 'To',
  sort: 'Sort',
  sorts: { serious: 'Most serious first', newest: 'Newest first', oldest: 'Oldest first' },
  sparesNote: 'Spare devices are not shown.',
  filterChip: {
    type: (label: string) => `Type: ${label}`,
    device: (name: string) => `Device: ${name}`,
    search: (text: string) => `Search: ${text}`,
    oldMarks: 'Old “seen” marks',
  },
  loading: 'Loading problems…',
  loadError: "Couldn't load problems.",
  loadErrorHint: 'Check your internet and try again.',
  stale: (time: string) => `Couldn't update. Showing what we had at ${time}.`,
  tryAgain: 'Try again',
  trying: 'Trying…',
  empty: {
    nothingNew: 'Nothing new to look at.',
    allSeen: (count: string, one: boolean) => `Every open problem has been seen. ${count} ${one ? 'is' : 'are'} in Seen.`,
    goToSeen: 'Go to Seen',
    allClear: 'All clear.',
    allClearHint: 'No open problems. New problems show up here as soon as a device reports one.',
    seen: 'Nothing marked as seen is still open.',
    fixed: 'Nothing fixed in this time.',
    fixed7d: 'Nothing fixed in the last 7 days.',
    show30d: 'Show last 30 days',
    division: (division: string) => `No open problems in ${division}.`,
    filtered: 'Nothing matches these filters.',
    filteredHint: 'Try fewer filters.',
    clearFilters: 'Clear filters',
    oldMarks: 'No old “seen” marks are left.',
  },
  oldMarks: {
    title: (marks: string, one: boolean) => `${marks} from before 10 Oct ${one ? 'is' : 'are'} hidden.`,
    text: "They were made by an older version of the dashboard. They do not change any device's status, and they never close by themselves.",
    show: 'Show them',
    hide: 'Back to Seen',
    cleanUp: 'Clean up…',
    viewing: 'These are old “seen” marks from before 10 Oct. They do not change any device’s status.',
  },
  showMore: 'Show more',
  loadingMore: 'Loading…',
  showing: (shown: string, total: string) => `Showing ${shown} of ${total}`,
  newPill: (count: string) => `${count} ↑`,
  announce: (place: string, title: string) => `New problem at ${place}: ${title}`,
  setAsideLink: (count: string) => `Set-aside check-ins: ${count}`,
}

export const PROBLEM_GROUP = {
  since: (when: string, length: string) => `since ${when} · ${length}`,
  openDevice: 'Open device',
  expand: (name: string) => `Show problems at ${name}`,
  collapse: (name: string) => `Hide problems at ${name}`,
  today: 'Today',
  yesterday: 'Yesterday',
}

export const PROBLEM_CARD = {
  severityPrefix: 'How serious',
  new: 'New',
  seen: 'Seen',
  fixed: 'Fixed',
  whatToDo: 'What to do:',
  started: (when: string, length: string, last: string) => `Started ${when} · ${length} ago · last report ${last}`,
  phoneSince: (when: string, length: string) => `Since ${when} · ${length}`,
  seenBy: (name: string, when: string) => `Seen by ${name} · ${when}`,
  seenByYou: (when: string) => `Seen by you · ${when}`,
  markSeen: 'Mark as seen',
  marking: 'Marking…',
  markSeenWithNote: 'Mark as seen with a note…',
  addNote: 'Add a note',
  markFixed: 'Mark as fixed…',
  copyForIt: 'Copy details for IT',
  openDeviceLabel: 'Open device',
  copied: 'Copied',
  more: (title: string) => `More actions for ${title}`,
  showDetails: 'Show details',
  hideDetails: 'Hide details',
  markedFixed: 'Marked as fixed',
  fixedJustNow: 'Fixed just now',
  seenError: "Couldn't mark it as seen. Try again.",
  alreadyFixed: 'This problem was already fixed.',
  fixedAt: (how: string, time: string, length: string) => `${how} at ${time} · lasted ${length}`,
  fixedByPerson: (name: string, time: string) => `Marked fixed by ${name} at ${time}`,
  cleanup: (name: string, time: string) => `Closed in a clean-up by ${name} at ${time}`,
  facts: {
    code: 'Code',
    message: 'Message from the system',
    details: 'Details',
    number: 'Problem number',
    started: 'Started',
    lastReport: 'Last report',
    deviceCode: 'Device code',
    linkBox: 'Link box',
    refusal: 'Why it cannot be closed by hand',
  },
}

export const MARK_SEEN = {
  title: 'Mark as seen',
  note: 'Note for your team (optional)',
  placeholder: 'For example: Branch called, power is back at 2 pm',
  cancel: 'Cancel',
  submit: 'Mark as seen',
  toast: 'Marked as seen. It stays open until it is fixed.',
  view: 'View',
}

export const MARK_FIXED = {
  title: 'Mark this problem as fixed',
  comesBack: 'If it comes back, it shows up again as a new problem.',
  reminder: 'Closing this leaves a reminder open: “Link box memory not checked yet”.',
  reason: 'What did you do?',
  reasonHelp: 'Saved with your name. At least 10 letters.',
  placeholder: 'For example: Restarted the link box at the branch',
  more: (count: number) => `${count} more ${count === 1 ? 'letter' : 'letters'} needed`,
  password: 'Your password',
  passwordHelp: "To confirm it's you.",
  cancel: 'Cancel',
  submit: 'Mark as fixed',
  busy: 'Saving…',
  toast: 'Marked as fixed.',
  toastReminder: 'A reminder stays open: “Link box memory not checked yet”.',
  view: 'View',
  refusals: {
    ALERT_NOT_ACTIVE: 'Already fixed. It fixed itself, or someone else closed it.',
    ALERT_CONDITION_CURRENT: "The device still has this problem, so it can't be closed by hand yet. It closes by itself once it is fixed.",
    ALERT_WORKFLOW_OWNED: 'This closes by itself when its task is finished.',
    IDEMPOTENCY_KEY_REUSED: 'Something went wrong. Press Mark as fixed again.',
  } as Record<string, string>,
  wrongPassword: "That password isn't right. Type it again.",
  failed: "That didn't work. Try again.",
}

export const SET_ASIDE = {
  title: 'Check-ins set aside for checking',
  subtitle: (place: string, count: string) => `${place} · ${count} to check`,
  allPlaces: 'All devices',
  intro: 'These records arrived damaged, so they were kept safely and not sent. All other check-ins went through normally.',
  received: (when: string) => `Received ${when}`,
  rowProblem: 'A value in the record could not be read',
  check: 'Check',
  empty: 'No check-ins waiting to be checked.',
  allChecked: 'All checked.',
  loadError: "Couldn't load the set-aside check-ins. Try again.",
  showMore: 'Show more',
  back: 'Back to the list',
  reviewTitle: 'Check a set-aside check-in',
  reviewSubtitle: (place: string, when: string) => `${place} · received ${when}`,
  chip: 'Set aside',
  others: 'Other check-ins from the same delivery went through normally.',
  explain: 'Checking this does not add or resend any check-in. It only records that someone looked at it. The original record shows only after you enter your password, and it hides again by itself.',
  reason: 'What did you find?',
  placeholder: 'For example: Record is damaged, nothing to send',
  password: 'Your password',
  reveal: 'Show original record',
  revealing: 'Checking password…',
  review: 'Mark as checked',
  reviewing: 'Saving…',
  original: 'Original record',
  hide: 'Hide',
  hidesIn: (seconds: number) => `Hides in ${seconds} s, or when you leave this page.`,
  showRaw: 'Show raw data',
  reviewed: 'Marked as checked.',
  revealed: 'Showing the original record for 60 seconds.',
  failed: "That didn't work. Try again.",
  facts: { code: 'Problem code', path: 'Which part', receipt: 'Delivery number', digest: 'Record fingerprint', item: 'Item in the delivery' },
  fields: { employee: 'Employee ID', time: 'Time', record: 'Machine record number', type: 'Check-in type' },
}

export const CLEAN_UP = {
  title: 'Clean up old problems',
  description: 'Find problems left over from earlier restarts or already solved, and close the ones you pick.',
  step1: '1. Choose devices',
  scopeLabel: 'Devices to check',
  pick: 'Pick devices',
  pickHelp: (count: string) => `Up to 50 · ${count} picked`,
  all: 'All devices',
  allHelp: 'Every device in use, not spares',
  find: 'Find a device',
  findPlaceholder: 'Device name',
  pickerLabel: 'Devices to pick from',
  openCount: (count: string) => count,
  noMatch: 'No device matches this search.',
  preview: 'Find old problems',
  previewing: 'Looking…',
  again: 'Look again',
  readOnly: 'This only looks. Nothing changes until you press Close problems.',
  step2: '2. Check the list',
  summary: (rows: string, devices: string, picked: string, until: string) => `${rows} at ${devices} · ${picked} picked · this list is valid until ${until}`,
  classesLabel: 'Kinds of old problems',
  statusNow: 'Status now:',
  statusAfter: 'After closing:',
  savedProblem: (before: string, after: string) => `Saved device problem: ${before} → ${after}`,
  noSavedProblem: 'No problem',
  leavesReminder: 'Leaves a reminder open: “Link box memory not checked yet”.',
  fixLabel: (name: string, before: string, after: string) => `Update ${name}'s saved problem from “${before}” to “${after}”`,
  close: (title: string) => `Close ${title}`,
  why: 'Why it is old',
  lastReport: 'Last report',
  notKnown: 'Not known',
  noDetails: 'No details.',
  checkFirst: 'Check before picking',
  memoryReminder: 'Leaves a memory reminder open',
  none: 'No old problems on these devices.',
  blocked: "Devices that can't be cleaned up now",
  skipped: 'Left as they are',
  blockedReason: 'A test is running on this device.',
  skippedOffline: "The device is offline, so we can't check it.",
  skippedOther: 'It could not be checked this time.',
  step3: '3. Close the picked problems',
  applyIntro: 'Each picked problem is closed and saved with your name. If a problem is still real, it shows up again by itself.',
  pickOne: 'Pick at least one problem.',
  pickAtMost: (count: string) => `Pick at most ${count} at a time.`,
  reason: 'What did you do?',
  reasonPlaceholder: 'For example: Old problems from the October recovery',
  typed: 'to confirm',
  typePrefix: 'Type',
  password: 'Your password',
  apply: 'Close problems',
  applying: 'Closing…',
  cancel: 'Close',
  done: (closed: string, reminders: string) => `Done. ${closed} closed · ${reminders} left open. Each device's status updates within a minute.`,
  toast: 'Old problems closed.',
  outOfDate: 'This list is out of date. Nothing was changed. Press Look again.',
  refusals: {
    CLEANUP_DISABLED: 'Closing old problems is switched off right now. Nothing was changed.',
    PREVIEW_INVALID: 'This list is out of date. Nothing was changed. Press Look again.',
    PREVIEW_EXPIRED: 'This list is out of date. Nothing was changed. Press Look again.',
    SCOPE_CHANGED: 'This list is out of date. Nothing was changed. Press Look again.',
    SELECTION_OUT_OF_SCOPE: 'This list is out of date. Nothing was changed. Press Look again.',
    CONFIRMATION_MISMATCH: "The typed words don't match. Type them exactly as shown.",
    IDEMPOTENCY_KEY_REUSED: 'Something went wrong. Press Close problems again.',
  } as Record<string, string>,
  previewFailed: "Couldn't look for old problems. Try again.",
  applyFailed: "Couldn't close the problems. Try again.",
  wrongPassword: "That password isn't right. Type it again.",
  facts: { problem: 'Problem', software: 'Software', softwareNow: 'Software now', restart: 'Restart', holds: 'Test-device hold', request: 'Request', list: 'List', code: 'Code' },
}

// One device page words (04-device): the header sentence, tab names and the
// fixed words of Summary, Problems, People and Tools. Status words come from
// deviceStatus.ts (HOME-7, D-1: a machine that is not responding counts as
// Offline, exactly like /status); problem titles from problems.ts; request
// failures and the machine password (COMM Key) words from requests.ts.
//
// Imported only from the lazy device chunk.

import type { Device } from '../types'
import { deviceStatus, OFFLINE_REASONS, type DeviceStatus, type PublicStatusField } from './deviceStatus'
import { problemWords } from './problems'
import { agoText, dayTimeText, sinceText } from './time'

/** A device row as the detail answer sends it (read-side fields of DEV-3 / HOME-2). */
export type DeviceDetail = Device & {
  public_status?: PublicStatusField | null
  status_since?: string | null
  offline_reason?: string | null
  attendance_today?: AttendanceToday | null
  place?: string | null
}

export interface AttendanceToday {
  day: string
  last_checkin_at: string | null
  checkins_today: number
  sent_to_hr_today: number
  waiting_to_send: number
  not_matched_today: number
  computed_at: string | null
}

export type DeviceTab = 'summary' | 'activity' | 'problems' | 'people' | 'tools'
export const DEVICE_TABS: readonly DeviceTab[] = ['summary', 'activity', 'problems', 'people', 'tools']
export const DEVICE_TAB_LABELS: Record<DeviceTab, string> = {
  summary: 'Summary',
  activity: 'Activity',
  problems: 'Problems',
  people: 'People',
  tools: 'Tools',
}

/** Health older than this is not trusted for "Working" (fail closed). */
export const STATUS_STALE_MS = 2 * 60_000

export const DEVICE_FRAME = {
  sections: 'Device sections',
  back: 'Back',
  previous: 'Previous device',
  next: 'Next device',
  lastHeard: (ago: string) => `Last heard from ${ago}`,
  never: 'Never',
  refreshFailed: "Couldn't refresh.",
  notFound: 'This device was not found. It may have been removed.',
  backToDevices: 'Back to devices',
  copyLink: 'Copy link',
  openPeople: 'Open in People',
  linkCopied: 'Link copied',
  nowStatus: (name: string, chip: string) => `${name} is now ${chip.toLowerCase()}.`,
  oneNote: '1 note',
} as const

/** "9:24 am · 3 h 20 min" (writing rule 5, Pakistan time). */
export function sinceWords(since: string | null | undefined, now: Date = new Date()): string {
  if (!since) return ''
  return `${dayTimeText(since, now)} · ${sinceText(since, now)}`
}

export interface HeaderStatus {
  status: DeviceStatus
  /** The one plain sentence under the name. */
  sentence: string
  /** True when the status is not confirmed (old or failed data). */
  stale: boolean
  /** "1 note" tag next to a Working chip. */
  noteTag: string | null
}

const healthAge = (device: DeviceDetail, now: Date) => {
  // The newest of the detail's evaluation and the live row's snapshot time.
  const times = [device.health?.evaluated_at, device.snapshot_at].map((value) => (value ? Date.parse(value) : NaN)).filter(Number.isFinite)
  return times.length ? now.getTime() - Math.max(...times) : null
}

/**
 * The header status of one device: the shared deviceStatus() chip plus the
 * sentence table of 04-device §1. Never says "Working" from old data.
 */
export function headerStatus(device: DeviceDetail, now: Date = new Date(), { detailFailed = false } = {}): HeaderStatus {
  const status = deviceStatus(device)
  const since = device.status_since ?? status.since ?? null
  const at = (text: string) => (since ? `${text} since ${sinceWords(since, now)}` : text)
  const age = healthAge(device, now)
  const stale = detailFailed || (age !== null && age > STATUS_STALE_MS && status.group === 'working')
  const lastUpdate = device.health?.evaluated_at ?? device.snapshot_at ?? device.last_seen_at
  if (stale && !device.is_spare) {
    return { status, sentence: `Status not confirmed. Last update ${lastUpdate ? agoText(lastUpdate, now) : 'not known'}.`, stale: true, noteTag: null }
  }
  if (status.group === 'spare') return { status, sentence: 'Spare (not in use). Not counted in totals or problems.', stale: false, noteTag: null }
  if (status.group === 'setup') return { status, sentence: 'Being set up. Not counted in totals yet.', stale: false, noteTag: null }
  if (status.code === 'QUARANTINED_DUPLICATE_SERIAL') {
    return { status, sentence: `${since ? `Blocked since ${sinceWords(since, now)}` : 'Blocked'}: another link box says it is connected to this same machine.`, stale: false, noteTag: null }
  }
  if (status.group === 'offline') {
    const reason = device.offline_reason && OFFLINE_REASONS[device.offline_reason] ? device.offline_reason : status.code
    if (reason === 'not_set_up') return { status, sentence: `Not set up yet. ${OFFLINE_REASONS.not_set_up.sentence}`, stale: false, noteTag: null }
    if (reason === 'no_internet') return { status, sentence: `${at('Offline')}. ${OFFLINE_REASONS.no_internet.sentence}`, stale: false, noteTag: null }
    const words = OFFLINE_REASONS[reason] ?? { heading: status.heading, sentence: status.sentence }
    return { status, sentence: `${at(words.heading)}. ${words.sentence}`, stale: false, noteTag: null }
  }
  if (status.group === 'attention') {
    return { status, sentence: `${at(status.heading || 'Needs attention')}.`, stale: false, noteTag: null }
  }
  if (status.note) {
    return { status, sentence: `${at('Working')}. ${status.note}`, stale: false, noteTag: DEVICE_FRAME.oneNote }
  }
  return { status, sentence: `${at('Working')}. Check-ins are arriving normally.`, stale: false, noteTag: null }
}

/** "ZKTeco" / "Hikvision" from terminal_vendor or the software family. */
export function machineBrand(device: Pick<Device, 'terminal_vendor' | 'firmware_family'>): 'ZKTeco' | 'Hikvision' {
  const vendor = device.terminal_vendor ?? device.firmware_family
  return vendor === 'hikvision' ? 'Hikvision' : 'ZKTeco'
}

/** Link box software "2.8.2" (no "Zone Lite", no build prefix). */
export const softwareVersion = (value: string | null | undefined) => (value ? value.replace(/^zone-lite-/i, '') : '')

/** Machine clock fact: "Correct" (within 2 min) / "3 min slow" / "2 min fast" / "Not checked yet". */
export function machineClock(drift: number | null | undefined, sampledAt: string | null | undefined): string {
  if (!sampledAt || drift === null || drift === undefined || !Number.isFinite(drift)) return 'Not checked yet'
  if (Math.abs(drift) <= 120) return 'Correct'
  const minutes = Math.max(1, Math.round(Math.abs(drift) / 60))
  // drift = machine time minus true time: positive is fast.
  return `${minutes} min ${drift > 0 ? 'fast' : 'slow'}`
}

export const SUMMARY_WORDS = {
  whatsHappening: "What's happening",
  everythingWorking: 'Everything is working',
  workingMeaning: 'Check-ins from this machine are reaching the dashboard.',
  spareTitle: 'Spare (not in use)',
  spareMeaning: 'This spare is still watched, but it is not counted in totals or problems.',
  blockedTitle: 'Blocked: duplicate device',
  setupTitle: 'Being set up',
  arriving: 'Check-ins still arrive.',
  notArriving: 'Check-ins are not arriving.',
  whatToDo: (action: string) => `What to do: ${action.replace(/\.$/, '')}.`,
  clearsWhen: (sentence: string) => `It clears by itself when ${sentence.replace(/^(it )?(closes|clears)( by itself)? (when|once|after) /i, '').replace(/\.$/, '')}.`,
  alsoOpen: (title: string) => `Also open: ${title}`,
  seeAll: (count: number) => `See all ${count}`,
  serialMismatch: 'A different machine is connected. Ask IT to confirm the new machine in Tools › Advanced tools.',
  openAdvanced: 'Open Advanced tools',
  putBack: 'Put back in use',
  // Key facts
  place: 'Place',
  division: 'Division',
  machine: 'Machine',
  noMachine: 'No machine linked yet',
  software: 'Link box software',
  lastCheckin: 'Last check-in received',
  checkinsToday: 'Check-ins today',
  sentToHr: 'Sent to HR system today',
  machineClock: 'Machine clock',
  noneToday: (when: string) => `None today (last: ${when})`,
  noneYet: 'None yet',
  sentOf: (sent: string, total: string) => `${sent} of ${total}`,
  waiting: (count: string) => `${count} waiting`,
  notMatched: (count: string) => `${count} not matched to an employee`,
  todayFooter: (ago: string) => `Today = since midnight, Pakistan time · Updated ${ago}`,
  todayError: "Couldn't load today's numbers.",
  notReported: 'Not reported yet',
  // Recent
  recent: 'Recent problems and changes',
  seeAllActivity: 'See all activity',
  nothingUnusual: 'Nothing unusual in the last 14 days.',
  recentError: "Couldn't load recent activity.",
  // Technical details
  technical: 'Technical details',
  identifiers: 'Identifiers',
  machineSection: 'Machine',
  softwareSection: 'Software',
  healthSection: 'Health evidence',
  memorySection: 'Link box memory',
  recordChecks: 'Record checks',
  connectionHistory: 'Machine connection history',
  connectionSubtitle: 'Recent drops and reconnects',
  connectionEmpty: 'No connection changes recorded yet.',
  connectionError: "Couldn't load the connection history.",
  detailError: "Couldn't load this device's details.",
} as const

/** The "What's happening" title, meaning, action and clears-when of a device. */
export interface WhatsHappening {
  tone: 'working' | 'attention' | 'offline' | 'neutral'
  title: string
  meaning: string
  action: string | null
  clearsWhen: string | null
  code: string
}

export function whatsHappening(device: DeviceDetail): WhatsHappening {
  const status = deviceStatus(device)
  if (status.group === 'spare') return { tone: 'neutral', title: SUMMARY_WORDS.spareTitle, meaning: SUMMARY_WORDS.spareMeaning, action: null, clearsWhen: null, code: 'SPARE' }
  if (status.group === 'setup') return { tone: 'neutral', title: SUMMARY_WORDS.setupTitle, meaning: 'It will show as working once its machine is linked.', action: null, clearsWhen: null, code: 'ONBOARDING' }
  if (status.group === 'offline') {
    const reason = device.offline_reason && OFFLINE_REASONS[device.offline_reason] ? device.offline_reason : status.code
    const words = OFFLINE_REASONS[reason] ?? OFFLINE_REASONS.no_internet
    const action = reason === 'no_internet'
      ? 'ask someone at the place to check the link box has power and internet'
      : reason === 'machine_not_responding'
        ? 'check the machine is switched on and its network cable is plugged in'
        : reason === 'not_set_up' ? null : 'call IT'
    return { tone: 'offline', title: words.heading, meaning: words.sentence, action, clearsWhen: 'the device reports again', code: reason }
  }
  const primary = device.health?.primary as (null | { code: string; plain_title?: string | null; plain_detail?: string | null; next_step?: string | null })
  const reasons = device.health?.reasons ?? []
  const firstReason = reasons[0]
  if (status.group === 'attention') {
    const code = primary?.code ?? firstReason?.code ?? status.code
    const words = problemWords({ code, plain_title: primary?.plain_title, plain_detail: primary?.plain_detail, next_step: primary?.next_step })
    const resolvable = reasons.find((reason) => reason.code === code)?.operator?.resolvable
    return {
      tone: 'attention',
      title: status.code === 'QUARANTINED_DUPLICATE_SERIAL' ? SUMMARY_WORDS.blockedTitle : status.heading || words.title,
      meaning: status.code === 'QUARANTINED_DUPLICATE_SERIAL' ? status.sentence : words.detail,
      action: words.next,
      clearsWhen: resolvable === false ? words.closesWhen : null,
      code,
    }
  }
  return { tone: 'working', title: SUMMARY_WORDS.everythingWorking, meaning: SUMMARY_WORDS.workingMeaning, action: null, clearsWhen: null, code: status.code }
}

export const PROBLEMS_WORDS = {
  happeningNow: 'Happening now',
  notFixed: (count: number) => `Not fixed yet (${count})`,
  fixedRecently: (count: number) => `Fixed recently (${count})`,
  newPill: 'New',
  seenPill: 'Seen',
  none: 'No problems on this device.',
  spare: 'Spares are not checked for problems.',
  loadError: "Couldn't load problems.",
  markSeen: 'Mark as seen',
  marking: 'Marking…',
  markFixed: 'Mark as fixed…',
  started: (when: string) => `Started ${when}`,
  stillHappening: 'still happening',
  lastReported: (ago: string) => `last reported ${ago}`,
  seenBy: (name: string, time: string) => `Seen by ${name} at ${time}. Still open until it is fixed.`,
  seenNote: (note: string) => ` Note: ${note}`,
  fixedAt: (when: string) => `Fixed ${when}.`,
  fixedNote: (note: string) => ` Note: ${note}`,
  markedSeen: 'Marked as seen. It stays open until the problem is fixed.',
  markSeenFailed: "Couldn't mark as seen. Try again.",
  clearsBySelf: (sentence: string) => SUMMARY_WORDS.clearsWhen(sentence),
  newCount: (count: number) => `(${count} new)`,
  alsoOpen: 'Also open (does not change the status)',
} as const

export const PEOPLE_WORDS = {
  title: 'People on this machine',
  counting: 'Counting…',
  notRead: 'Not read yet',
  lastRead: (ago: string) => `List last read from the machine ${ago}`,
  stillChecking: '(still checking the list)',
  allowed: 'Changes to people: allowed',
  paused: (reason: string) => `Changes to people: paused. ${reason}`,
  pausedFallback: 'The full list has not been read yet.',
  hikvision: 'Fingerprints and faces are added on the machine itself.',
  open: 'Open people on this machine',
  manageZkt: 'Add, change or remove people, or give someone temporary admin access for 10 minutes.',
  manageHik: 'See people and their CNIC links. Fingerprints and faces are added on the machine itself.',
  adminUntil: (time: string) => `Temporary admin access is on until ${time}.`,
} as const

export const TOOLS_WORDS = {
  everyday: 'Everyday',
  advanced: 'Advanced tools · for IT',
  advancedIntro: 'These change how the device works. Use them only if you know why.',
  // Refresh people
  refreshTitle: 'Refresh people list from the machine',
  refreshText: (ago: string | null) => `Reads the full list of people from the machine again. ${ago ? `Last read ${ago}.` : 'Not read yet.'}`,
  refreshNow: 'Refresh now',
  refreshing: 'Refreshing…',
  refreshFailed: "Couldn't start the refresh. Try again.",
  refreshed: (count: string | null) => (count ? `People list refreshed: ${count} people.` : 'People list refreshed.'),
  waitingLinkBox: 'Waiting for the link box to come online',
  waitingMachine: 'Waiting for the machine',
  tryingAgain: 'Trying again',
  keepTrying: "We'll keep trying until the machine answers.",
  cancel: 'Cancel',
  cancelled: 'Cancelled before it started.',
  cancelFailed: "Couldn't cancel. It may already be running.",
  didntWork: (reason: string) => `Didn't work: ${reason.replace(/\.$/, '')}.`,
  tryAgain: 'Try again',
  checkFailed: "Can't check the progress right now. We'll keep trying.",
  // Recover attendance
  recoverTitle: 'Recover attendance',
  recoverText: 'Reads every record straight from the machine and adds any check-ins the dashboard never received.',
  ready: 'Ready to start.',
  start: 'Start',
  starting: 'Starting…',
  startedAt: (time: string) => `Started at ${time}.`,
  running: 'A recovery is already running for this machine.',
  openIt: 'Open it',
  follow: 'Follow it in Recover attendance',
  waitFor: (reason: string) => `It will be able to start when ${reason.replace(/\.$/, '').replace(/^./, (letter) => letter.toLowerCase())}.`,
  hikChoices: 'Other choices (only active people, only the newest records) are in Recover attendance.',
  startFailed: "Couldn't start. Try again.",
  checking: 'Checking…',
  preflightError: "Couldn't check whether a recovery can start.",
  cantStart: "It can't start right now.",
  // Restart
  restartTitle: 'Restart the machine',
  restartText: 'The machine is unavailable for about a minute while it restarts.',
  restartNotCapable: "This machine model can't be restarted from here.",
  restartLease: (time: string | null) => `Can't restart while someone has temporary admin access${time ? ` (ends ${time})` : ''}.`,
  restartOpen: 'Restart machine…',
  restartConfirm: (name: string) => `Restart the machine at ${name}?`,
  restartButton: 'Restart machine',
  sending: 'Sending…',
  reason: 'Reason',
  reasonDefault: 'Restarted from the dashboard',
  password: 'Your password',
  enterPassword: 'Enter your password.',
  wrongPassword: 'That password is not right. Type it again.',
  restartFailed: "Couldn't send the restart. Try again.",
  restarted: (time: string) => `Machine restarted at ${time}.`,
  reasonShort: 'Write a reason of at least 3 letters.',
} as const

export const ADVANCED_WORDS = {
  // Confirm a new machine
  replaceTitle: 'A different machine is connected',
  replaceText: (oldSerial: string, newSerial: string) => ({ before: 'This device was set up for machine ', old: oldSerial, middle: ' but is now connected to machine ', fresh: newSerial, after: '. Changes to people and check-ins are paused until IT confirms the new machine.' }),
  why: 'Why? (saved with your name)',
  replaceDefault: 'Machine replaced on site',
  typeToConfirm: 'Type',
  toConfirm: 'to confirm',
  replaceButton: 'Confirm new machine',
  confirming: 'Confirming…',
  replaced: 'New machine confirmed. Waiting for the link box to accept it.',
  replaceFailed: "Couldn't confirm the new machine.",
  // Spare
  spareTitleInUse: 'Use as a spare',
  spareTitleSpare: 'Spare (not in use)',
  inUse: 'This device is counted in totals and problems.',
  spareText: 'This spare is still watched, but it is not counted in totals or problems.',
  useAsSpare: 'Use as a spare',
  putBack: 'Put back in use',
  saving: 'Saving…',
  movedToSpare: (name: string) => `${name} moved to spare devices.`,
  backInUse: (name: string) => `${name} is back in use.`,
  undo: 'Undo',
  spareFailed: "Couldn't change it. Try again.",
  // Machine password (COMM Key)
  passwordTitle: 'Machine password (COMM Key)',
  passwordInUse: (ago: string | null) => (ago ? `Password in use, confirmed ${ago}` : 'Password in use'),
  notManaged: 'Not managed by the dashboard yet',
  needsSoftware: 'Change waiting for newer link box software',
  switchedOff: 'Changing the machine password is switched off for now. Ask IT to switch it on.',
  applyAfterUpdate: 'This will apply after the link box software is updated.',
  lastFailed: (sentence: string) => `Last try failed: ${sentence}`,
  inProgress: (mode: string, ends: string) => `Change in progress · ${mode} · ends ${ends}`,
  cancelChange: 'Cancel change',
  changeOn: 'Change it on',
  cantOnMachine: (reason: string) => `Changing it on the machine isn't possible for this machine: ${reason}`,
  newPassword: 'New password (numbers only)',
  machineSerial: 'Machine serial number',
  serialNote: 'The change only happens if the link box finds the machine with this serial number.',
  passwordReasonDefault: 'Machine password recovery',
  changeButton: 'Change password',
  numbersOnly: 'Use numbers only, not starting with 0.',
  sentToLinkBox: 'Password change sent to the link box.',
  afterUpdate: 'The password change will apply after the software update.',
  changeFailed: "Couldn't send the change.",
  changeCancelled: 'Change cancelled. Nothing was changed.',
  cancelFailed: "Couldn't cancel.",
  // Show the machine password
  revealTitle: 'Show the machine password',
  revealText: 'Needs your password, and your name is recorded. It hides after 15 seconds, when you leave this window or close this device.',
  revealWhy: 'Why do you need it?',
  revealDefault: 'Machine password check',
  revealButton: 'Show for 15 seconds',
  revealed: 'Machine password:',
  hidesIn: (seconds: number) => `Hides in ${seconds} s`,
  revealFailed: "Couldn't show the password.",
  // Clear an out-of-date error
  clearTitle: 'Clear an out-of-date error',
  clearText: (title: string) => `This device still has an old saved error (${title}) with no open problem behind it. Clearing it sets the device to its current status.`,
  clearButton: 'Clear an out-of-date error',
  // Clear old problems
  cleanupTitle: 'Clear old problems on this device',
  cleanupText: 'Closes problems that are left over from older link box software. You see the list before anything changes.',
  cleanupButton: 'Choose problems to close',
  // Custody card
  custodyTitle: 'Check-ins received by the dashboard (engineer view)',
  custodyShow: 'Show the engineer view',
} as const

/** Plain words for the restart refusals (409) the backend sends today. */
export function restartError(status: number | null, message: string | null | undefined): string {
  if (status === 403) return TOOLS_WORDS.wrongPassword
  const text = (message ?? '').toLowerCase()
  if (text.includes('lease')) return TOOLS_WORDS.restartLease(null)
  if (text.includes('certified') || text.includes('capab')) return TOOLS_WORDS.restartNotCapable
  if (status === 422) return TOOLS_WORDS.reasonShort
  return TOOLS_WORDS.restartFailed
}

/** Why Recover attendance can't start for this machine (preflight hard blockers). */
export const RECOVER_BLOCKERS: Record<string, string> = {
  FEATURE_DISABLED: 'Recovering attendance from the dashboard is switched off for now.',
  CONNECTOR_INACTIVE: 'This device is switched off in the dashboard.',
  DUPLICATE_SERIAL_QUARANTINE: 'Another link box says it is linked to this machine. That must be sorted out first.',
  NO_ZKT: 'No machine is linked to this device yet.',
  FIRMWARE_UNSUPPORTED: 'The link box needs newer software first.',
  HISTORY_STREAM_UNCERTIFIED: "This link box software can't read the full history yet.",
  RANGE_RESUME_UNCERTIFIED: "This machine model isn't approved for a full read yet.",
  SERIAL_UNKNOWN: "The machine's serial number isn't known yet.",
  SOURCE_GENERATION_STALE: "The machine's records changed since the last check. Ask IT.",
}

/** "It will be able to start when {…}." (preflight waitable blockers). */
export const RECOVER_WAITS: Record<string, string> = {
  IDENTITY_SNAPSHOT_UNSTABLE: 'the list of people has been read in full',
  WAITING_FOR_ZKT: 'the machine is steadily online',
  WAITING_FOR_DEVICE: 'the link box is back online',
  COMMAND_ACTIVE: 'the request already running has finished',
  LEASE_ACTIVE: 'temporary admin access has ended',
}

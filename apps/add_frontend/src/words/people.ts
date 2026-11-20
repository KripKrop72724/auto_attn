// People words (07-people PPL-1, code-keyed part): why changes are off,
// problem chips, backend messages that reach People word for word, and
// initials. Request type and status words come from words/requests.ts.
// "Employee ID" is the machine's number (D-24); HR's number is
// "Employee ID in HR's records".

import { plural } from './glossary'
import type { Tone } from './types'

export const EMPLOYEE_ID = 'Employee ID'
export const HR_EMPLOYEE_ID = "Employee ID in HR's records"
export const HR_EMPLOYEE_ID_HELP = 'It may differ from the Employee ID on the machine.'

// Why changes are off (writes_disabled_reason, UsersWorkspace actionReason).
export const WRITES_OFF: Record<string, string> = {
  TERMINAL_SERIAL_CONFIRMATION_REQUIRED: 'Confirm this machine first. It is a one-time check.',
  TERMINAL_SERIAL_PENDING_DEVICE_ACK: 'Waiting for the link box to save your confirmation.',
  TERMINAL_SERIAL_PIN_FAILED: "The last confirmation didn't go through. Check the serial number and try again.",
  STABILITY_CERTIFICATION_PENDING: 'Machine confirmed. A short safety check is still running.',
  HIKVISION_WRITE_QUALIFICATION_PENDING: "Changing people from the dashboard isn't ready for this machine model yet. You can still see everyone. Confirming the machine again won't change this.",
  FULL_USER_SNAPSHOT_REQUIRED: 'The full list of people has to be read from the machine first.',
  USER_SNAPSHOT_TRUNCATED: 'The last read of the list was incomplete. Refresh the people list from the machine, then try again.',
  LEGACY_28_BYTE_RECORD: "This older machine model can't be changed from the dashboard.",
  SERIAL_MISMATCH: "This machine's serial number doesn't match the one confirmed before. Was the machine replaced?",
  DUPLICATE_SERIAL: 'Another device reports the same machine. Ask IT.',
  RECOVERY_SERIAL_PENDING_PROOF: 'This machine was reset or replaced. IT has to confirm it first.',
  DEVICE_NOT_WRITE_CERTIFIED: "This machine isn't approved for changes from the dashboard.",
}
export const WRITES_OFF_FALLBACK = 'Changes are off for this machine right now. Ask IT.'

/** Plain reason why adding or changing people is off on this machine. */
export function writesOff(code: string | null | undefined): { sentence: string; unknown: boolean } {
  const sentence = WRITES_OFF[(code ?? '').toUpperCase()]
  return sentence ? { sentence, unknown: false } : { sentence: WRITES_OFF_FALLBACK, unknown: true }
}

export const CAPABILITY_OFF = {
  create: "This machine doesn't allow adding people from the dashboard.",
  update: "This machine doesn't allow changing people from the dashboard.",
  delete: "This machine doesn't allow removing people from the dashboard.",
} as const

export interface PersonFacts {
  cnic_available?: boolean | null
  cnic_format_suspect?: boolean | null
  identity_conflict_code?: string | null
  identity_conflict_resolved?: boolean | null
  identity_conflict_members?: Array<string | number | { user_id: string }> | null
  current_command_state?: string | null
  privilege?: number | null
  looks_damaged?: boolean | null
}

export interface PersonProblem {
  key: 'no_cnic' | 'cnic_typed' | 'same_cnic' | 'same_person' | 'sending' | 'admin' | 'damaged'
  label: string
  tone: Tone | 'info'
}

const idList = (members: Array<string | number | { user_id: string }> | null | undefined) => {
  const list = (members ?? []).map((member) => (typeof member === 'object' ? member.user_id : String(member)))
  if (list.length <= 2) return list.join(' and ')
  return `${list[0]} and ${plural(list.length - 1, 'more', 'more')}`
}

/** One plain chip per fact (07-people P4). */
export function personProblems(user: PersonFacts): PersonProblem[] {
  const chips: PersonProblem[] = []
  if (user.looks_damaged) chips.push({ key: 'damaged', label: 'Damaged entry', tone: 'problem' })
  if (user.cnic_available === false && !user.cnic_format_suspect) chips.push({ key: 'no_cnic', label: 'No CNIC', tone: 'warning' })
  if (user.cnic_format_suspect) chips.push({ key: 'cnic_typed', label: 'CNIC typed wrongly', tone: 'warning' })
  if (user.identity_conflict_code && !user.identity_conflict_resolved) {
    const others = idList(user.identity_conflict_members)
    chips.push({ key: 'same_cnic', label: others ? `Same CNIC as ${others}` : 'Same CNIC as someone else', tone: 'problem' })
  }
  if (user.identity_conflict_resolved) {
    const others = idList(user.identity_conflict_members)
    chips.push({ key: 'same_person', label: others ? `Same person as ${others} (confirmed)` : 'Same person (confirmed)', tone: 'neutral' })
  }
  if (user.current_command_state) chips.push({ key: 'sending', label: 'Sending to machine…', tone: 'info' })
  if (user.privilege === 14) chips.push({ key: 'admin', label: 'Machine admin', tone: 'neutral' })
  return chips
}

// Backend messages that reach People word for word (matched by exact text or prefix).
export const PEOPLE_ERRORS: Array<{ match: string; prefix?: boolean; sentence: string }> = [
  { match: 'No assigned ZKT device.', sentence: 'No attendance machine is linked to this device yet.' },
  { match: 'Connector not found.', sentence: 'This device no longer exists. Go back to People.' },
  { match: 'Password confirmation failed.', sentence: 'Wrong password. Try again.' },
  { match: 'This connector is quarantined because its ZKT serial is duplicated.', sentence: 'This device is blocked because another device reports the same machine. Ask IT.' },
  { match: 'A complete fresh user snapshot is required before user mutation.', sentence: 'Refresh the people list from the machine first, then try again.' },
  { match: 'An enrollment administrator lease is active for this user/device.', sentence: 'Someone has temporary admin access on this machine right now. Wait for it to end.' },
  { match: 'Hikvision employee numbers require 1', prefix: true, sentence: 'Employee ID must be 1 to 32 digits.' },
  { match: 'ZKT employee/user IDs cannot exceed 24 characters.', sentence: 'Employee ID can be at most 24 characters.' },
  { match: 'That employee/user ID has already been used on this terminal.', sentence: 'This Employee ID is already used on this machine.' },
  { match: 'The qualified Hikvision profile capacity has been reached.', sentence: 'This machine is full. Remove someone first.' },
  { match: 'The terminal user capacity has been reached.', sentence: 'This machine is full. Remove someone first.' },
  { match: 'The terminal has no never-used 16-bit UID remaining.', sentence: 'This machine is full. Remove someone first.' },
  { match: 'That idempotency key', prefix: true, sentence: 'This request was already sent. Refresh the page.' },
  { match: 'The idempotent create command no longer references a user.', sentence: 'This request was already sent. Refresh the page.' },
  { match: 'The terminal currently encodes this exact CNIC', prefix: true, sentence: 'This CNIC is already used by another Employee ID on this machine.' },
  { match: 'This exact CNIC is reserved by pending terminal user', prefix: true, sentence: 'This CNIC is being added for another Employee ID right now. Wait a moment.' },
  { match: 'Device user is not active.', sentence: 'This person is no longer on the machine. Refresh.' },
  { match: 'Device user not found.', sentence: 'This person is no longer on the machine. Refresh.' },
  { match: 'Target terminal user not found.', sentence: 'This person is no longer on the machine. Refresh.' },
  { match: 'User changed since it was loaded. Refresh and retry.', sentence: 'Someone else changed this person. Refresh and try again.' },
  { match: 'Target user changed since it was selected. Refresh and retry.', sentence: 'Someone else changed this person. Refresh and try again.' },
  { match: 'CNIC cannot be cleared.', sentence: 'You can change the CNIC but not remove it.' },
  { match: 'Hikvision role changes are not qualified on this connector.', sentence: "The admin role can't be changed on this machine." },
  { match: 'Typed confirmation must exactly match', prefix: true, sentence: "The confirmation doesn't match. Type it exactly as shown." },
  { match: 'A reason is required for permanent administrator elevation.', sentence: 'Write a short reason.' },
  { match: 'Demote this permanent administrator before deletion.', sentence: "Machine admins can't be removed. Change their role to Regular first." },
  { match: 'This device already has an active bulk user deletion job.', sentence: 'A removal is already running on this machine.' },
  { match: 'This device already has an active mutating command.', sentence: 'Another change is still being sent to this machine. Wait for it to finish.' },
  { match: 'Lease not found.', sentence: 'This temporary admin access has already ended.' },
  { match: 'Lease target is unavailable.', sentence: 'This temporary admin access has already ended.' },
  { match: 'User deletion job not found.', sentence: "That removal can't be found. Refresh." },
  { match: 'The authenticated connector has not reported a terminal serial.', sentence: "The link box hasn't reported a machine yet. Check the cable." },
  { match: 'CNIC must contain exactly 13 digits.', sentence: 'CNIC must be 13 digits.' },
]

const READ_ONLY = /^This terminal is read-only \(([A-Z0-9_]+)\)\.$/
const USER_CHANGED = /^User \S+ changed since it was selected\.$/
const USER_ACTIVE_COMMAND = /^User \S+ already has an active command\.$/
const USER_PERMANENT_ADMIN = /^User \S+ is a permanent administrator and cannot be deleted\.$/
const USER_GONE = /^(Selected terminal users no longer exist:|User \S+ is no longer active\.)/

/** Plain sentence for a backend People message, or null when it has no words (use plainApiError's fallback). */
export function peopleError(message: string | null | undefined): string | null {
  const text = (message ?? '').trim()
  if (!text) return null
  const readOnly = READ_ONLY.exec(text)
  if (readOnly) return writesOff(readOnly[1]).sentence
  if (USER_CHANGED.test(text)) return 'Someone else changed this person. Refresh and try again.'
  if (USER_ACTIVE_COMMAND.test(text)) return 'Another change is still being sent to this machine. Wait for it to finish.'
  if (USER_PERMANENT_ADMIN.test(text)) return "Machine admins can't be removed. Change their role to Regular first."
  if (USER_GONE.test(text)) return 'Some selected people are no longer on the machine. Refresh.'
  const found = PEOPLE_ERRORS.find((entry) => (entry.prefix ? text.startsWith(entry.match) : text === entry.match))
  return found ? found.sentence : null
}

const SKIP_TITLES = new Set(['dr', 'dr.', 'mr', 'mr.', 'mrs', 'mrs.', 'ms', 'ms.', 'syed', 'muhammad', 'mohammad'])

/** First letters of the first two words, skipping titles (Dr, Mr, Syed, Muhammad …). */
export function personInitials(name: string | null | undefined): string {
  const words = (name ?? '').trim().split(/\s+/).filter(Boolean)
  const kept = words.filter((word) => !SKIP_TITLES.has(word.toLowerCase()))
  const source = kept.length ? kept : words
  return source.slice(0, 2).map((word) => word[0]?.toUpperCase() ?? '').join('') || '?'
}

// ---------------------------------------------------------------------------
// Page words (07-people Words tables). Everything the People pages show lives
// here so the plain-words guard and the dictionaries test can see it.

/** "Same CNIC as 1008" / "1008 and 1009" / "1008 and 2 more". */
export const employeeIdList = (members: Array<string | number | { user_id: string }> | null | undefined) => idList(members)

/** People list: the "Damaged entry" fact (looks_damaged, 3 Nov fingerprint loss). */
export const DAMAGED_ENTRY = {
  chip: 'Damaged entry',
  reason: 'This entry looks damaged. Removing it from the dashboard can erase every fingerprint on this machine. Ask IT to remove it on the machine itself.',
} as const

export const PICKER = {
  title: 'People',
  description: 'People saved on each attendance machine. Every change is recorded.',
  searchLabel: 'Search devices',
  searchPlaceholder: 'Search place or city',
  listLabel: 'Devices',
  chipsLabel: 'Show',
  hasFixes: 'Has people to fix',
  adminOn: 'Admin access on',
  group: (label: string, count: number | null) => `${label} · ${count == null ? '–' : formatCountText(count)}`,
  spareGroup: 'Spare (not in use)',
  showSpares: (count: number) => `Show spare devices (${formatCountText(count)})`,
  hideSpares: 'Hide spare devices',
  loading: 'Loading devices…',
  loadFailed: "Couldn't load the device list.",
  loadFailedHelp: 'Check your connection, then try again.',
  noMatch: (query: string) => `No device matches “${query}”.`,
  noMatchHelp: 'Try a place or city name.',
  clearSearch: 'Clear search',
  noDivision: (label: string) => `No ${label} devices.`,
  noDivisionHelp: 'Switch to All to see every device.',
  showAll: 'Show all',
  noDevices: 'No devices yet.',
  noneForChip: 'No devices to show for this filter.',
  people: (count: number | null | undefined) => count == null ? 'People not read yet' : plural(count, 'person'),
  needFixing: (count: number) => `${formatCountText(count)} need fixing`,
  adminAccessOn: 'Temporary admin access is on',
  offlineLine: 'Changes will wait until it is back online.',
  noMachine: 'No machine linked yet',
  listRead: (ago: string) => `List read from the machine ${ago}`,
  open: (name: string) => `Open ${name}`,
  columns: { device: 'Device', status: 'Status', people: 'People', fixing: 'Need fixing', listRead: 'List read from machine', open: 'Open' },
} as const

const formatCountText = (value: number) => new Intl.NumberFormat('en-PK').format(value)

export const HEADER = {
  deviceLabel: 'Device',
  people: (count: number | null | undefined) => count == null ? 'People not read yet' : plural(count, 'person'),
  listRead: (ago: string) => `List read from the machine ${ago}`,
  listReading: 'The list of people is still being read from the machine.',
  lastHeard: (ago: string) => `Last heard from ${ago}`,
  addPerson: 'Add person',
  changeDevice: 'Change device',
  refresh: 'Refresh people list from the machine',
  refreshBusy: 'Asking the machine…',
  refreshOffline: 'The machine must be online for this.',
  refreshQueued: 'Asked the machine for its list of people. This can take a minute.',
  refreshFailed: "Couldn't ask the machine. Is it online?",
  openDevice: 'Open device details',
  machineConfirmed: (serial: string) => `Machine confirmed: serial ${serial}`,
  statusFailed: "Couldn't get this machine's latest status. Showing what we had.",
  otherDivision: (label: string) => `This device is ${label}`,
  moreFor: 'More for this device',
  notFound: 'This device no longer exists.',
  notFoundHelp: 'Choose another device.',
} as const

export const NOTICE = {
  viewOnlyChip: 'View only',
  viewOnly: "You can see the people on this machine, but you can't change them right now.",
  exceptionLabel: 'Allowed by IT for this machine',
  exceptionTitle: 'Changes allowed by IT for this machine',
  exceptionForever: 'No end date. Only for this machine.',
  exceptionUntil: (when: string) => `Allowed until ${when}.`,
  exceptionPendingSoftware: ' A link box software update will remove this limit.',
  exceptionNoRole: " The admin role can't be changed here.",
  capabilityTitle: "Some changes aren't available on this machine model.",
  capabilityAdd: 'Adding people',
  capabilityRemove: 'Removing people',
  capabilityChange: 'Changing people',
} as const

export const MACHINE_CHECK = {
  eyebrow: 'One-time check',
  title: 'Is this the right machine?',
  body: (serial: string, place: string) => `This machine reports serial number ${serial}. Check the sticker on the machine at ${place}. If it matches, confirm it so people can be added and changed.`,
  ready: 'Link box online — ready',
  offline: "Link box offline — your confirmation will be sent when it's back",
  chip: 'Waiting for you',
  cardButton: 'Yes, this is the right machine',
  blocked: "You can't add or change people until this is done.",
  pendingLabel: 'Machine check in progress',
  pendingEyebrow: 'Almost done',
  pendingTitle: 'Waiting for the link box to save it',
  pendingBody: (serial: string) => `Your confirmation was accepted. Adding and changing people opens after the link box saves serial number ${serial} and a short check passes.`,
  pendingSteps: { waiting: 'Confirmed', sent: 'Link box saved it', confirmed: 'Short check passed' },
  pendingUsual: 'Usually a few minutes.',
  pendingOffline: 'Keep the link box switched on and connected.',
  pendingOfflineChip: 'Link box offline',
  slow: 'This is taking longer than usual.',
  tryAgain: 'Try again',
  mismatch: "This machine's serial number doesn't match the one confirmed before. Was the machine replaced?",
  openDevice: 'Open device details',
  dialogTitle: 'Confirm this machine',
  dialogTitleAgain: 'Confirm this machine again',
  dialogDescription: 'Check the serial number, then enter your password.',
  dialogMachine: 'Machine',
  onlineChip: 'Link box online',
  offlineChip: 'Link box offline — will send later',
  serialLabel: 'Serial number shown by the machine',
  dialogBody: (place: string) => `Only confirm if this matches the sticker on the machine at ${place}. The link box then saves it and runs a short check; after that you can add and change people.`,
  dialogOffline: 'The link box is offline. Keep it switched on and connected. Your confirmation waits up to 10 minutes.',
  password: 'Your password',
  confirm: 'Confirm machine',
  confirming: 'Confirming…',
  sent: 'Confirmed. The link box is saving it now.',
  queued: 'Saved. It will be sent when the link box is back online.',
  failed: "Couldn't confirm the machine. Try again.",
  cancel: 'Cancel',
} as const

export const LIST = {
  heading: 'People on this machine',
  sectionsLabel: 'Sections',
  tabs: { people: 'People', shared: 'Same CNIC', old: 'Old check-ins' },
  summaryFixing: (count: number) => `${formatCountText(count)} need fixing`,
  summaryCnic: (withCnic: number, total: number) => withCnic >= total ? 'Everyone has a CNIC' : `${formatCountText(withCnic)} of ${formatCountText(total)} have a CNIC`,
  summaryOld: (count: number) => `${plural(count, 'old check-in')} waiting`,
  searchLabel: 'Search name, Employee ID or CNIC',
  cnicHint: 'Type all 13 digits to search by CNIC',
  chipsLabel: 'Show',
  everyone: 'Everyone',
  needsFixing: 'Needs fixing',
  admins: 'Admins',
  addedThisWeek: 'Added this week',
  addedHint: 'New on this machine in the last 7 days',
  problem: 'Problem',
  problems: { missing: 'No CNIC', shared: 'Same CNIC', typed: 'CNIC typed wrongly', confirmed: 'Confirmed same person' },
  role: 'Role',
  roles: { all: 'Everyone', regular: 'Regular', admin: 'Machine admins' },
  shift: 'Works in shifts',
  changing: 'Change being sent',
  sortLabel: 'Sort',
  sorts: { problems: 'Needs fixing first', name: 'Name A–Z', id: 'Employee ID', newest: 'Newest first' },
  cnicEnding: (last4: string) => `CNIC ending ${last4}`,
  quoted: (text: string) => `“${text}”`,
  regular: 'Regular',
  machineAdmin: 'Machine admin',
  columns: { person: 'Person', id: EMPLOYEE_ID, cnic: 'CNIC', role: 'Role', needs: 'Needs', actions: 'Actions' },
  showing: (shown: number, total: number) => `Showing ${formatCountText(shown)} of ${formatCountText(total)}`,
  ticksKept: 'Your ticks are kept when you search.',
  showMore: 'Show more people',
  loadingMore: 'Loading…',
  loading: 'Loading people…',
  found: (count: number) => `${formatCountText(count)} found`,
  emptyTitle: 'No people on this machine yet.',
  emptyBody: 'People added on the machine show up here after the next list read.',
  noMatchTitle: 'No one matches.',
  noMatchBody: 'Try another name or Employee ID, or clear the filters.',
  clearFilters: 'Clear filters',
  loadFailed: "Couldn't load the people on this machine.",
  refreshFailed: (time: string) => `Couldn't refresh the list. Showing what we had at ${time}.`,
  notOnMachine: (id: string) => `${EMPLOYEE_ID} ${id} isn't on this machine.`,
  rowLabel: (name: string, id: string, fixing: boolean) => `${name}, ${EMPLOYEE_ID} ${id}${fixing ? ', needs fixing' : ''}`,
  employeeId: (id: string) => `${EMPLOYEE_ID} ${id}`,
  worksInShifts: 'Works in shifts',
  select: 'Select',
  done: 'Done',
  selectPerson: (name: string) => `Select ${name}`,
  selectAll: 'Select all on this page',
  selectedOfPage: (selected: number, eligible: number) => `${formatCountText(selected)} of ${formatCountText(eligible)} on this page selected`,
  selectedTotal: (page: number, total: number) => `${formatCountText(page)} on this page · ${formatCountText(total)} selected in total`,
  cantSelect: "Machine admins and people with a change being sent can't be selected.",
  edit: 'Edit',
  editPerson: (name: string) => `Edit ${name}`,
  moreFor: (name: string) => `More for ${name}`,
  optionsFor: (name: string) => `Options for ${name}`,
  adminAccess: 'Temporary admin access',
  adminAccessHelp: 'Lets them add fingerprints or a face on the machine for 10 minutes',
  remove: 'Remove from machine',
  removeHelp: 'Their past check-ins stay saved',
  removePerson: (name: string) => `Remove ${name}`,
  busy: 'A change for this person is still being sent.',
  readOnly: 'View only on this machine.',
  leaseTaken: 'Someone already has temporary admin access on this machine.',
  alreadyAdmin: 'Already a machine admin.',
  adminCantRemove: "Machine admins can't be removed. Change their role to Regular first.",
  removalRunning: 'A removal is already running on this machine.',
  linkedMissing: "That person isn't in the list right now. Refresh and try again.",
} as const

/** Step words for a change shown on the person's row. */
export const ROW_STEP = {
  waiting: 'Waiting',
  sent: 'Sent to machine',
  confirmed: 'Confirmed',
  failed: 'Failed — tap for details',
  sending: 'Sending to machine…',
} as const

export const SHEET = {
  regular: 'Regular',
  machineAdmin: 'Machine admin',
  worksInShifts: (yes: boolean) => `Works in shifts: ${yes ? 'Yes' : 'No'}`,
  nameOnMachine: 'Name saved on the machine:',
  noCnic: 'No CNIC saved',
  copy: 'Copy',
  copied: 'Copied',
  edit: 'Edit',
  addCnic: 'Add CNIC',
  fixCnic: 'Fix CNIC',
  fixThisCnic: 'Fix this CNIC',
  openSameCnic: 'Open Same CNIC',
  openOldCheckins: 'Open Old check-ins',
  adminAccess: 'Temporary admin access',
  remove: 'Remove from machine',
  changeTitle: 'Change in progress',
} as const

/** Fix steps per problem (PROBLEM_HELP). */
export const PROBLEM_HELP = {
  no_cnic: {
    title: 'No CNIC',
    body: "This person has no CNIC on the machine, so their check-ins are not matched to an employee and don't reach the HR system (Oracle).",
    steps: ['Get their CNIC from HR.', 'Tap Add CNIC and type it.', 'Their waiting check-ins are then sent by the automatic sending in Attendance › Fix tools.'],
  },
  cnic_typed: {
    title: 'CNIC typed wrongly',
    body: "The CNIC in this person's name on the machine is typed in a way the dashboard can't read, so their check-ins are set aside.",
    steps: ['Check the CNIC with HR.', 'Tap Fix CNIC and type it. The name is shortened so the full CNIC fits.'],
  },
  same_cnic: {
    title: (ids: string) => ids ? `Same CNIC as ${EMPLOYEE_ID} ${ids}` : 'Same CNIC as someone else',
    body: "Their check-ins are set aside, so they don't reach the HR system (Oracle).",
    steps: ['Ask HR whose CNIC this is.', 'If it is one person on the machine twice, confirm it on the Same CNIC tab.', "If not, fix the wrong person's CNIC."],
  },
  damaged: {
    title: 'Damaged entry',
    body: DAMAGED_ENTRY.reason,
    steps: [] as string[],
  },
  old: (count: number) => `${plural(count, 'older check-in')} from this ${EMPLOYEE_ID} are waiting for details.`,
} as const

/** The add / edit / remove-one / give access dialog. */
export const PERSON_DIALOG = {
  addTitle: (device: string) => `Add a person to ${device}`,
  addDescription: 'They are added as a regular person on this machine.',
  editTitle: (name: string) => `Edit ${name}`,
  editDescription: 'The dashboard checks the machine after saving.',
  removeTitle: (name: string, device: string) => `Remove ${name} from ${device}?`,
  removeDescription: 'Their past check-ins stay saved.',
  leaseTitle: 'Give temporary admin access',
  leaseDescription: (name: string, device: string) => `${name} can add fingerprints or a face on ${device} for 10 minutes. It ends by itself, even if the internet drops.`,
  hikFullDelete: 'This also deletes their face, fingerprints, card and PIN from the machine, so they can\'t check in here any more. Their past check-ins stay saved.',
  hikOlder: 'This link box has older software: their fingerprints or card may stay on the machine. Update the link box software to remove everything.',
  suspectNote: (name: string, kept: string) => `The machine has this person's CNIC typed wrongly in their name (“${name}”), so their check-ins are not matched. Check the CNIC with HR and type it below. The name is shortened to “${kept}” so the full CNIC fits.`,
  problemNoCnic: 'No CNIC saved yet. Type it below.',
  problemSameCnic: (ids: string) => `Same CNIC as ${EMPLOYEE_ID} ${ids}. Type the correct CNIC.`,
  fullName: 'Full name',
  cnic: 'CNIC',
  cnicConflict: 'Correct CNIC (needed — same CNIC as someone else)',
  cnicMissing: 'CNIC (needed — none saved yet)',
  cnicOptional: 'New CNIC (leave empty to keep the current one)',
  cnicPlaceholder: '13 digits',
  cnicCount: (count: number) => `${count} of 13 digits`,
  moreOptions: 'More options',
  employeeIdOptional: `${EMPLOYEE_ID} (optional)`,
  employeeIdHint: 'Leave empty to use the next free number.',
  role: 'Role on the machine',
  roleRegular: 'Regular',
  roleAdmin: 'Machine admin (always)',
  roleLocked: "The admin role can't be changed on this machine.",
  roleHik: "Makes them an admin on this machine. Temporary admin access isn't available on this model.",
  roleHint: 'To add fingerprints, use temporary admin access instead.',
  shift: 'Works in shifts',
  shiftHint: 'Adds “-S” to the name on the machine.',
  machineName: 'Name on the machine',
  fits: 'Fits',
  shortened: 'Shortened to fit',
  needCnic: 'Type the CNIC to continue.',
  cnicHidden: 'The saved CNIC stays hidden for privacy.',
  needCnicPreview: 'Type the CNIC to see the name on the machine.',
  removeKept: 'Their past check-ins stay saved.',
  leaseEnds: 'Ends by itself after 10 minutes',
  leaseOnly: (device: string) => `Only on ${device}. It ends on time even if the internet drops.`,
  elevateTitle: 'Make a machine admin (always)',
  elevateBody: "This doesn't end by itself. Use it only if temporary admin access isn't enough.",
  why: 'Why?',
  typeExactly: 'To confirm, type exactly:',
  typeIdOrName: (id: string) => `To confirm, type their ${EMPLOYEE_ID} (${id}) or full name`,
  password: 'Your password',
  cancel: 'Cancel',
  sending: 'Sending to the machine…',
  removing: 'Removing…',
  turningOn: 'Turning on…',
  add: 'Add person',
  save: 'Save changes',
  removeButton: 'Remove from machine',
  giveAccess: 'Give access',
  errName: 'Type the full name.',
  errCnicConflict: 'Type the correct CNIC.',
  errCnicMissing: "Type this person's CNIC.",
  errCnicLength: (count: number) => `CNIC must be 13 digits (${count} typed).`,
  errEmployeeId: `${EMPLOYEE_ID} can only have digits.`,
  errPassword: 'Type your password.',
  errReason: 'Write a short reason (at least 10 letters).',
  errPhrase: "The confirmation doesn't match. Type it exactly as shown.",
  errIdOrName: `Type their ${EMPLOYEE_ID} or full name exactly.`,
  nothingChanged: 'Nothing changed yet.',
  failed: "Couldn't send this change. Nothing was changed.",
  toastAdd: (name: string) => `Adding ${name}. You can follow it in Changes.`,
  toastEdit: (name: string) => `Saving changes to ${name}. You can follow it in Changes.`,
  toastRemove: (name: string) => `Removing ${name}. Their past check-ins stay saved.`,
  toastLease: (name: string) => `Turning on temporary admin access for ${name}.`,
} as const

/** Change labels per command type (Changes tray). */
export function changeLabel(type: string | null | undefined, name?: string | null): string {
  const who = name || 'this person'
  switch ((type ?? '').toUpperCase()) {
    case 'CREATE_USER': return `Adding ${who}`
    case 'UPDATE_USER': return `Saving changes to ${who}`
    case 'DELETE_USER': return `Removing ${who}`
    case 'GRANT_TEMP_ADMIN': return `Turning on temporary admin access for ${who}`
    case 'REVOKE_TEMP_ADMIN': return 'Ending temporary admin access'
    case 'REFRESH_USERS': return 'Refreshing the people list from the machine'
    case 'PIN_TERMINAL_SERIAL': return 'Confirming the machine'
    default: return 'Changing the machine'
  }
}

/** Past tense for the "Confirmed: …" toast. */
export function changeDone(type: string | null | undefined, name?: string | null): string {
  const who = name || 'The person'
  switch ((type ?? '').toUpperCase()) {
    case 'CREATE_USER': return `${who} added`
    case 'UPDATE_USER': return `changes to ${name || 'the person'} saved`
    case 'DELETE_USER': return `${who} removed`
    case 'GRANT_TEMP_ADMIN': return `temporary admin access turned on for ${name || 'the person'}`
    case 'REVOKE_TEMP_ADMIN': return 'temporary admin access ended'
    case 'REFRESH_USERS': return 'people list refreshed'
    case 'PIN_TERMINAL_SERIAL': return 'machine confirmed'
    default: return 'change made'
  }
}

/** The line under the steps, per backend command status (P9 table). */
export function changeLine(status: string | null | undefined, reason: string, at: string): string {
  switch ((status ?? '').toUpperCase()) {
    case 'QUEUED': return 'Saved. Waiting to send.'
    case 'WAITING_FOR_DEVICE': return 'Waiting for the link box to come online.'
    case 'WAITING_FOR_ZKT': return 'Waiting for the machine to answer.'
    case 'RETRYING': return 'Trying again.'
    case 'DISPATCHED':
    case 'ACKNOWLEDGED':
    case 'RUNNING': return 'The machine is making the change.'
    case 'CANCEL_REQUESTED': return 'Cancelling…'
    case 'SUCCEEDED': return at ? `Confirmed on the machine at ${at}.` : 'Confirmed on the machine.'
    case 'FAILED': return `Failed: ${reason.replace(/\.$/, '')}.`
    case 'EXPIRED': return "Failed: the machine didn't answer in time. Nothing was changed."
    case 'CANCELLED':
    case 'CANCELED': return 'Cancelled. Nothing was changed.'
    default: return 'Saved. Waiting to send.'
  }
}

export const CHANGES = {
  title: 'Changes on this machine',
  summary: (count: number, sent: number) => `${plural(count, 'change')}${sent ? ` · ${formatCountText(sent)} sent to machine` : ''}`,
  cancel: 'Cancel',
  cancelFailed: "Couldn't cancel. It may already be on its way.",
  tryAgain: 'Try again',
  close: 'Close',
  lostTouch: 'Lost touch with this change. It may still finish; this page will catch up.',
  confirmedToast: (label: string, place: string) => `${place ? `${place}: ` : ''}Confirmed: ${label}.`,
  failedToast: (label: string, reason: string) => `Failed: ${label}. ${reason}`,
  show: 'Show changes',
  hide: 'Hide changes',
} as const

export const ADMIN_ACCESS = {
  title: 'Temporary admin access',
  who: (name: string | null | undefined, id: string | null | undefined) => name ? `${name}${id ? ` (${EMPLOYEE_ID} ${id})` : ''}` : '',
  endsIn: (clockText: string, at: string) => `Ends in ${clockText}${at ? ` (${at})` : ''}`,
  waitingEnd: 'Waiting for the machine to confirm the end time.',
  states: {
    REQUESTED: 'Turning on…',
    GRANTING: 'Turning on…',
    REVOKING: 'Turning off…',
    OVERDUE: 'Should have ended. Waiting for the machine to confirm.',
    FAILED: "Couldn't turn on temporary admin access.",
    REVOKED: 'Access ended',
    EXPIRED: 'Access ended',
  } as Record<string, string>,
  endNow: 'End now',
  endTitle: 'End temporary admin access now?',
  endBody: (name: string, device: string) => `${name} goes back to a regular person on ${device}.`,
  endCheck: 'The dashboard checks the machine afterwards.',
  typePhrase: 'To confirm, type exactly:',
  keep: 'Keep it',
  ending: 'Ending…',
  ended: 'Ending temporary admin access. You can follow it in Changes.',
  endFailed: "Couldn't end temporary admin access. Try again.",
  someone: 'Someone',
  fiveMinutes: 'Temporary admin access ends in 5 minutes.',
  oneMinute: 'Temporary admin access ends in 1 minute.',
} as const

export const SAME_CNIC = {
  title: (people: number) => people > 2 ? `Same CNIC on ${formatCountText(people)} people` : 'Same CNIC on two people',
  intro: 'Their check-ins are set aside until you confirm they are one person, or HR fixes the wrong CNIC. Nothing is deleted or merged.',
  needsDecision: 'Needs a decision',
  confirmedChip: 'Confirmed',
  confirmed: 'Same person (confirmed)',
  loadFailed: "Couldn't check for same CNICs.",
  loading: 'Checking for same CNICs…',
  classification: {
    EXACT_NAME_MATCH: 'Same name',
    POSSIBLE_NAME_VARIANT: 'Similar names',
    MIXED_NAMES_HIGH_RISK: 'Different names — check with HR first',
  } as Record<string, string>,
  advice: {
    CONFIRM_SAME_EMPLOYEE: 'Probably one person added twice. If so, confirm it.',
    HR_IDENTITY_REVIEW: "Ask HR whose CNIC this is, then fix the wrong person's CNIC.",
  } as Record<string, string>,
  adviceFallback: 'Ask HR which record is right.',
  checkins: (count: number) => plural(count, 'check-in'),
  last: (when: string) => `last ${when}`,
  noCheckins: 'No check-ins yet',
  member: (name: string, id: string) => `${name} — ${EMPLOYEE_ID} ${id}`,
  yes: 'Yes, same person…',
  editMember: (id: string) => `Edit ${EMPLOYEE_ID} ${id}`,
  undo: 'Undo',
  emptyTitle: 'No same CNICs on this machine.',
  emptyBody: 'Everyone has their own CNIC.',
  noneConfirmed: 'Nothing confirmed yet.',
  cnic: 'CNIC',
  dialogTitle: 'Are these the same person?',
  dialogUndoTitle: 'Undo “same person”?',
  dialogDescription: 'Confirm only if HR has checked that these records belong to one person.',
  dialogUndoDescription: 'Their new check-ins will be not matched again until this is sorted out.',
  nothingChanges: 'Nothing on the machine changes: no one is removed or merged, and no check-ins are changed.',
  why: 'Why?',
  typeExactly: 'To confirm, type exactly:',
  password: 'Your password',
  confirmButton: 'Yes, same person',
  undoButton: 'Undo',
  saving: 'Saving…',
  cancel: 'Cancel',
  errReason: 'Write a short reason (at least 10 letters).',
  errPhrase: "The confirmation doesn't match. Type it exactly as shown.",
  errPassword: 'Type your password.',
  saved: 'Saved: same person. Their check-ins can now go to the HR system.',
  undone: 'Undone. Their new check-ins will be set aside again.',
  failed: "Couldn't save. Try again.",
} as const

export const OLD_CHECKINS = {
  title: 'Old check-ins without a CNIC',
  intro: (count: number) => `${plural(count, 'check-in')} can't go to the HR system until you add the person's details.`,
  sendLater: "Saving details doesn't send them yet: you send them in Attendance › Fix tools › Re-check not-matched check-ins.",
  canFix: 'Can fix here',
  otherStep: 'Needs another step',
  loadFailed: "Couldn't load old check-ins.",
  loading: 'Loading old check-ins…',
  searchLabel: 'Search name or Employee ID',
  noLonger: 'No longer on this machine',
  facts: (count: number, range: string) => `${plural(count, 'check-in')}${range ? ` · ${range}` : ''}`,
  paths: {
    HR_DIRECTORY_EVIDENCE: 'Needs HR details',
    HR_DIRECTORY_EVENT_GROUP: 'Needs HR details',
    ACTIVE_USER_ENRICHMENT: 'Add the CNIC to the person on the machine',
    CURRENT_IDENTITY_EVIDENCE: 'Check it is the same person',
    VERIFIED_TOMBSTONE_REPAIR: 'Removed person (record kept)',
    IDENTITY_CONFLICT_REVIEW: 'Fix the Same CNIC first',
    IDENTITY_REUSE_REVIEW: 'This Employee ID was used by someone else before. Ask IT.',
  } as Record<string, string>,
  pathFallback: 'Ask IT what to do next.',
  addHere: 'Add the details here. Then send the not-matched check-ins in Attendance › Fix tools.',
  addCnic: 'Add CNIC',
  confirmSame: 'Confirm same person',
  enterHr: 'Enter HR details',
  openSameCnic: 'Open Same CNIC',
  emptyTitle: 'No old check-ins are waiting.',
  emptyBody: 'Everything from this machine can go to the HR system.',
  noneOther: 'Nothing here.',
  dialogHr: 'Enter HR details',
  dialogSame: 'Confirm it is the same person',
  dialogHrDescription: "Copy the details from HR's records. Saving doesn't send the check-ins yet.",
  dialogSameDescription: 'Confirm these old check-ins belong to the person who is on the machine now.',
  who: (id: string, device: string) => `${EMPLOYEE_ID} ${id} · ${device}`,
  range: (count: number, from: string, to: string) => `${plural(count, 'check-in')} from ${from} to ${to}.`,
  cnic: 'CNIC (from HR)',
  hrId: HR_EMPLOYEE_ID,
  hrIdHelp: (id: string) => `Type the number HR has. It may differ from the ${EMPLOYEE_ID} on the machine (${id}).`,
  service: 'Service number',
  name: 'Name (as in HR)',
  zone: 'HR zone (optional)',
  sameNote: 'The dashboard checks that these check-ins belong to this person on this machine.',
  hrNote: "Don't guess. The service number and name must match HR's records exactly.",
  how: 'How did you check this? (saved in the record)',
  checked: 'I checked these details with HR',
  typeExactly: 'To confirm, type exactly:',
  password: 'Your password',
  saving: 'Saving…',
  save: 'Save',
  saveDetails: 'Save details',
  cancel: 'Cancel',
  errCnic: 'CNIC must be 13 digits.',
  errHrId: `${HR_EMPLOYEE_ID} can only have digits.`,
  errService: 'Service number can only have letters, digits, dot, dash or underscore.',
  errName: 'Type the name.',
  errReason: 'Write a short reason (at least 10 letters).',
  errTick: 'Tick the box to confirm.',
  errPhrase: "The confirmation doesn't match. Type it exactly as shown.",
  errPassword: 'Type your password.',
  errChanged: 'Something changed. Refresh and try again.',
  failed: "Couldn't save the details. Try again.",
  saved: 'Details saved. Next: send the not-matched check-ins.',
  open: 'Open',
} as const

export const REMOVE_MANY = {
  selected: (count: number) => `${formatCountText(count)} selected`,
  kept: 'Their past check-ins stay saved.',
  clear: 'Clear',
  checking: 'Checking…',
  remove: (count: number) => `Remove ${formatCountText(count)}`,
  noneLeft: 'None of these people can be removed now (something changed). Your ticks were cleared.',
  someLeft: (count: number) => `${plural(count, 'person')} can't be removed any more and ${count === 1 ? 'was' : 'were'} taken off your list.`,
  changed: 'Some details changed on the machine. Check the list again.',
  checkFailed: "Couldn't check your selection. Nothing was removed.",
  deviceChanged: 'You changed device. Nothing was removed.',
  noneNow: 'None of these people can be removed now. Nothing was removed.',
  title: (count: number, device: string) => `Remove ${plural(count, 'person')} from ${device}?`,
  description: 'They are removed one by one, and each removal is checked on the machine. It stops if something looks wrong.',
  member: (name: string, id: string) => `${name} — ${EMPLOYEE_ID} ${id}`,
  why: 'Why?',
  typeExactly: 'To confirm, type exactly:',
  phraseNote: (deviceId: string) => `(${deviceId} is this device's number in the dashboard)`,
  password: 'Your password',
  cancel: 'Cancel',
  starting: 'Starting…',
  submit: (count: number) => `Remove ${plural(count, 'person')}`,
  listChanged: 'The list changed while this was open. Check it and type the new confirmation.',
  startFailed: "Couldn't start. Nothing was removed.",
  errReason: 'Write a short reason (at least 10 letters).',
  errPhrase: "The confirmation doesn't match. Type it exactly as shown.",
  errPassword: 'Type your password.',
  progressTitle: (device: string) => `Removing people from ${device}`,
  progressCounts: (removed: number, waiting: number, failed: number, skipped: number) => `${formatCountText(removed)} removed · ${formatCountText(waiting)} waiting · ${formatCountText(failed)} not removed · ${formatCountText(skipped)} skipped`,
  progressDone: (done: number, total: number) => `${formatCountText(done)} of ${formatCountText(total)} done`,
  stop: 'Stop',
  stopPassword: 'Your password',
  stopConfirm: 'Stop removing',
  stopping: 'Stopping…',
  clearCard: 'Clear',
  toastStarted: (count: number) => `Removing ${plural(count, 'person')}. You can follow it in Changes.`,
  toastDone: (count: number) => `Done: ${plural(count, 'person')} removed. Their past check-ins are still saved.`,
  toastProblems: (ok: number, bad: number) => `Finished with problems: ${formatCountText(ok)} removed, ${formatCountText(bad)} not removed. See the list in Changes.`,
  toastStopping: "Stopping. The person being removed now will finish; the rest won't be touched.",
  lostTouch: 'Lost touch with the removal. It keeps going; this page will catch up.',
  stopFailed: "Couldn't stop it. Try again.",
} as const

/** Heading of a removal job, by status. */
export function removalHeading(status: string, counts: { succeeded: number; failed: number; pending: number; canceled: number; expired: number; requested: number }): string {
  const ok = counts.succeeded
  const bad = counts.failed
  const left = counts.pending + counts.canceled + counts.expired
  switch ((status ?? '').toUpperCase()) {
    case 'QUEUED': return 'Starting…'
    case 'RUNNING': return `Removing: ${formatCountText(ok + bad)} of ${formatCountText(counts.requested)} done`
    case 'CANCEL_REQUESTED': return 'Stopping after the person being removed now'
    case 'SUCCEEDED': return `Done: ${formatCountText(ok)} removed`
    case 'PARTIAL': return `Finished with problems: ${formatCountText(ok)} removed, ${formatCountText(bad + left)} not removed`
    case 'FAILED': return `Stopped by a problem: ${formatCountText(ok)} removed, ${formatCountText(bad + left)} not removed`
    case 'EXPIRED': return `Ran out of time: ${formatCountText(ok)} removed, ${formatCountText(left + bad)} not done`
    case 'CANCELED':
    case 'CANCELLED': return `Stopped: ${formatCountText(ok)} removed, ${formatCountText(left)} not touched`
    default: return 'Removing people'
  }
}

/** One person's line in a removal job. */
export function removalItem(status: string, reason?: string | null): string {
  switch ((status ?? '').toUpperCase()) {
    case 'SUCCEEDED': return 'removed'
    case 'FAILED': return `not removed: ${reason || "the machine didn't confirm it"}`
    case 'CANCELED':
    case 'CANCELLED': return 'skipped'
    case 'EXPIRED': return 'not done in time'
    case 'RUNNING':
    case 'DISPATCHED':
    case 'ACKNOWLEDGED': return 'being removed…'
    default: return 'waiting'
  }
}

// Words of the app frame, Home, Devices and Settings (01-foundation Words,
// 03-home-devices Words, 08-advanced Settings, 09-delivery menu note). Pages
// import these; the device status words themselves are in deviceStatus.ts.
import { formatCount } from './glossary'

const n = (value: number) => formatCount(value)
const s = (count: number, one: string, many = `${one}s`) => (count === 1 ? one : many)

export const FRAME_WORDS = {
  mainMenu: 'Main menu',
  advancedTools: 'Advanced tools',
  skipLink: 'Skip to main content',
  brandName: 'State Life',
  brandSub: 'Attendance',
  pakistanTime: 'All times are Pakistan time',
  publicStatus: 'Public status page',
  opensInNewTab: '(opens in a new tab)',
  more: 'More',
  moreTitle: 'More',
  signedInAs: (name: string) => `Signed in as ${name}`,
  signOut: 'Sign out',
  signingOut: 'Signing out…',
  settings: 'Settings',
  showTechnical: 'Show technical details',
  showTechnicalHelp: 'Opens every "Technical details" box. For IT.',
  account: (name: string) => `${name}, account menu`,
  switchedOff: 'Switched off',
  switchedOffHidden: 'Switched off for now',
  /** Problems badge hidden text. */
  problemsBadge: (count: number) => `${n(count)} new ${s(count, 'problem')}`,
} as const

/** Menu items: label and the one-line description used in the More sheet. */
export const MENU_WORDS = {
  home: { label: 'Home', description: 'How every attendance device is doing right now' },
  devices: { label: 'Devices', description: 'Every attendance device, the ones that need attention first' },
  attendance: { label: 'Attendance', description: 'Check-ins and what reached the HR system' },
  problems: { label: 'Problems', description: 'Things on the devices that need someone to look' },
  people: { label: 'People', description: 'People saved on each attendance machine' },
  recover: { label: 'Recover attendance', description: 'Read records straight from a machine to find missed check-ins' },
  updates: { label: 'Software updates', description: 'Update the link box software' },
  setup: { label: 'Set up a device', description: 'Prepare a new link box for a machine' },
  settings: { label: 'Settings', description: 'Check-in phone messages and display options' },
} as const

/** The Live indicator (01-foundation C). */
export const LIVE_WORDS = {
  connecting: 'Connecting…',
  live: 'Live',
  reconnecting: 'Reconnecting…',
  offline: 'Offline · showing last saved data',
  offlineShort: 'Offline',
  backOnline: 'Back online',
  liveDetail: (time: string) => (time ? `Live: changes show up as they happen. Last update ${time}.` : 'Live: changes show up as they happen.'),
  notLiveDetail: (time: string) => (time ? `Not live since ${time}. The page keeps trying. If this stays, check the internet connection.` : 'Not live yet. The page keeps trying. If this stays, check the internet connection.'),
  banner: (time: string) => (time ? `Offline · showing last saved data from ${time}. Trying again…` : 'Offline · trying to load the latest data again…'),
  tryNow: 'Try now',
  trying: 'Trying…',
  statusLabel: 'Connection',
} as const

/** The menu-change note on Home (09-delivery D, DLV-4; removed in PR-9). */
export const MENU_NOTE_WORDS = {
  title: 'The menu has new names',
  body: 'Old links and bookmarks still work.',
  whatMoved: 'What moved',
  gotIt: 'Got it',
  close: 'Close',
  moves: [
    { old: 'Fleet', now: 'Home and Devices' },
    { old: 'Alerts', now: 'Problems' },
    { old: 'Users', now: 'People' },
    { old: 'Reconciliation', now: 'Recover attendance (Advanced tools)' },
    { old: 'Firmware', now: 'Software updates and Set up a device' },
  ],
} as const

export const REFRESH_WORDS = {
  failed: (time: string) => (time ? `Could not refresh. Showing what we knew at ${time}. Trying again…` : 'Could not refresh. Trying again…'),
  upToDate: 'Up to date',
  tryNow: 'Try now',
  trying: 'Trying…',
  loadFailedTitle: 'Could not load devices',
  loadFailedBody: 'Check the internet connection, then try again.',
  updatedAgo: (ago: string) => `Updated ${ago}`,
  updatedAt: (time: string) => `Updated at ${time}`,
} as const

export const HOME_WORDS = {
  title: 'Home',
  description: 'How every attendance device is doing right now.',
  summary: 'Summary',
  loadingDevices: 'Loading devices…',
  tiles: {
    devices: 'Devices',
    working: 'Working',
    attention: 'Needs attention',
    offline: 'Offline',
  },
  tileLinks: {
    devices: 'Show all devices',
    working: 'Show working devices',
    attention: 'Show devices that need attention',
    offline: 'Show offline devices',
  },
  divisionSplit: (life: number, health: number) => `Life ${n(life)} · Health ${n(health)}`,
  settingUp: (count: number) => `+ ${n(count)} setting up`,
  withNote: (count: number) => `${n(count)} with a note`,
  publicOnline: (online: number, total: number) => `${n(online)} of ${n(total)} online on the public status page`,
  panelWorking: (working: number, total: number) => `${n(working)} of ${n(total)} working`,
  panelAllWorking: (total: number) => (total === 1 ? 'Working' : `All ${n(total)} working`),
  panelNone: 'No devices in use',
  panelMore: (count: number) => `and ${n(count)} more · Show all`,
  panelNotes: (count: number) => `${n(count)} working with a note`,
  panelShowAll: (division: string) => `Show all ${division} devices`,
  open: 'Open',
  /** The row link's name: what the row shows (name, status, reason, since), so it is read out in full. */
  openDevice: (name: string, ...parts: Array<string | null | undefined>) => [`Open ${name}`, ...parts.filter(Boolean)].join(', '),
  since: (when: string, howLong: string) => (howLong ? `Since ${when} · ${howLong}` : `Since ${when}`),
  emptyTitle: 'No devices yet',
  emptyBody: 'New devices appear here by themselves once they are set up.',
  spares: (total: number, life: number, health: number) => `Spare devices (not in use): ${n(total)} · Life ${n(life)} · Health ${n(health)}`,
  sparesOne: (total: number) => `Spare devices (not in use): ${n(total)}`,
  leases: (count: number) => count === 1 ? '1 person has temporary admin access on a machine' : `${n(count)} people have temporary admin access on machines`,
} as const

/** Home headline (03-home-devices Words, finalizer "Headline"). */
export function homeHeadline({ total, attention, offline, setup = 0, division, attentionDivision, offlineDivision }: {
  total: number
  attention: number
  offline: number
  setup?: number
  /** The chosen division word ("Life"), when the switch is not All. */
  division?: string | null
  /** When only one division has the attention / offline devices (switch on All). */
  attentionDivision?: string | null
  offlineDivision?: string | null
}): string {
  const scope = division ? `${division} ` : ''
  if (!attention && !offline) {
    if (!total) return setup ? `${n(setup)} ${scope}${s(setup, 'device is', 'devices are')} being set up.` : ''
    return total === 1 ? `The ${scope}device is working.` : `All ${n(total)} ${scope}devices are working.`
  }
  const att = attention
    ? `${n(attention)} ${attentionDivision && !division ? `${attentionDivision} ` : scope}${s(attention, 'device needs', 'devices need')} attention`
    : ''
  if (!offline) return `${att}.`
  const offlineScope = offlineDivision && !division ? `${offlineDivision} ` : scope
  if (!attention) return `${n(offline)} ${offlineScope}${s(offline, 'device is', 'devices are')} offline.`
  if (offlineDivision && !division) return `${att} and ${n(offline)} ${offlineDivision} ${s(offline, 'device is', 'devices are')} offline.`
  return `${att} and ${n(offline)} ${s(offline, 'is', 'are')} offline.`
}

/** "Health also has 1 offline device." when one division is chosen and the other has trouble. */
export function otherDivisionLine(division: string, attention: number, offline: number): string {
  const parts = [
    offline ? `${n(offline)} offline ${s(offline, 'device')}` : '',
    attention ? `${n(attention)} ${s(attention, 'device that needs', 'devices that need')} attention` : '',
  ].filter(Boolean)
  return parts.length ? `${division} also has ${parts.join(' and ')}.` : ''
}

export const ATTENDANCE_TODAY_WORDS = {
  title: 'Attendance today',
  received: 'Received',
  sent: 'Sent to HR system (Oracle)',
  sentShort: 'Sent to HR system',
  waiting: 'Waiting',
  notMatched: 'Not matched to an employee',
  notMatchedShort: 'Not matched',
  all: 'All',
  life: 'Life',
  health: 'Health',
  oldestWaiting: (when: string) => `Oldest since ${when}`,
  lastSent: (time: string, ago: string) => `Last check-in reached the HR system at ${time} · ${ago}.`,
  noneToday: 'No check-ins yet today.',
  counting: "Counting today's check-ins. This takes a few seconds.",
  failed: "Could not load today's attendance figures.",
  seeAttendance: 'See attendance',
  refused: (problem: number, setAside: number) => `Refused by the HR system: ${n(problem)} · Set aside: ${n(setAside)}`,
  numberLabel: (division: string, count: number, what: 'received' | 'sent' | 'waiting' | 'not_matched') =>
    `${division}: ${n(count)} ${({ received: 'received', sent: 'sent to the HR system', waiting: 'waiting', not_matched: 'not matched to an employee' })[what]}`,
} as const

export const PROBLEMS_BLOCK_WORDS = {
  title: 'New problems',
  header: (fresh: number, seen: number) => `${n(fresh)} new · ${n(seen)} seen and still open`,
  seeAll: 'See all problems',
  empty: 'No new problems.',
  seenStillOpen: (seen: number) => `${n(seen)} seen ${s(seen, 'problem is', 'problems are')} still open. See ${s(seen, 'it', 'them')}`,
  failed: 'Could not load problems.',
  severity: { CRITICAL: 'Urgent', HIGH: 'Important', WARNING: 'Important', MEDIUM: 'Important', LOW: 'Low', INFO: 'Low' } as Record<string, string>,
} as const

export const DEVICES_WORDS = {
  title: 'Devices',
  description: 'Every attendance device, the ones that need attention first.',
  listLabel: 'Devices',
  searchLabel: 'Search devices',
  searchPlaceholder: 'Search by name, city, code or serial',
  status: 'Status',
  statusChips: {
    all: 'All',
    attention: 'Needs attention',
    offline: 'Offline',
    working: 'Working',
    setup: 'Setting up',
  },
  spareChip: 'Spare',
  spareSwitch: 'Show spare devices only',
  noteChip: 'With a note',
  city: 'City',
  brand: 'Machine brand',
  brandAny: 'All',
  version: 'Software version',
  versionAny: 'Any',
  versionUnknown: 'Software version not known',
  versionOption: (version: string, count: number) => `Software ${version} · ${n(count)} ${s(count, 'device')}`,
  /** Shown in a data-brand span after a version used only on face machines. */
  versionBrand: 'Hikvision',
  versionActive: (version: string) => (version === 'unknown' ? 'Software version not known' : `Software ${version}`),
  sort: 'Sort',
  sortOptions: { attention: 'Needs attention first', name: 'Name (A to Z)', seen: 'Not seen for longest' },
  filters: 'Filters',
  filtersCount: (count: number) => (count ? `Filters (${count})` : 'Filters'),
  showDevices: (count: number) => `Show ${n(count)} ${s(count, 'device')}`,
  clearAll: 'Clear all',
  clearAllFilters: 'Clear all filters',
  columns: { device: 'Device', status: 'Status', happening: 'What is happening', lastSeen: 'Last seen', machine: 'Machine' },
  unfiltered: (total: number) => `${n(total)} ${s(total, 'device')}`,
  noMatch: 'No devices match these filters.',
  noSpares: 'No spare devices.',
  noSparesBody: 'To make a device a spare, open it and mark it as a spare.',
  notCounted: 'Not counted in any totals.',
  notSeen: 'Not seen yet',
  noMachine: 'No machine linked yet',
  people: 'People',
  peopleOn: (name: string) => `People on ${name}`,
  rowLabel: (name: string, division: string, status: string) => `Open ${name}, ${division}, ${status.toLowerCase()}`,
  activeCity: (city: string) => `City: ${city}`,
  activeBrand: (brand: string) => `Machine brand: ${brand}`,
  activeSearch: (q: string) => `Search: ${q}`,
  announceOffline: (name: string) => `${name} is offline.`,
  announceWorking: (name: string) => `${name} is working again.`,
} as const

export const SETTINGS_WORDS = {
  title: 'Settings',
  description: 'Choices for how the dashboard shows things to you.',
  display: 'Display',
  technical: 'Show technical details',
  technicalHelp: 'Opens every "Technical details" box on every page. For IT. Remembered in this browser.',
  contact: 'Contact IT',
  contactBody: 'For help with a device, a check-in or this dashboard, contact IT.',
  oldScreens: 'Use the old screens for now',
  oldScreensBody: 'The old screens stay available for a short time while everyone gets used to the new ones.',
  oldScreensLink: 'Open the old screens',
  account: 'Account',
} as const

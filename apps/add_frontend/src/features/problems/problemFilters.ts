// The Problems page filters, kept in the URL (PRB-3). Every key here is a plain
// word or id; nothing personal is ever written (useUrlState refuses the
// cnic / password / reason / confirmation keys, and a CNIC-like search).
import { flag, oneOf, pktDate, text, type UrlField, type UrlValues } from '../../lib/urlState'
import { pakistanDay } from '../../lib/time'
import type { DivisionChoice } from '../../lib/prefs'

export const PROBLEM_TABS = ['new', 'seen', 'fixed'] as const
export type ProblemTab = typeof PROBLEM_TABS[number]

export const SERIOUS = ['urgent', 'important', 'low'] as const
export type Serious = typeof SERIOUS[number]

export const PROBLEM_TYPES = ['link_box', 'link_box_memory', 'machine', 'checkins', 'hr_system', 'people', 'software', 'phone'] as const
export type ProblemType = typeof PROBLEM_TYPES[number]

export const WHEN = ['', 'any', 'today', 'yesterday', '7d', '30d', 'custom'] as const
export type When = typeof WHEN[number]
export type EffectiveWhen = Exclude<When, ''>

export const SORTS = ['serious', 'newest', 'oldest'] as const
export type ProblemSort = typeof SORTS[number]

/** Which backend severities each chip covers (all five levels are reachable). */
export const SEVERITIES_OF: Record<Serious, string[]> = {
  urgent: ['CRITICAL'],
  important: ['HIGH'],
  low: ['WARNING', 'MEDIUM', 'INFO'],
}

export const seriousOf = (severity: string | null | undefined): Serious => {
  const value = (severity || '').toUpperCase()
  if (value === 'CRITICAL') return 'urgent'
  if (value === 'HIGH') return 'important'
  return 'low'
}

/** Lower is worse: the backend order (be/web.py severity_order). */
export const severityRank = (severity: string | null | undefined) => {
  const rank = ['CRITICAL', 'HIGH', 'WARNING', 'MEDIUM', 'INFO'].indexOf((severity || '').toUpperCase())
  return rank === -1 ? 5 : rank
}

/** A comma list of known values ("urgent,low"); unknown parts are dropped. */
function list<T extends string>(values: readonly T[]): UrlField<T[]> {
  const allowed = new Set<string>(values)
  return {
    parse: (raw) => {
      if (!raw) return []
      const picked = new Set(raw.split(',').map((part) => part.trim()).filter((part) => allowed.has(part)))
      return values.filter((value) => picked.has(value))
    },
    serialize: (value) => (value.length ? values.filter((item) => value.includes(item)).join(',') : null),
    defaultValue: [],
  }
}

/** `tab=new|seen|fixed`; the old `tab=open` reads as `new`. */
const tabField: UrlField<ProblemTab> = {
  parse: (raw) => (raw === 'open' ? 'new' : (PROBLEM_TABS as readonly string[]).includes(raw || '') ? raw as ProblemTab : 'new'),
  serialize: (value) => value,
  defaultValue: 'new',
}

/** A device id (connector id), set by a picker, never typed. */
const deviceField: UrlField<string> = {
  parse: (raw) => (raw && /^[A-Za-z0-9-]{1,100}$/.test(raw) ? raw : ''),
  serialize: (value) => (value && /^[A-Za-z0-9-]{1,100}$/.test(value) ? value : null),
  defaultValue: '',
}

export const PROBLEM_URL_SCHEMA = {
  tab: tabField,
  serious: list(SERIOUS),
  type: list(PROBLEM_TYPES),
  device: deviceField,
  when: oneOf(WHEN, ''),
  from: pktDate(),
  to: pktDate(),
  q: text('', { maxLength: 100 }),
  att: flag(),
  sort: oneOf(SORTS, 'serious'),
  old: flag(),
}

export type ProblemFilters = UrlValues<typeof PROBLEM_URL_SCHEMA>

/** The date window in force: Fixed defaults to the last 7 days, the others to any time. */
export const effectiveWhen = (filters: Pick<ProblemFilters, 'tab' | 'when'>): EffectiveWhen =>
  filters.when || (filters.tab === 'fixed' ? '7d' : 'any')

const DAY_MS = 86_400_000
const midnight = (key: string) => new Date(`${key}T00:00:00+05:00`)

/** UTC bounds of a Pakistan-time window. Stable for a whole day, so it makes a stable request key. */
export function dateWindow(when: EffectiveWhen, from: string, to: string, now: Date | number = Date.now()) {
  const today = midnight(pakistanDay(now))
  const iso = (date: Date) => date.toISOString()
  switch (when) {
    case 'today': return { from_time: iso(today) }
    case 'yesterday': return { from_time: iso(new Date(today.getTime() - DAY_MS)), to_time: iso(today) }
    case '7d': return { from_time: iso(new Date(today.getTime() - 6 * DAY_MS)) }
    case '30d': return { from_time: iso(new Date(today.getTime() - 29 * DAY_MS)) }
    case 'custom': return {
      ...(from ? { from_time: iso(midnight(from)) } : {}),
      ...(to ? { to_time: iso(new Date(midnight(to).getTime() + DAY_MS)) } : {}),
    }
    default: return {}
  }
}

export type ProblemQueue = 'NEEDS_ACTION' | 'SEEN' | 'LEGACY_SEEN' | 'RESOLVED'

export const queueFor = (filters: Pick<ProblemFilters, 'tab' | 'old'>): ProblemQueue =>
  filters.tab === 'fixed' ? 'RESOLVED' : filters.tab === 'seen' ? (filters.old ? 'LEGACY_SEEN' : 'SEEN') : 'NEEDS_ACTION'

/** One page holds every open problem (one open row per device and kind); Fixed pages by 50. */
export const pageSize = (tab: ProblemTab) => (tab === 'fixed' ? 50 : 500)

export type ProblemApiParams = Record<string, string | number | undefined>

/** GET /api/v1/alerts parameters for the filters (without the cursor). */
export function toApiParams(filters: ProblemFilters, division: DivisionChoice, now: Date | number = Date.now()): ProblemApiParams {
  const severities = filters.serious.flatMap((value) => SEVERITIES_OF[value])
  return {
    queue: queueFor(filters),
    order: filters.tab === 'fixed' ? 'fixed_newest' : filters.sort === 'serious' ? undefined : filters.sort,
    severity: severities.length ? severities.join(',') : undefined,
    type: filters.type.length ? filters.type.join(',') : undefined,
    connector_id: filters.device || undefined,
    q: filters.q.trim() || undefined,
    affects_attendance: filters.att ? 'true' : undefined,
    division: division === 'all' ? undefined : division,
    ...dateWindow(effectiveWhen(filters), filters.from, filters.to, now),
    limit: pageSize(filters.tab),
  }
}

/** True when any filter narrows the list (the tab, sort and division do not count). */
export const hasFilters = (filters: ProblemFilters) =>
  Boolean(filters.serious.length || filters.type.length || filters.device || filters.q.trim() || filters.att || filters.when)

/** Everything "Clear all" resets (the tab stays). */
export const CLEARED: Partial<ProblemFilters> = { serious: [], type: [], device: '', when: '', from: '', to: '', q: '', att: false, sort: 'serious', old: false }

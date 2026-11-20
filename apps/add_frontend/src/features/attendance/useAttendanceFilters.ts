// Attendance filters in the page URL (ATT-7). A link, a reload or Back shows
// the same list. Rules: the date is `when` (+ `from` / `to` for picked dates),
// places are `device`, statuses `status`; a CNIC typed in the search box goes
// to the server as `cnic=` but never into the page URL; old links
// (`view=all-events | needs-review | release-history`) are rewritten once.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { flag, integer, oneOf, pktDate, SENSITIVE_DIGITS, TYPING_DEBOUNCE_MS, useUrlState, type UrlField } from '../../lib/urlState'
import type { DivisionChoice } from '../../lib/prefs'
import type { DeliveryGroup } from '../../words/attendance'
import { GROUP_ORDER, PHONE_FILTERS, type PhoneFilter } from './attendanceWords'
import { pktPresetRange, type WhenPreset } from './pktDates'

export const PAGE_SIZE = 100
export const MAX_ROWS = 500
export const MAX_PLACES = 50

export type AttendanceView = 'checkins' | 'fix-tools'
export type FixTool = '' | 'auto-send' | 'held-staff' | 'held-old' | 'history'
const TOOLS: readonly FixTool[] = ['auto-send', 'held-staff', 'held-old', 'history']

function list<T extends string>(allowed: readonly T[], max = 50): UrlField<T[]> {
  const set = new Set<string>(allowed)
  const empty: T[] = []
  return {
    parse: (raw) => raw ? [...new Set(raw.split(',').map((item) => item.trim()).filter((item) => !allowed.length || set.has(item)))].slice(0, max) as T[] : empty,
    serialize: (value) => (value.length ? value.join(',') : null),
    defaultValue: empty,
  }
}

const freeList = (max = MAX_PLACES): UrlField<string[]> => {
  const empty: string[] = []
  return {
    parse: (raw) => raw ? [...new Set(raw.split(',').map((item) => item.trim()).filter((item) => /^[\w.:-]{1,120}$/.test(item)))].slice(0, max) : empty,
    serialize: (value) => (value.length ? value.join(',') : null),
    defaultValue: empty,
  }
}

export const FILTER_SCHEMA = {
  when: oneOf(['today', 'yesterday', '7d', 'custom'] as const, 'today'),
  from: pktDate(),
  to: pktDate(),
  device: freeList(),
  status: list<DeliveryGroup>(GROUP_ORDER),
  type: oneOf(['both', 'in', 'out'] as const, 'both'),
  by_admin: flag(),
  phone: list<PhoneFilter>(Object.keys(PHONE_FILTERS) as PhoneFilter[]),
  time: oneOf(['any', 'wrong'] as const, 'any'),
  arrived: oneOf(['any', 'live', 'later'] as const, 'any'),
  no_id: flag(),
  machine_match: flag(),
  event: integer(0, { min: 0 }),
}

export type FilterValues = {
  when: WhenPreset
  from: string
  to: string
  device: string[]
  status: DeliveryGroup[]
  type: 'both' | 'in' | 'out'
  by_admin: boolean
  phone: PhoneFilter[]
  time: 'any' | 'wrong'
  arrived: 'any' | 'live' | 'later'
  no_id: boolean
  machine_match: boolean
  event: number
}

/** A 13-digit CNIC, dashes allowed ("35202-1234567-1"). */
export const CNIC_PATTERN = /^\d{5}-?\d{7}-?\d$/
export const cnicDigits = (value: string) => (CNIC_PATTERN.test(value.trim()) ? value.replace(/\D/g, '') : null)

/** Rewrites old Attendance links once; returns null when nothing changes. */
export function rewriteLegacySearch(search: string): string | null {
  const params = new URLSearchParams(search)
  const view = params.get('view')
  if (view === 'all-events') params.set('view', 'checkins')
  else if (view === 'needs-review') {
    params.set('view', 'fix-tools')
    params.set('tool', 'held-staff')
    params.delete('user_key')
  } else if (view === 'release-history') {
    params.set('view', 'fix-tools')
    params.set('tool', 'history')
    if (params.get('release_job')) params.set('kind', 'old-release')
  } else return null
  const query = params.toString()
  return query ? `?${query}` : ''
}

export function readView(search: string): { view: AttendanceView; tool: FixTool } {
  const params = new URLSearchParams(search)
  const raw = params.get('view')
  const legacyTool: FixTool = raw === 'needs-review' ? 'held-staff' : raw === 'release-history' ? 'history' : ''
  const view: AttendanceView = raw === 'fix-tools' || legacyTool ? 'fix-tools' : 'checkins'
  const tool = (params.get('tool') || legacyTool) as FixTool
  return { view, tool: view === 'fix-tools' && TOOLS.includes(tool) ? tool : '' }
}

/** Server parameters for the check-in list (`/api/v1/attendance`). */
export function listParams(filters: FilterValues, division: DivisionChoice, search: string, now: Date | number = Date.now()): URLSearchParams {
  const params = new URLSearchParams()
  const window = pktPresetRange(filters.when, now, filters.from, filters.to)
  params.set('from_time', window.from_time)
  if (window.to_time) {
    params.set('to_time', window.to_time)
    params.set('to_exclusive', 'true')
  }
  if (division !== 'all') params.set('division', division)
  filters.device.forEach((id) => params.append('device_id', id))
  filters.status.forEach((group) => params.append('delivery_group', group))
  const term = search.trim()
  const cnic = cnicDigits(term)
  if (cnic) params.set('cnic', cnic)
  else if (term) params.set('q', term)
  if (filters.type === 'in') params.set('punch', '0')
  if (filters.type === 'out') params.set('punch', '1')
  if (filters.by_admin) params.set('forced', 'true')
  filters.phone.forEach((value) => params.append('notify_status', PHONE_FILTERS[value]))
  if (filters.time === 'wrong') {
    params.append('clock_quality', 'DRIFTED')
    params.append('clock_quality', 'INVALID')
  }
  if (filters.arrived !== 'any') params.set('source_group', filters.arrived)
  if (filters.no_id) params.append('ords_status', 'CNIC_NOT_LINKED')
  if (filters.machine_match) params.set('unknown_synced_cnic', 'true')
  params.set('limit', String(PAGE_SIZE))
  // The phone-message figures moved to Settings: a list refresh never recomputes them.
  params.set('notify_summary', 'false')
  return params
}

/** Server parameters for the totals (`/api/v1/attendance/summary`). */
export function summaryParams(filters: FilterValues, division: DivisionChoice, now: Date | number = Date.now()): URLSearchParams {
  const params = new URLSearchParams()
  const window = pktPresetRange(filters.when, now, filters.from, filters.to)
  params.set('from_time', window.from_time)
  if (window.to_time) params.set('to_time', window.to_time)
  if (division !== 'all') params.set('division', division)
  filters.device.forEach((id) => params.append('device_id', id))
  return params
}

/** How many "More filters" are on (date, place, status and search are not counted). */
export const moreFiltersCount = (filters: FilterValues) =>
  Number(filters.type !== 'both') + Number(filters.by_admin) + filters.phone.length + Number(filters.time === 'wrong') +
  Number(filters.arrived !== 'any') + Number(filters.no_id) + Number(filters.machine_match)

/** True when anything narrows the list beyond the date and division. */
export const narrowed = (filters: FilterValues, search: string) =>
  Boolean(search.trim()) || filters.device.length > 0 || filters.status.length > 0 || moreFiltersCount(filters) > 0

export const CLEARED: Partial<FilterValues> = {
  device: [], status: [], type: 'both', by_admin: false, phone: [], time: 'any', arrived: 'any', no_id: false, machine_match: false,
}

/**
 * The page's filters, view and search box. The search text lives here as
 * well as in the URL so a CNIC (never written to the URL) stays in the box.
 */
export function useAttendanceFilters() {
  const location = useLocation()
  const navigate = useNavigate()
  const [filters, setUrl] = useUrlState(FILTER_SCHEMA)
  const { view, tool } = useMemo(() => readView(location.search), [location.search])

  // Old links: rewrite once, keeping every other key (device_id, force_run, direct_run …).
  useLayoutEffect(() => {
    const next = rewriteLegacySearch(location.search)
    if (next !== null) navigate(`${location.pathname}${next}${location.hash}`, { replace: true, preventScrollReset: true })
  }, [location.hash, location.pathname, location.search, navigate])

  const urlQ = useMemo(() => new URLSearchParams(location.search).get('q') || '', [location.search])
  const [search, setSearchState] = useState(urlQ)
  const written = useRef(urlQ)
  const timer = useRef<number | null>(null)
  const searchRef = useRef({ pathname: location.pathname, search: location.search, hash: location.hash })
  searchRef.current = { pathname: location.pathname, search: location.search, hash: location.hash }
  // Back / a link changed `q`: the box follows. Our own writes are skipped.
  useEffect(() => {
    if (urlQ === written.current) return
    written.current = urlQ
    setSearchState(urlQ)
  }, [urlQ])
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current) }, [])

  const setSearch = useCallback((value: string) => {
    setSearchState(value)
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      timer.current = null
      const trimmed = value.trim()
      const safe = trimmed && !SENSITIVE_DIGITS.test(trimmed) && !CNIC_PATTERN.test(trimmed) ? trimmed.slice(0, 120) : ''
      const current = searchRef.current
      const params = new URLSearchParams(current.search)
      if ((params.get('q') || '') === safe) return
      if (safe) params.set('q', safe)
      else params.delete('q')
      written.current = safe
      const query = params.toString()
      navigate(`${current.pathname}${query ? `?${query}` : ''}${current.hash}`, { replace: true, preventScrollReset: true })
    }, TYPING_DEBOUNCE_MS)
  }, [navigate])

  /** Switches tab or tool; other keys stay. `extra` sets or removes (null) more keys. */
  const go = useCallback((next: { view?: AttendanceView; tool?: FixTool; extra?: Record<string, string | null> }, replace = false) => {
    const current = searchRef.current
    const params = new URLSearchParams(current.search)
    const nextView = next.view ?? readView(current.search).view
    if (nextView === 'checkins') params.delete('view')
    else params.set('view', nextView)
    const nextTool = next.tool ?? (next.view ? '' : readView(current.search).tool)
    if (nextView === 'fix-tools' && nextTool) params.set('tool', nextTool)
    else params.delete('tool')
    for (const [key, value] of Object.entries(next.extra || {})) {
      if (value === null) params.delete(key)
      else params.set(key, value)
    }
    const query = params.toString()
    navigate(`${current.pathname}${query ? `?${query}` : ''}${current.hash}`, { replace, preventScrollReset: true })
  }, [navigate])

  return { filters: filters as FilterValues, setFilters: setUrl as (change: Partial<FilterValues>, options?: { history?: 'push' | 'replace' }) => void, view, tool, go, search, setSearch }
}

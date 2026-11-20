// The Problems page (PRB-5; was Alerts). Tabs New / Seen / Fixed with counts,
// problems grouped by device, filters in the URL, Mark as seen / fixed with
// per-card busy state, the set-aside check-ins sheet and "Clean up old
// problems" in the ⋯ menu.
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Icon } from '../Icon'
import {
  ActionMenu, Button, Callout, DivisionTag, EmptyState, ErrorState, FilterBar, IconButton, PageHeader, Skeleton, StatusChip, Tabs, TabPanel,
  formatCount, useIsPhone, useToast, type ActionMenuItem, type ActiveFilter,
} from '../ui'
import { useUrlState } from '../lib/urlState'
import { useDivision } from '../lib/useDivision'
import { usePrefs } from '../lib/prefs'
import { ago, clock, day, dayTime, duration, pakistanDay } from '../lib/time'
import { api, queryString } from '../api'
import { plural } from '../words/glossary'
import { MARK_SEEN, PROBLEMS_PAGE, PROBLEM_CARD, PROBLEM_GROUP, PROBLEM_TYPE_LABELS } from '../words/problems'
import { divisionLabel } from '../lib/division'
import type { AlertResolveResponse, AttendanceQuarantineResponse, Device } from '../types'
import {
  CLEARED, PROBLEM_TYPES, PROBLEM_URL_SCHEMA, SERIOUS, SORTS, effectiveWhen, hasFilters, pageSize, seriousOf, severityRank, toApiParams,
  type EffectiveWhen, type ProblemFilters, type ProblemTab, type Serious,
} from './problems/problemFilters'
import { useProblems, type ProblemsData } from './problems/useProblems'
import { ProblemCard, deviceLabel, problemIsFixed, severityLabel, severityTone } from './problems/ProblemCard'
import { MarkFixedDialog } from './problems/MarkFixedDialog'
import { MarkSeenNote } from './problems/MarkSeenNote'
import { SetAsideSheet } from './problems/SetAsideSheet'
import { devicePath } from './problems/problemLinks'
import type { ProblemRow } from './problems/types'
import './problems/problems.css'

// Opened on demand; keeps the clean-up tool out of the Problems chunk.
const HealthCleanupDialog = lazy(() => import('./HealthCleanupDialog').then((module) => ({ default: module.HealthCleanupDialog })))

const ID_PREFIX = 'problems'
const MANY_GROUPS = 8

interface DeviceGroup {
  id: string
  row: ProblemRow
  rows: ProblemRow[]
  worst: string
  since: string
}

/** One group per device: worst severity first, then the earliest start, then the name. */
export function groupByDevice(rows: ProblemRow[]): DeviceGroup[] {
  const groups = new Map<string, DeviceGroup>()
  for (const row of rows) {
    const id = row.device?.connector_id || `problem-${row.id}`
    const group = groups.get(id)
    if (!group) {
      groups.set(id, { id, row, rows: [row], worst: row.severity, since: row.first_seen_at })
      continue
    }
    group.rows.push(row)
    if (severityRank(row.severity) < severityRank(group.worst)) group.worst = row.severity
    if (Date.parse(row.first_seen_at) < Date.parse(group.since)) group.since = row.first_seen_at
  }
  const list = [...groups.values()]
  for (const group of list) {
    group.rows.sort((left, right) => severityRank(left.severity) - severityRank(right.severity) || Date.parse(right.last_seen_at) - Date.parse(left.last_seen_at))
  }
  return list.sort((left, right) =>
    severityRank(left.worst) - severityRank(right.worst)
    || Date.parse(left.since) - Date.parse(right.since)
    || deviceLabel(left.row.device).name.localeCompare(deviceLabel(right.row.device).name))
}

function DeviceGroupCard({ group, defaultOpen, children }: { group: DeviceGroup; defaultOpen: boolean; children: React.ReactNode }) {
  const location = useLocation()
  const [open, setOpen] = useState(defaultOpen)
  const device = deviceLabel(group.row.device)
  const bodyId = `problem-group-${group.id}`
  const connectorId = group.row.device?.connector_id
  return (
    <section className="problem-group" aria-label={device.name}>
      <div className="problem-group-header">
        <button
          type="button"
          className="problem-group-toggle"
          aria-expanded={open}
          aria-controls={bodyId}
          aria-label={open ? PROBLEM_GROUP.collapse(device.name) : PROBLEM_GROUP.expand(device.name)}
          onClick={() => setOpen((value) => !value)}
        >
          <span className="problem-group-name">
            <DivisionTag division={device.division} />
            <strong data-device-code={device.code}>{device.name}</strong>
          </span>
          <span className="problem-group-meta">
            <StatusChip tone={severityTone(group.worst)} label={severityLabel(group.worst)} labelPrefix={PROBLEM_CARD.severityPrefix} icon="alert" size="sm" />
            <span>{plural(group.rows.length, 'problem')}</span>
            <span>{PROBLEM_GROUP.since(dayTime(group.since), duration(group.since))}</span>
          </span>
          <Icon name="chevronDown" size="sm" className="problem-group-chevron" />
        </button>
        {connectorId && (
          <Link className="button secondary problem-open-device" to={devicePath(connectorId)} state={{ from: location }}>
            {PROBLEM_GROUP.openDevice}<Icon name="chevron" size="sm" />
          </Link>
        )}
      </div>
      <div id={bodyId} className="problem-group-body" data-open={open ? '' : undefined} hidden={!open}>
        <div className="problem-group-inner">{children}</div>
      </div>
    </section>
  )
}

const dayHeading = (key: string, sample: string, now: number) => {
  if (key === pakistanDay(now)) return `${PROBLEM_GROUP.today} · ${day(sample, now)}`
  if (key === pakistanDay(now - 86_400_000)) return `${PROBLEM_GROUP.yesterday} · ${day(sample, now)}`
  return day(sample, now)
}

/** Fixed rows grouped by the Pakistan day they were fixed, newest first. */
export function groupByDay(rows: ProblemRow[], now = Date.now()) {
  const sorted = [...rows].sort((left, right) => Date.parse(right.resolved_at || right.last_seen_at) - Date.parse(left.resolved_at || left.last_seen_at) || right.id - left.id)
  const days: Array<{ key: string; label: string; rows: ProblemRow[] }> = []
  for (const row of sorted) {
    const at = row.resolved_at || row.last_seen_at
    const key = pakistanDay(at)
    const last = days[days.length - 1]
    if (last && last.key === key) last.rows.push(row)
    else days.push({ key, label: dayHeading(key, at, now), rows: [row] })
  }
  return days
}

const fixedLineFor = (when: EffectiveWhen, from: string, to: string) => {
  if (when === 'custom') return PROBLEMS_PAGE.fixedLine.custom(from ? day(`${from}T12:00:00+05:00`) : '', to ? day(`${to}T12:00:00+05:00`) : '')
  return PROBLEMS_PAGE.fixedLine[when]
}

function useTick(ms: number) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), ms)
    return () => window.clearInterval(timer)
  }, [ms])
  return now
}

export interface ProblemsViewProps {
  devices: Device[]
  revision: number
  deviceRevision?: number
  username?: string
}

export function ProblemsView({ devices, revision, deviceRevision = 0, username }: ProblemsViewProps) {
  const phone = useIsPhone()
  const toast = useToast()
  const [filters, setFilters] = useUrlState(PROBLEM_URL_SCHEMA)
  const [division, setDivision] = useDivision()
  const [prefs, setPrefs] = usePrefs()
  const now = useTick(15_000)
  const dayKey = pakistanDay(now)
  const params = useMemo(() => toApiParams(filters, division), [filters, division, dayKey]) // eslint-disable-line react-hooks/exhaustive-deps
  const data = useProblems(params, { tab: filters.tab, revision, deviceRevision })
  const [fixing, setFixing] = useState<ProblemRow | null>(null)
  const [noting, setNoting] = useState<ProblemRow | null>(null)
  const [noteBusy, setNoteBusy] = useState(false)
  const [setAside, setSetAside] = useState<{ deviceId: string | null; place: string | null; division: 'life' | 'health' | null } | null>(null)
  const [cleanupOpen, setCleanupOpen] = useState(false)
  const [setAsideTotal, setSetAsideTotal] = useState<number | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const tabsRef = useRef<HTMLDivElement>(null)
  const location = useLocation()

  // The permanent "Set-aside check-ins" link (finalizer): one cheap count.
  useEffect(() => {
    const controller = new AbortController()
    void api<AttendanceQuarantineResponse>(`/api/v1/attendance-quarantine${queryString({ review_state: 'OPEN', limit: 1 })}`, { signal: controller.signal })
      .then((response) => setSetAsideTotal(response?.totals?.open ?? null))
      .catch(() => undefined)
    return () => controller.abort()
  }, [revision])

  // A filter change scrolls the list back to its top (only when it is above the screen).
  const paramsKey = JSON.stringify(params)
  const firstKey = useRef(paramsKey)
  useEffect(() => {
    if (firstKey.current === paramsKey) return
    firstKey.current = paramsKey
    const list = listRef.current
    if (list && list.getBoundingClientRect().top < 0 && typeof list.scrollIntoView === 'function') list.scrollIntoView({ block: 'start' })
  }, [paramsKey])

  const set = useCallback((change: Partial<ProblemFilters>) => setFilters(change), [setFilters])
  const toggleSerious = (value: Serious) => set({ serious: filters.serious.includes(value) ? filters.serious.filter((item) => item !== value) : [...filters.serious, value] })

  const counts = data.counts
  const tab = filters.tab
  const when = effectiveWhen(filters)
  const filtered = hasFilters(filters)
  const ready = data.status === 'ready'
  const pickable = useMemo(() => devices.filter((device) => !device.is_spare).map((device) => {
    const label = deviceLabel({ ...device, division: device.division ?? null })
    return { id: device.connector_id, name: label.name, division: label.division }
  }).sort((left, right) => left.name.localeCompare(right.name)), [devices])
  const pickedDevice = pickable.find((device) => device.id === filters.device)

  const tabs = [
    { id: 'new' as const, label: PROBLEMS_PAGE.tabs.new, count: counts ? counts.open : null },
    { id: 'seen' as const, label: PROBLEMS_PAGE.tabs.seen, count: counts ? counts.seen : null },
    { id: 'fixed' as const, label: PROBLEMS_PAGE.tabs.fixed, count: counts ? counts.fixed : null },
  ]

  const view = useCallback((next: ProblemTab) => set({ tab: next, old: false }), [set])

  const focusNext = (id: number) => {
    window.setTimeout(() => {
      const cards = Array.from(document.querySelectorAll<HTMLElement>('.problem-card[data-problem-id]'))
      const index = cards.findIndex((card) => card.dataset.problemId === String(id))
      const next = cards.slice(index + 1).find((card) => card.dataset.problemId !== String(id)) || cards.slice(0, Math.max(0, index)).reverse()[0]
      if (next) next.focus()
      else tabsRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.focus()
    }, 60)
  }

  const markSeen = async (row: ProblemRow, note?: string) => {
    const result = await data.markSeen(row, note)
    if (result === 'seen') {
      toast.show({ tone: 'success', text: MARK_SEEN.toast, action: { label: MARK_SEEN.view, onClick: () => view('seen') } })
    }
    return result
  }

  const fixedDone = (row: ProblemRow, response: AlertResolveResponse) => {
    data.fixedDone(row, response)
    focusNext(row.id)
  }

  const openSetAside = (row: ProblemRow) => {
    const label = deviceLabel(row.device)
    setSetAside({ deviceId: row.device?.connector_id || null, place: label.name || null, division: row.device ? label.division : null })
  }

  // Active filter chips with ×.
  const active: ActiveFilter[] = [
    ...filters.serious.map((value) => ({ id: `serious-${value}`, label: PROBLEMS_PAGE.serious[value], onRemove: () => set({ serious: filters.serious.filter((item) => item !== value) }) })),
    ...filters.type.map((value) => ({ id: `type-${value}`, label: PROBLEMS_PAGE.filterChip.type(PROBLEM_TYPE_LABELS[value]), onRemove: () => set({ type: filters.type.filter((item) => item !== value) }) })),
    ...(filters.device ? [{ id: 'device', label: PROBLEMS_PAGE.filterChip.device(pickedDevice?.name || PROBLEMS_PAGE.device), onRemove: () => set({ device: '' }) }] : []),
    ...(filters.when ? [{ id: 'when', label: filters.when === 'custom' ? fixedLineFor('custom', filters.from, filters.to).replace(/^Closed /, '').replace(/\.$/, '') : PROBLEMS_PAGE.dates[filters.when], onRemove: () => set({ when: '', from: '', to: '' }) }] : []),
    ...(filters.q.trim() ? [{ id: 'q', label: PROBLEMS_PAGE.filterChip.search(filters.q.trim()), onRemove: () => set({ q: '' }) }] : []),
    ...(filters.att ? [{ id: 'att', label: PROBLEMS_PAGE.affectsAttendance, onRemove: () => set({ att: false }) }] : []),
    ...(filters.old && tab === 'seen' ? [{ id: 'old', label: PROBLEMS_PAGE.filterChip.oldMarks, onRemove: () => set({ old: false }) }] : []),
  ]
  const moreCount = filters.type.length + (filters.device ? 1 : 0) + (filters.when ? 1 : 0) + (phone && filters.sort !== 'serious' ? 1 : 0)

  const bySeverity = counts?.by_severity
  const chips = [
    ...SERIOUS.map((value) => ({
      id: value,
      label: PROBLEMS_PAGE.serious[value],
      count: bySeverity ? (value === 'low' ? (bySeverity.WARNING ?? 0) + (bySeverity.MEDIUM ?? 0) + (bySeverity.INFO ?? 0) : value === 'urgent' ? bySeverity.CRITICAL ?? 0 : bySeverity.HIGH ?? 0) : null,
    })),
    { id: 'att', label: PROBLEMS_PAGE.affectsAttendance },
  ]
  const chipValue = [...filters.serious, ...(filters.att ? ['att'] : [])]

  const moreFilters = (
    <div className="problem-filters">
      <fieldset className="problem-filter-group">
        <legend>{PROBLEMS_PAGE.type}</legend>
        {PROBLEM_TYPES.map((value) => (
          <label key={value} className="problem-check">
            <input type="checkbox" checked={filters.type.includes(value)} onChange={(event) => set({ type: event.target.checked ? [...filters.type, value] : filters.type.filter((item) => item !== value) })} />
            <span>{PROBLEM_TYPE_LABELS[value]}</span>
            {typeof counts?.by_type?.[value] === 'number' && <span className="problem-check-count">{formatCount(counts.by_type[value] ?? 0)}</span>}
          </label>
        ))}
      </fieldset>
      <label className="problem-filter-field">
        <span>{PROBLEMS_PAGE.device}</span>
        <select value={filters.device} onChange={(event) => set({ device: event.target.value })}>
          <option value="">{PROBLEMS_PAGE.allDevices}</option>
          {(['life', 'health'] as const).map((key) => {
            const members = pickable.filter((device) => device.division === key)
            if (!members.length) return null
            return (
              <optgroup key={key} label={`${divisionLabel[key]} (${members.length})`}>
                {members.map((device) => <option key={device.id} value={device.id}>{device.name}</option>)}
              </optgroup>
            )
          })}
        </select>
      </label>
      <label className="problem-filter-field">
        <span>{PROBLEMS_PAGE.date}</span>
        <select value={when} onChange={(event) => {
          const next = event.target.value as EffectiveWhen
          const fallback = tab === 'fixed' ? '7d' : 'any'
          set({ when: next === fallback ? '' : next, ...(next === 'custom' ? {} : { from: '', to: '' }) })
        }}>
          {(['any', 'today', 'yesterday', '7d', '30d', 'custom'] as const).map((value) => <option key={value} value={value}>{PROBLEMS_PAGE.dates[value]}</option>)}
        </select>
      </label>
      {when === 'custom' && (
        <div className="problem-filter-dates">
          <label className="problem-filter-field"><span>{PROBLEMS_PAGE.from}</span><input type="date" value={filters.from} max={filters.to || undefined} onChange={(event) => set({ from: event.target.value })} /></label>
          <label className="problem-filter-field"><span>{PROBLEMS_PAGE.to}</span><input type="date" value={filters.to} min={filters.from || undefined} onChange={(event) => set({ to: event.target.value })} /></label>
        </div>
      )}
      {phone && tab !== 'fixed' && (
        <label className="problem-filter-field">
          <span>{PROBLEMS_PAGE.sort}</span>
          <select value={filters.sort} onChange={(event) => set({ sort: event.target.value as ProblemFilters['sort'] })}>
            {SORTS.map((value) => <option key={value} value={value}>{PROBLEMS_PAGE.sorts[value]}</option>)}
          </select>
        </label>
      )}
      <p className="problem-filter-note">{PROBLEMS_PAGE.sparesNote}</p>
    </div>
  )

  const menu: ActionMenuItem[] = [
    { id: 'cleanup', label: PROBLEMS_PAGE.cleanUp, icon: 'tool', onSelect: () => setCleanupOpen(true) },
    { id: 'technical', label: prefs.technical ? PROBLEMS_PAGE.hideTechnical : PROBLEMS_PAGE.showTechnical, icon: prefs.technical ? 'eyeOff' : 'eye', onSelect: () => setPrefs({ technical: !prefs.technical }) },
  ]

  const refreshLine = (
    <div className="problems-refresh">
      {data.updatedAt && <span className="problems-updated">{PROBLEMS_PAGE.updated(ago(data.updatedAt, now))}</span>}
      <IconButton icon="refresh" label={PROBLEMS_PAGE.refresh} busy={data.refreshing} onClick={() => void data.refresh()} />
    </div>
  )

  return (
    <div className="problems-page">
      <PageHeader
        title={PROBLEMS_PAGE.title}
        description={PROBLEMS_PAGE.description}
        primaryAction={phone ? undefined : refreshLine}
        moreActions={phone ? undefined : menu}
      />
      <div className="problems-tabs" ref={tabsRef}>
        <Tabs tabs={tabs} value={tab} onChange={view} label={PROBLEMS_PAGE.tabsLabel} idPrefix={ID_PREFIX} className="problems-tablist" />
        {!counts && data.status === 'loading' && <span className="sr-only">{PROBLEMS_PAGE.loading}</span>}
      </div>
      <TabPanel idPrefix={ID_PREFIX} id={tab} className="problems-panel">
        <div className="problems-subline">
          <p className="problems-tab-line">{tab === 'fixed' ? fixedLineFor(when, filters.from, filters.to) : filters.old ? PROBLEMS_PAGE.oldMarks.viewing : PROBLEMS_PAGE.tabLine[tab]}</p>
          {phone && <div className="problems-phone-tools">{refreshLine}<span className="problems-phone-menu"><ActionMenu items={menu} /></span></div>}
        </div>
        {Boolean(setAsideTotal) && (
          <button type="button" className="text-button problems-set-aside-link" onClick={() => setSetAside({ deviceId: null, place: null, division: null })}>
            {PROBLEMS_PAGE.setAsideLink(formatCount(setAsideTotal ?? 0))}<Icon name="chevron" size="sm" />
          </button>
        )}
        <section aria-label={PROBLEMS_PAGE.filtersLabel}>
          <FilterBar
            className="problems-filter-bar"
            search={{ value: filters.q, onChange: (value) => set({ q: value }), label: PROBLEMS_PAGE.search, placeholder: PROBLEMS_PAGE.search }}
            chips={chips}
            chipValue={chipValue}
            chipsLabel={PROBLEMS_PAGE.seriousLabel}
            onChipChange={(id) => (id === 'att' ? set({ att: !filters.att }) : toggleSerious(id as Serious))}
            moreFilters={moreFilters}
            moreFiltersCount={moreCount}
            active={active}
            onClearAll={() => set(CLEARED)}
            sort={!phone && tab !== 'fixed' ? { value: filters.sort, options: SORTS.map((id) => ({ id, label: PROBLEMS_PAGE.sorts[id] })), onChange: (value) => set({ sort: value as ProblemFilters['sort'] }) } : undefined}
          />
        </section>
        {ready && counts && <ResultLine data={data} tab={tab} old={filters.old} division={division} onDivision={setDivision} />}
        {Boolean(data.refreshError) && data.rows.length > 0 && (
          <div className="problems-stale" role="status">
            <span>{PROBLEMS_PAGE.stale(clock(data.updatedAt || new Date()))}</span>
            <Button variant="link" busyLabel={PROBLEMS_PAGE.trying} onClick={() => data.refresh()}>{PROBLEMS_PAGE.tryAgain}</Button>
          </div>
        )}
        <div
          ref={listRef}
          className="problems-list"
          aria-busy={data.switching || data.status === 'loading' || undefined}
          data-switching={data.switching ? '' : undefined}
        >
          {data.switching && <div className="problems-progress" aria-hidden="true" />}
          <ListBody
            data={data}
            filters={filters}
            filtered={filtered}
            division={division}
            username={username}
            linkSearch={location.search}
            onMarkSeen={(row) => void markSeen(row)}
            onNote={(row) => setNoting(row)}
            onFix={(row) => setFixing(row)}
            onSetAside={openSetAside}
            onClear={() => set(CLEARED)}
            onTab={view}
            onWhen={(value) => set({ when: value })}
            onShowOld={(show) => set({ old: show })}
            onCleanup={() => setCleanupOpen(true)}
            now={now}
          />
        </div>
        <LiveAnnouncer rows={data.arrived} />
      </TabPanel>

      {fixing && (
        <MarkFixedDialog
          alert={fixing}
          onClose={() => setFixing(null)}
          onResolved={(response) => fixedDone(fixing, response)}
          onGone={() => data.fixedDone(fixing, null)}
          onRefused={data.refreshSoon}
          onView={() => view('fixed')}
        />
      )}
      {noting && (
        <MarkSeenNote
          row={noting}
          place={deviceLabel(noting.device).name}
          busy={noteBusy}
          onClose={() => setNoting(null)}
          onSubmit={async (note) => {
            setNoteBusy(true)
            const result = await markSeen(noting, note)
            setNoteBusy(false)
            if (result !== 'failed') setNoting(null)
          }}
        />
      )}
      {setAside && (
        <SetAsideSheet
          deviceId={setAside.deviceId}
          place={setAside.place}
          division={setAside.division}
          onClose={() => setSetAside(null)}
          onChanged={() => data.refresh()}
        />
      )}
      {cleanupOpen && (
        <Suspense fallback={null}>
          <HealthCleanupDialog devices={devices} onClose={() => setCleanupOpen(false)} onApplied={() => data.refresh()} />
        </Suspense>
      )}
    </div>
  )
}

function ResultLine({ data, tab, old, division, onDivision }: {
  data: ProblemsData
  tab: ProblemTab
  old: boolean
  division: 'all' | 'life' | 'health'
  onDivision: (next: 'all' | 'life' | 'health') => void
}) {
  const counts = data.counts
  if (!counts) return null
  const devices = new Set(data.rows.filter((row) => !problemIsFixed(row) || tab === 'fixed').map((row) => row.device?.connector_id).filter(Boolean)).size
  const key = tab === 'new' ? 'open' : tab === 'seen' ? 'seen' : 'fixed'
  const total = tab === 'seen' && old ? counts.legacy_seen : counts[key]
  const head = tab === 'fixed'
    ? `${formatCount(total)} fixed`
    : tab === 'seen' && old
      ? plural(total, 'old “seen” mark')
      : `${plural(total, tab === 'new' ? 'new problem' : 'seen problem')} at ${plural(devices, 'device')}`
  const life = counts.by_division?.life?.[key]
  const health = counts.by_division?.health?.[key]
  return (
    <p className="problems-result" aria-live="polite">
      <span>{head}</span>
      {division === 'all' && !old && typeof life === 'number' && typeof health === 'number' && (
        <>
          <span aria-hidden="true"> · </span>
          <button type="button" className="text-button" onClick={() => onDivision('life')}>{divisionLabel.life} {formatCount(life)}</button>
          <span aria-hidden="true"> · </span>
          <button type="button" className="text-button" onClick={() => onDivision('health')}>{divisionLabel.health} {formatCount(health)}</button>
        </>
      )}
    </p>
  )
}

function LiveAnnouncer({ rows }: { rows: ProblemRow[] }) {
  const last = rows[rows.length - 1]
  const text = last && !problemIsFixed(last) ? PROBLEMS_PAGE.announce(deviceLabel(last.device).name, last.plain_title || '') : ''
  return <p className="sr-only" aria-live="polite">{text}</p>
}

function ListBody({
  data, filters, filtered, division, username, linkSearch, onMarkSeen, onNote, onFix, onSetAside, onClear, onTab, onWhen, onShowOld, onCleanup, now,
}: {
  data: ProblemsData
  filters: ProblemFilters
  filtered: boolean
  division: 'all' | 'life' | 'health'
  username?: string
  linkSearch: string
  onMarkSeen: (row: ProblemRow) => void
  onNote: (row: ProblemRow) => void
  onFix: (row: ProblemRow) => void
  onSetAside: (row: ProblemRow) => void
  onClear: () => void
  onTab: (tab: ProblemTab) => void
  onWhen: (value: ProblemFilters['when']) => void
  onShowOld: (show: boolean) => void
  onCleanup: () => void
  now: number
}) {
  const tab = filters.tab
  const counts = data.counts
  const groups = useMemo(() => groupByDevice(data.rows), [data.rows])

  if (data.status === 'loading' && !data.rows.length) {
    return (
      <div className="problems-skeleton" role="status">
        <span className="sr-only">{PROBLEMS_PAGE.loading}</span>
        {[0, 1, 2].map((index) => (
          <div key={index} className="problem-group is-skeleton" aria-hidden="true">
            <Skeleton variant="line" width="45%" />
            <Skeleton variant="block" count={2} />
          </div>
        ))}
      </div>
    )
  }
  if (data.status === 'error' && !data.rows.length) {
    return <ErrorState title={PROBLEMS_PAGE.loadError} message={PROBLEMS_PAGE.loadErrorHint} error={data.error} onRetry={() => data.refresh()} headingLevel={2} />
  }

  const card = (row: ProblemRow, inGroup: boolean) => (
    <ProblemCard
      key={row.id}
      row={row}
      headingLevel={inGroup ? 3 : 2}
      showDevice={!inGroup}
      showOpenDevice={!inGroup}
      phase={data.phases[row.id]}
      busy={data.busy[row.id]}
      rowError={data.rowErrors[row.id]}
      username={username}
      onMarkSeen={onMarkSeen}
      onMarkSeenWithNote={onNote}
      onMarkFixed={onFix}
      onSetAside={onSetAside}
      linkSearch={linkSearch}
      now={now}
    />
  )

  const oldNote = tab !== 'fixed' && !filters.old && counts && counts.legacy_seen > 0 && (tab === 'seen' || !data.rows.length) ? (
    <Callout tone="info" icon={null} className="problems-old-marks" title={PROBLEMS_PAGE.oldMarks.title(plural(counts.legacy_seen, 'old “seen” mark'), counts.legacy_seen === 1)}
      action={(
        <span className="problems-old-actions">
          <Button variant="secondary" size="sm" onClick={() => { onTab('seen'); onShowOld(true) }}>{PROBLEMS_PAGE.oldMarks.show}</Button>
          <Button variant="secondary" size="sm" onClick={onCleanup}>{PROBLEMS_PAGE.oldMarks.cleanUp}</Button>
        </span>
      )}
    >
      {PROBLEMS_PAGE.oldMarks.text}
    </Callout>
  ) : null

  if (!data.rows.length) {
    let empty: React.ReactNode
    if (filtered) {
      empty = <EmptyState icon="search" title={PROBLEMS_PAGE.empty.filtered} action={<Button variant="secondary" onClick={onClear}>{PROBLEMS_PAGE.empty.clearFilters}</Button>} headingLevel={2}>{PROBLEMS_PAGE.empty.filteredHint}</EmptyState>
    } else if (tab === 'fixed') {
      const week = effectiveWhen(filters) === '7d'
      empty = <EmptyState icon="check" title={week ? PROBLEMS_PAGE.empty.fixed7d : PROBLEMS_PAGE.empty.fixed} action={week ? <Button variant="secondary" onClick={() => onWhen('30d')}>{PROBLEMS_PAGE.empty.show30d}</Button> : undefined} headingLevel={2} />
    } else if (tab === 'seen') {
      empty = <EmptyState icon="check" title={filters.old ? PROBLEMS_PAGE.empty.oldMarks : PROBLEMS_PAGE.empty.seen} headingLevel={2} />
    } else if (division !== 'all') {
      empty = <EmptyState icon="check" title={PROBLEMS_PAGE.empty.division(divisionLabel[division])} headingLevel={2} />
    } else if (counts && counts.seen > 0) {
      empty = <EmptyState icon="check" title={PROBLEMS_PAGE.empty.nothingNew} action={<Button variant="secondary" onClick={() => onTab('seen')}>{PROBLEMS_PAGE.empty.goToSeen}</Button>} headingLevel={2}>{PROBLEMS_PAGE.empty.allSeen(formatCount(counts.seen), counts.seen === 1)}</EmptyState>
    } else {
      empty = <EmptyState icon="check" title={PROBLEMS_PAGE.empty.allClear} headingLevel={2}>{PROBLEMS_PAGE.empty.allClearHint}</EmptyState>
    }
    return <>{empty}{oldNote}</>
  }

  let body: React.ReactNode
  if (tab === 'fixed') {
    body = groupByDay(data.rows, now).map((group) => (
      <section key={group.key} className="problem-day" aria-label={group.label}>
        <h2 className="problem-day-title">{group.label}</h2>
        <div className="problem-day-rows">{group.rows.map((row) => card(row, false))}</div>
      </section>
    ))
  } else if (filters.sort !== 'serious') {
    const sorted = [...data.rows].sort((left, right) => {
      const diff = Date.parse(left.first_seen_at) - Date.parse(right.first_seen_at)
      return filters.sort === 'oldest' ? diff || left.id - right.id : -diff || right.id - left.id
    })
    body = <div className="problem-flat">{sorted.map((row) => card(row, false))}</div>
  } else {
    const many = groups.length > MANY_GROUPS
    body = groups.map((group) => (
      <DeviceGroupCard key={group.id} group={group} defaultOpen={!many || seriousOf(group.worst) !== 'low'}>
        {group.rows.map((row) => card(row, true))}
      </DeviceGroupCard>
    ))
  }

  // Every tab pages the same way: New and Seen hold up to 500 a page, Fixed 50.
  const paged = tab === 'fixed' || Boolean(data.nextCursor) || data.rows.length > pageSize(tab)
  const total = counts ? (tab === 'new' ? counts.open : tab === 'seen' ? (filters.old ? counts.legacy_seen : counts.seen) : counts.fixed) : data.rows.length
  return (
    <>
      {body}
      {paged && (
        <div className="problems-more">
          {data.nextCursor && <Button variant="secondary" busy={data.loadingMore} busyLabel={PROBLEMS_PAGE.loadingMore} onClick={() => data.loadMore()}>{PROBLEMS_PAGE.showMore}</Button>}
          <small>{PROBLEMS_PAGE.showing(formatCount(data.rows.length), formatCount(Math.max(total, data.rows.length)))}</small>
        </div>
      )}
      {oldNote}
    </>
  )
}

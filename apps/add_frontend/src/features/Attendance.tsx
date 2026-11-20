// Attendance (06-attendance): Check-ins (today's totals, filters in the URL,
// one status per check-in, the check-in sheet) and Fix tools. Live check-ins
// slide in at the top, or wait behind an "N new check-ins" pill while the
// reader is scrolled down. Every safeguard inside the tools is unchanged.
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import './Attendance.css'
import { api } from '../api'
import { Icon } from '../Icon'
import { ActionMenu, PageHeader, TabPanel, Tabs, useIsPhone } from '../ui'
import { useThrottledRunner } from '../lib/throttle'
import { useDivision } from '../lib/useDivision'
import type { DivisionChoice } from '../lib/prefs'
import type { RealtimeState } from '../realtime'
import type { AttendanceEvent, CheckinNotificationOverview, Device, Overview } from '../types'
import { checkinNotifyBanner } from './CheckinNotification'
import { DirectOrdsSend } from './DirectOrdsSend'
import { QuickReleaseOutcome, UnknownSyncedPlaces, useQuickRelease } from './UnknownSyncedRelease'
import { useUnknownAutoReleaseStatus } from './unknownAutoRelease'
import { AttendanceFilters } from './attendance/AttendanceFilters'
import { AttendanceListSkeleton, AttendanceRowItem, canSelect } from './attendance/AttendanceList'
import { AttendanceSheet } from './attendance/AttendanceSheet'
import { AttendanceToday, updatedLine } from './attendance/AttendanceToday'
import type { HistoryKind } from './attendance/AttendanceHistory'
import { checkins, rowGroup } from './attendance/attendanceWords'
import { msUntilNextPktMidnight, pktPresetRange, todayKey } from '../lib/pktDates'
import { useAttendanceSummary } from './attendance/useAttendanceSummary'
import {
  CLEARED, listParams, MAX_ROWS, narrowed, summaryParams, useAttendanceFilters, type FilterValues,
} from './attendance/useAttendanceFilters'
import type { AttendanceAllTime, AttendanceListResponse, AttendanceRow } from './attendance/types'

type Toast = { notice: (message: string) => void; error: (message: string) => void }

// Fix tools load only when the tab opens (keeps the Check-ins page within its size budget).
const FixTools = lazy(() => import('./attendance/FixTools').then((module) => ({ default: module.FixTools })))

const LIVE_REFRESH_MS = 3_000
const FILTER_DELAY_MS = 300
const NEW_TAG_MS = 10_000
const CHANGED_MS = 1_200

const CONNECTION_WORDS: Record<RealtimeState, string> = {
  connecting: 'Connecting…', live: 'Live', reconnecting: 'Reconnecting…', stale: 'Offline · showing last saved data',
}

const mergeByEventUid = (first: AttendanceRow[], second: AttendanceRow[]) => {
  const rows = new Map<string, AttendanceRow>()
  ;[...first, ...second].forEach((row) => {
    if (!rows.has(row.event_uid)) rows.set(row.event_uid, row)
  })
  return [...rows.values()]
}

const reducedMotion = () => typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
const safeRows = (value: Partial<AttendanceListResponse> | null | undefined): AttendanceRow[] =>
  Array.isArray(value?.rows) ? value.rows.filter((row) => row && typeof row.id === 'number') : []

function CheckinsTab({
  devices, revision, realtimeState, toast, division, filters, setFilters, search, setSearch, summaryTotal,
  frozen, setFrozen, selectMode, setSelectMode, notifyState, onOpenSettings, openEvent, closeEvent,
}: {
  devices: Device[]
  revision: number
  realtimeState: RealtimeState
  toast: Toast
  division: DivisionChoice
  filters: FilterValues
  setFilters: (change: Partial<FilterValues>, options?: { history?: 'push' | 'replace' }) => void
  search: string
  setSearch: (value: string) => void
  summaryTotal: number | null
  frozen: boolean
  setFrozen: (value: boolean) => void
  selectMode: boolean
  setSelectMode: (value: boolean) => void
  notifyState: Pick<CheckinNotificationOverview, 'state'> | null | undefined
  onOpenSettings: () => void
  openEvent: (row: AttendanceRow, trigger: HTMLElement) => void
  closeEvent: () => void
}) {
  const [rows, setRows] = useState<AttendanceRow[]>([])
  const rowsRef = useRef(rows)
  rowsRef.current = rows
  const phone = useIsPhone()
  const [pendingRows, setPendingRows] = useState<AttendanceRow[]>([])
  const [nextCursor, setNextCursor] = useState<number | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [upToDate, setUpToDate] = useState('')
  const [error, setError] = useState('')
  const [announcement, setAnnouncement] = useState('')
  const [selected, setSelected] = useState<Map<number, AttendanceRow>>(new Map())
  const [singleSendRequest, setSingleSendRequest] = useState(0)
  const [topVisible, setTopVisible] = useState(true)
  const [newKeys, setNewKeys] = useState<Set<string>>(new Set())
  const [changedKeys, setChangedKeys] = useState<Set<string>>(new Set())
  const [now, setNow] = useState(() => Date.now())
  const frozenRef = useRef(frozen)
  frozenRef.current = frozen
  const latestRequest = useRef<AbortController | null>(null)
  const pageRequest = useRef<AbortController | null>(null)
  const topSentinel = useRef<HTMLDivElement>(null)
  const initialRevision = useRef(revision)
  const sheetTrigger = useRef<HTMLElement | null>(null)
  const autoRelease = useUnknownAutoReleaseStatus(true)
  const autoSending = autoRelease.data ? autoRelease.data.enabled && autoRelease.data.allowed : null

  const [dayKey, setDayKey] = useState(() => todayKey())
  // "Today" moves at Pakistan midnight: the list is replaced with the new day.
  useEffect(() => {
    const timer = window.setTimeout(() => setDayKey(todayKey()), msUntilNextPktMidnight())
    return () => window.clearTimeout(timer)
  }, [dayKey])
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [])

  const query = useMemo(() => listParams(filters, division, search).toString(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [filters.when, filters.from, filters.to, filters.device.join(), filters.status.join(), filters.type, filters.by_admin, filters.phone.join(), filters.time, filters.arrived, filters.no_id, filters.machine_match, division, search, dayKey])
  const queryRef = useRef(query)
  queryRef.current = query

  const markNew = useCallback((keys: string[]) => {
    if (!keys.length) return
    setNewKeys((current) => new Set([...current, ...keys]))
    window.setTimeout(() => setNewKeys((current) => {
      const next = new Set(current)
      keys.forEach((key) => next.delete(key))
      return next
    }), NEW_TAG_MS)
  }, [])
  const markChanged = useCallback((keys: string[]) => {
    if (!keys.length) return
    setChangedKeys((current) => new Set([...current, ...keys]))
    window.setTimeout(() => setChangedKeys((current) => {
      const next = new Set(current)
      keys.forEach((key) => next.delete(key))
      return next
    }), CHANGED_MS)
  }, [])

  const replaceFeed = useCallback(async () => {
    latestRequest.current?.abort()
    pageRequest.current?.abort()
    const controller = new AbortController()
    latestRequest.current = controller
    setLoading(true)
    setError('')
    setPendingRows([])
    try {
      const response = await api<AttendanceListResponse>(`/api/v1/attendance?${queryRef.current}`, { signal: controller.signal })
      if (controller.signal.aborted || latestRequest.current !== controller) return
      const fresh = safeRows(response)
      setRows(fresh)
      setNextCursor(response.next_cursor ?? null)
      setLoaded(true)
      setAnnouncement(`Showing ${checkins(fresh.length)}.`)
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setError("Couldn't load check-ins. Check your connection, then try again.")
    } finally {
      if (latestRequest.current === controller) setLoading(false)
    }
  }, [])

  // `user`: the person pressed Refresh (the only time the button is busy).
  const refreshFeed = useCallback(async (reveal = false, user = false) => {
    latestRequest.current?.abort()
    const controller = new AbortController()
    latestRequest.current = controller
    if (user) setRefreshing(true)
    try {
      const response = await api<AttendanceListResponse>(`/api/v1/attendance?${queryRef.current}`, { signal: controller.signal })
      if (controller.signal.aborted || latestRequest.current !== controller) return
      const fresh = safeRows(response)
      const current = rowsRef.current
      const currentById = new Map(current.map((row) => [row.event_uid, row]))
      const unseen = fresh.filter((row) => !currentById.has(row.event_uid))
      const newestById = new Map(fresh.map((row) => [row.event_uid, row]))
      const changed = fresh.filter((row) => {
        const before = currentById.get(row.event_uid)
        return before && rowGroup(before) !== rowGroup(row)
      })
      const refreshedCurrent = current.map((row) => newestById.get(row.event_uid) || row)
      setError('')
      if (reveal || topVisible) {
        setRows(mergeByEventUid(unseen, refreshedCurrent).slice(0, MAX_ROWS))
        setPendingRows([])
        markNew(unseen.map((row) => row.event_uid))
        setAnnouncement(unseen.length ? `${checkins(unseen.length).replace('check-in', 'new check-in')}.` : changed.length ? `${checkins(changed.length)} changed.` : 'Up to date.')
      } else {
        setRows(refreshedCurrent)
        setPendingRows((queued) => mergeByEventUid(unseen, queued))
        if (unseen.length) setAnnouncement(`${checkins(unseen.length).replace('check-in', 'new check-in')} above.`)
      }
      markChanged(changed.map((row) => row.event_uid))
      if (user) {
        setUpToDate(unseen.length ? checkins(unseen.length).replace('check-in', 'new check-in') : 'Up to date')
        window.setTimeout(() => setUpToDate(''), 2_000)
      }
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setError("Couldn't update. Showing the last saved list.")
    } finally {
      if (latestRequest.current === controller && user) setRefreshing(false)
    }
  }, [markChanged, markNew, topVisible])

  const loadOlder = async () => {
    if (!nextCursor) return
    pageRequest.current?.abort()
    const controller = new AbortController()
    pageRequest.current = controller
    setLoadingMore(true)
    setError('')
    try {
      const response = await api<AttendanceListResponse>(`/api/v1/attendance?${queryRef.current}&cursor=${nextCursor}`, { signal: controller.signal })
      if (controller.signal.aborted || pageRequest.current !== controller) return
      const older = safeRows(response)
      setRows((current) => mergeByEventUid(current, older))
      setNextCursor(response.next_cursor ?? null)
      setAnnouncement(`Showing ${checkins(older.length).replace('check-in', 'older check-in')}.`)
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setError("Couldn't load older check-ins. Try again.")
    } finally {
      if (pageRequest.current === controller) setLoadingMore(false)
    }
  }

  // A filter change: wait a moment for more typing, then replace the list.
  useEffect(() => {
    const timeout = window.setTimeout(() => { if (!frozenRef.current) void replaceFeed() }, FILTER_DELAY_MS)
    return () => window.clearTimeout(timeout)
  }, [query, replaceFeed])

  useEffect(() => () => {
    latestRequest.current?.abort()
    pageRequest.current?.abort()
  }, [])

  // Live check-ins arrive in bursts: re-read the list at most every 3 s (ATT-1).
  const liveRefresh = useThrottledRunner(() => (frozenRef.current ? undefined : refreshFeed(false)), LIVE_REFRESH_MS)
  useEffect(() => {
    if (revision === initialRevision.current) return
    initialRevision.current = revision
    if (!frozen) liveRefresh.schedule()
  }, [frozen, liveRefresh, revision])

  useEffect(() => {
    if (!topSentinel.current || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(([entry]) => setTopVisible(entry.isIntersecting), { threshold: 0.7 })
    observer.observe(topSentinel.current)
    return () => observer.disconnect()
  }, [rows.length])

  const revealPending = () => {
    setRows((current) => mergeByEventUid(pendingRows, current).slice(0, MAX_ROWS))
    markNew(pendingRows.map((row) => row.event_uid))
    setAnnouncement(`${checkins(pendingRows.length).replace('check-in', 'new check-in')}.`)
    setPendingRows([])
    topSentinel.current?.scrollIntoView({ block: 'start', behavior: reducedMotion() ? 'auto' : 'smooth' })
  }

  const toggleFrozen = () => {
    if (!frozen) {
      frozenRef.current = true
      liveRefresh.cancel()
      latestRequest.current?.abort()
      pageRequest.current?.abort()
      setLoading(false)
      setRefreshing(false)
      setLoadingMore(false)
      setFrozen(true)
      setAnnouncement('Live updates paused.')
      toast.notice('Live updates paused')
    } else {
      frozenRef.current = false
      setFrozen(false)
      setAnnouncement('Live updates on.')
      toast.notice('Live updates on')
      void refreshFeed(false)
    }
  }

  const selectableRows = rows.filter(canSelect)
  const toggleSelection = useCallback((row: AttendanceRow) => setSelected((current) => {
    const next = new Map(current)
    if (next.has(row.id)) next.delete(row.id)
    else if (next.size < MAX_ROWS) next.set(row.id, row)
    return next
  }), [])
  const clearSelection = useCallback(() => setSelected(new Map()), [])
  const refreshDirectProgress = useCallback(() => { void refreshFeed(true) }, [refreshFeed])
  const quick = useQuickRelease({ clearSelection, refresh: refreshDirectProgress })
  useEffect(() => { if (!selectMode) setSelected(new Map()) }, [selectMode])

  const isNarrowed = narrowed(filters, search)
  const clearFilters = () => { setFilters(CLEARED); setSearch('') }
  const rangeToday = filters.when === 'today'
  const atCap = selected.size >= MAX_ROWS
  const banner = checkinNotifyBanner(notifyState)
  const connection = frozen ? 'List frozen' : CONNECTION_WORDS[realtimeState]
  const showSearchWider = isNarrowed && Boolean(search.trim()) && rangeToday
  const busyList = loading && loaded

  return (
    <div className="attendance-checkins" data-phone-first>
      {banner && <p className="attendance-banner" role="status"><Icon name="alert" /> {banner} <button type="button" className="text-button" onClick={onOpenSettings}>Open settings</button></p>}
      {filters.machine_match && <UnknownSyncedPlaces devices={devices} active />}

      <section className="attendance-feed" aria-labelledby="attendance-feed-title">
        <header className="attendance-feed-header">
          <div className="attendance-feed-title">
            <h2 id="attendance-feed-title">Check-ins</h2>
            <p className={`attendance-live-state ${frozen ? 'is-paused' : `is-${realtimeState}`}`}><i aria-hidden="true" /> Newest first · {connection}</p>
          </div>
          <div className="attendance-feed-actions">
            <button type="button" className={`button secondary ${selectMode ? 'is-active' : ''}`} aria-pressed={selectMode} onClick={() => setSelectMode(!selectMode)}>{selectMode ? 'Done' : 'Select'}</button>
            {phone
              ? <ActionMenu label="More list actions" items={[
                { id: 'freeze', label: frozen ? 'Unfreeze list' : 'Freeze list', onSelect: toggleFrozen },
                { id: 'refresh', label: refreshing ? 'Refreshing…' : 'Refresh', disabled: refreshing, onSelect: () => void refreshFeed(true, true) },
              ]} />
              : <>
                <button type="button" className="button secondary" onClick={toggleFrozen}><Icon name={frozen ? 'pulse' : 'pause'} /> {frozen ? 'Unfreeze list' : 'Freeze list'}</button>
                <button type="button" className="button secondary" disabled={refreshing} aria-busy={refreshing} onClick={() => void refreshFeed(true, true)}><Icon name="refresh" /> {refreshing ? 'Refreshing…' : upToDate || 'Refresh'}</button>
              </>}
          </div>
        </header>
        {frozen && <p className="attendance-frozen" role="status">List frozen · new check-ins show when you unfreeze. Sending to the HR system goes on as normal. <button type="button" className="text-button" onClick={toggleFrozen}>Unfreeze list</button></p>}

        <DirectOrdsSend selected={[...selected.values()] as unknown as AttendanceEvent[]} clearSelection={clearSelection} refresh={refreshDirectProgress} openRequest={singleSendRequest} onCopied={(text) => toast.notice(text)} />
        {quick.outcome && <QuickReleaseOutcome outcome={quick.outcome} />}
        {error && <div className="message pattern-blocked operational-error" role="alert"><Icon name="alert" /><span>{error}</span><button className="button secondary" type="button" onClick={() => void replaceFeed()}>Try again</button></div>}
        <div className="sr-only" aria-live="polite">{announcement}</div>
        <div ref={topSentinel} className="attendance-top-sentinel" aria-hidden="true" />
        {pendingRows.length > 0 && <button className="attendance-new-events" type="button" onClick={revealPending}><Icon name="pulse" /> {checkins(pendingRows.length).replace('check-in', 'new check-in')} · Show</button>}

        <div className={`attendance-list ${busyList ? 'is-busy' : ''} ${error && rows.length ? 'is-stale' : ''}`} aria-busy={loading || refreshing}>
          {busyList && <span className="attendance-progress" aria-hidden="true" />}
          {selectMode && selectableRows.length > 0 && (
            <label className="attendance-select-all">
              <input type="checkbox" checked={selectableRows.every((row) => selected.has(row.id))} disabled={atCap && !selectableRows.every((row) => selected.has(row.id))} onChange={(event) => setSelected((current) => {
                const next = new Map(current)
                selectableRows.forEach((row) => { if (event.target.checked && next.size < MAX_ROWS) next.set(row.id, row); else if (!event.target.checked) next.delete(row.id) })
                return next
              })} /> Select all shown
            </label>
          )}
          <div className="attendance-list-head" aria-hidden="true"><span>Person</span><span>Time</span><span>Place</span><span>Status</span></div>
          {loading && !loaded && <AttendanceListSkeleton />}
          {rows.length > 0 && <ul className="attendance-rows" aria-label="Check-ins">
            {rows.map((row) => (
              <AttendanceRowItem
                key={row.event_uid}
                row={row}
                now={now}
                autoSending={autoSending}
                selectMode={selectMode}
                selected={selected.has(row.id)}
                selectDisabled={!canSelect(row) || (atCap && !selected.has(row.id))}
                isNew={newKeys.has(row.event_uid)}
                changed={changedKeys.has(row.event_uid)}
                onToggle={toggleSelection}
                onOpen={(item, trigger) => { sheetTrigger.current = trigger; openEvent(item, trigger) }}
              />
            ))}
          </ul>}
          {!loading && loaded && !rows.length && !error && (isNarrowed
            ? <div className="empty-state"><Icon name="search" /><h3>No check-ins match</h3><p>Try fewer filters or a longer date range.</p><div className="attendance-empty-actions"><button className="button secondary" type="button" onClick={clearFilters}>Clear filters</button>{showSearchWider && <button className="text-button" type="button" onClick={() => setFilters({ when: '7d' })}>Search the last 7 days instead</button>}</div></div>
            : <div className="empty-state"><Icon name="clock" /><h3>{rangeToday ? 'No check-ins yet today' : 'No check-ins on these dates'}</h3><p>{rangeToday ? 'New check-ins appear here as people check in.' : 'Try a longer date range.'}</p></div>)}
        </div>
        <div className="load-more attendance-pagination">
          <small>{rows.length >= MAX_ROWS ? `Showing the newest ${MAX_ROWS}` : !isNarrowed && summaryTotal !== null ? `Showing ${rows.length.toLocaleString('en-PK')} of ${summaryTotal.toLocaleString('en-PK')}` : `Showing ${rows.length.toLocaleString('en-PK')}`}</small>
          {nextCursor && rows.length < MAX_ROWS && <button className="button secondary" type="button" disabled={loadingMore} onClick={() => void loadOlder()}>{loadingMore ? 'Loading…' : 'Show older check-ins'}</button>}
        </div>
      </section>

      {selectMode && (selected.size > 0 || quick.progress) && (
        <div className="attendance-select-bar" role="region" aria-label="Chosen check-ins">
          <p><strong>{selected.size.toLocaleString('en-PK')} selected</strong>{atCap && <span> · You can send up to {MAX_ROWS} at a time.</span>}</p>
          <div className="attendance-select-actions">
            <button type="button" className="button primary" disabled={!selected.size || Boolean(quick.progress)} onClick={() => setSingleSendRequest((value) => value + 1)}>Send to HR system</button>
            {filters.machine_match && <button type="button" className="button secondary" disabled={Boolean(quick.progress) || !selected.size} onClick={() => void quick.release([...selected.keys()])}>{quick.progress ? `Sending ${quick.progress.done.toLocaleString()} of ${quick.progress.total.toLocaleString()}…` : `Send ${selected.size.toLocaleString()} now`}</button>}
            <button type="button" className="button secondary" disabled={Boolean(quick.progress)} onClick={clearSelection}>Clear</button>
          </div>
        </div>
      )}
      {filters.event > 0 && (
        <AttendanceSheet
          row={rows.find((row) => row.id === filters.event) || null}
          missing={loaded && !loading && !rows.some((row) => row.id === filters.event)}
          onClose={closeEvent}
          onSend={(row) => { closeEvent(); setSelected(new Map([[row.id, row]])); setSingleSendRequest((value) => value + 1) }}
          onChanged={() => void refreshFeed(true)}
          toast={toast}
          autoSending={autoSending}
          returnFocusRef={sheetTrigger}
        />
      )}
    </div>
  )
}

export function AttendanceView({
  devices,
  revision,
  realtimeState,
  realtimeLastSyncAt: _realtimeLastSyncAt,
  toast,
  overview,
}: {
  devices: Device[]
  revision: number
  realtimeState: RealtimeState
  realtimeLastSyncAt: Date | null
  toast?: Toast
  overview?: Overview | null
}) {
  const navigate = useNavigate()
  const [division] = useDivision()
  const { filters, setFilters, view, tool, go, search, setSearch } = useAttendanceFilters()
  const safeToast = useMemo(() => toast || { notice: () => undefined, error: () => undefined }, [toast])
  const [frozen, setFrozen] = useState(false)
  const [selectMode, setSelectMode] = useState(false)
  const [dayKey, setDayKey] = useState(() => todayKey())
  useEffect(() => {
    const timer = window.setTimeout(() => setDayKey(todayKey()), msUntilNextPktMidnight())
    return () => window.clearTimeout(timer)
  }, [dayKey])
  const range = useMemo(() => pktPresetRange(filters.when, Date.now(), filters.from, filters.to),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [filters.when, filters.from, filters.to, dayKey])
  const summaryQuery = useMemo(() => summaryParams(filters, division).toString(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [filters.when, filters.from, filters.to, filters.device.join(), division, dayKey])
  const summary = useAttendanceSummary({ query: summaryQuery, tooLong: range.days > 31, revision, frozen })
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 10_000)
    return () => window.clearInterval(timer)
  }, [])
  const totals = summary.data?.totals
  const fixCount = filters.when === 'today' && totals ? totals.not_matched + totals.set_aside + totals.problem : null
  const params = new URLSearchParams(typeof window === 'undefined' ? '' : window.location.search)
  const extended = overview as (Overview & { attendance_all_time?: AttendanceAllTime; checkin_notifications?: CheckinNotificationOverview }) | null | undefined
  const historyKind = (['all', 'hand', 'not-matched', 'old-release', 'old-repair'].includes(params.get('kind') || '') ? params.get('kind') : 'all') as HistoryKind
  // Opening pushes `event=<id>` (Back closes the sheet); closing replaces it.
  const openEvent = useCallback((row: AttendanceRow) => setFilters({ event: row.id }), [setFilters])
  const closeEvent = useCallback(() => setFilters({ event: 0 }, { history: 'replace' }), [setFilters])

  return (
    <div className="attendance-workspace">
      <PageHeader
        title="Attendance"
        description={view === 'checkins' ? 'Every check-in from every machine, and whether it reached the HR system (Oracle).' : 'Tools to send check-ins that are waiting, not matched or set aside.'}
      />
      <Tabs
        idPrefix="attendance"
        label="Attendance sections"
        value={view}
        onChange={(next) => go({ view: next })}
        tabs={[
          { id: 'checkins', label: 'Check-ins' },
          { id: 'fix-tools', label: 'Fix tools', count: fixCount && fixCount > 0 ? fixCount : null },
        ]}
        className="attendance-tabs"
      />
      <TabPanel idPrefix="attendance" id={view}>
        {view === 'checkins' && <>
          <p className="attendance-date-line"><strong>{range.label}</strong>{summary.data && <span> · {updatedLine(summary.data.computed_at, now)}</span>}</p>
          <AttendanceToday
            state={summary}
            division={division}
            chosen={filters.status}
            onChoose={(group) => setFilters({ status: group ? [group] : [] })}
            rangeLabel={range.label}
            now={now}
          />
          <AttendanceFilters
            filters={filters}
            onChange={(change) => setFilters(change)}
            search={search}
            onSearch={setSearch}
            devices={devices}
            division={division}
            counts={totals || null}
          />
          <CheckinsTab
            devices={devices}
            revision={revision}
            realtimeState={realtimeState}
            toast={safeToast}
            division={division}
            filters={filters}
            setFilters={setFilters}
            search={search}
            setSearch={setSearch}
            summaryTotal={totals ? totals.received : null}
            frozen={frozen}
            setFrozen={setFrozen}
            selectMode={selectMode}
            setSelectMode={setSelectMode}
            notifyState={extended?.checkin_notifications}
            onOpenSettings={() => navigate('/settings')}
            openEvent={openEvent}
            closeEvent={closeEvent}
          />
        </>}
        {view === 'fix-tools' && (
          <Suspense fallback={<p className="attendance-history-note" role="status">Loading…</p>}><FixTools
            tool={tool}
            onTool={(next) => go({ tool: next })}
            onShowMachineMatch={() => go({ view: 'checkins', extra: { machine_match: '1' } })}
            onChooseByHand={() => { setSelectMode(true); go({ view: 'checkins', extra: { status: 'not_matched' } }) }}
            devices={devices}
            toast={safeToast}
            allTime={extended?.attendance_all_time}
            overviewLoading={overview === undefined || overview === null}
            deviceId={params.get('device_id')}
            revision={revision}
            historyKind={historyKind}
            onHistoryKind={(kind) => go({ tool: 'history', extra: { kind: kind === 'all' ? null : kind } })}
            releaseJobId={params.get('release_job')}
          /></Suspense>
        )}
      </TabPanel>
    </div>
  )
}


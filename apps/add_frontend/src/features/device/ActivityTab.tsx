// Activity (04-device §3): what the device did, one plain sentence per event,
// grouped by Pakistan day, routine runs folded. Filters (Problems only and
// the six groups), a day choice, Load older, and live rows for this device
// only. "Technical log" keeps the raw rows for IT.
import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from '../../api'
import { useAppData } from '../../AppData'
import { formatNumber } from '../../lib/format'
import { ago, day, pakistanDay } from '../../lib/time'
import { useThrottledRunner } from '../../lib/throttle'
import type { RealtimeState } from '../../realtime'
import type { DeviceLog } from '../../types'
import { Button, Callout, EmptyState, ErrorState, IconButton, SegmentedControl, Skeleton } from '../../ui'
import { ACTIVITY_TAB, ACTIVITY_TOPICS, ACTIVITY_WORDS_TAB, TOPIC_LABELS } from '../../words/activity'
import type { DeviceDetail } from '../../words/device'
import { ActivityEventSheet, ActivityRow, itemFromLog } from './ActivityRow'
import {
  ActivityMissing, activityWindow, fetchActivity, itemKey, itemTime, keptDays,
  type ActivityItem, type ActivityPage, type ActivityWhen,
} from './activityData'
import { DETAIL_REFRESH_MS, useDeviceUrl } from './useDeviceData'

const TechnicalLog = lazy(() => import('./TechnicalLog').then((module) => ({ default: module.TechnicalLog })))

/** The frame's live state when the device view sits inside the dashboard. */
function useRealtimeState(): RealtimeState | null {
  try {
    return useAppData().realtime.state
  } catch {
    return null
  }
}

const LIVE_WORDS: Record<RealtimeState, string> = {
  live: ACTIVITY_WORDS_TAB.live,
  connecting: ACTIVITY_WORDS_TAB.reconnecting,
  reconnecting: ACTIVITY_WORDS_TAB.reconnecting,
  stale: ACTIVITY_WORDS_TAB.offline,
}

const mergeItems = (top: ActivityItem[], rest: ActivityItem[]) => {
  const seen = new Set<string>()
  return [...top, ...rest].filter((item) => {
    const key = itemKey(item)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

async function fallbackPage(connectorId: string, since: string, until: string, cursor: number | null, show: 'all' | 'problems'): Promise<ActivityPage & { fallback: true; logCursor: number | null }> {
  const params = new URLSearchParams({ since, until, limit: '100' })
  if (cursor) params.set('cursor', String(cursor))
  const answer = await api<{ rows: DeviceLog[]; next_cursor: number | null }>(`/api/v1/devices/${encodeURIComponent(connectorId)}/logs?${params.toString()}`)
  const items = (answer.rows || []).map(itemFromLog).filter((item) => show === 'all' || item.kind === 'problem' || item.kind === 'review' || item.kind === 'retried')
  return { items, next_cursor: answer.next_cursor ? String(answer.next_cursor) : null, newest_id: answer.rows?.[0]?.id ?? null, counts: null, fallback: true, logCursor: answer.next_cursor }
}

type LoadedPage = ActivityPage & { fallback?: boolean }

export function ActivityTab({ device, revision }: { device: DeviceDetail; revision: number }) {
  const [url, setUrl] = useDeviceUrl()
  const realtime = useRealtimeState()
  const id = device.connector_id
  const range = useMemo(() => activityWindow(url.when as ActivityWhen, url.day), [url.when, url.day])
  const filterKey = JSON.stringify({ show: url.show, topic: url.topic, ...range })
  const [older, setOlder] = useState<ActivityItem[]>([])
  const [cursor, setCursor] = useState<string | null | undefined>(undefined)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [olderError, setOlderError] = useState(false)
  const [fresh, setFresh] = useState<ActivityItem[]>([])
  const [waiting, setWaiting] = useState<ActivityItem[]>([])
  const [newKeys, setNewKeys] = useState<Set<string>>(new Set())
  const [open, setOpen] = useState<ActivityItem | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const first = useQuery<LoadedPage>({
    queryKey: ['dv-activity', id, filterKey],
    queryFn: async ({ signal }) => {
      try {
        return await fetchActivity(id, { show: url.show, topics: url.topic, since: range.since, until: range.until, limit: 50 }, signal)
      } catch (error) {
        if (error instanceof ActivityMissing) return fallbackPage(id, range.since, range.until, null, url.show)
        throw error
      }
    },
    retry: false,
    staleTime: 0,
    placeholderData: keepPreviousData,
  })
  const { refetch, isFetching } = first

  useEffect(() => {
    setOlder([])
    setCursor(undefined)
    setFresh([])
    setWaiting([])
    setNewKeys(new Set())
    setOlderError(false)
  }, [filterKey])
  useEffect(() => { if (first.data) setUpdatedAt(Date.now()) }, [first.data])

  const page = first.data
  const fallback = Boolean(page?.fallback)
  const nextCursor = cursor === undefined ? page?.next_cursor ?? null : cursor
  const newestId = useRef<number | null>(null)
  useEffect(() => { newestId.current = page?.newest_id ?? null }, [page?.newest_id])

  // Live rows: only for this device (the frame's revision), at most every 3 s.
  const live = useThrottledRunner(async () => {
    if (fallback || url.when !== 'today' || newestId.current == null) return
    try {
      const answer = await fetchActivity(id, { show: url.show, topics: url.topic, after: newestId.current, since: range.since, until: new Date(Date.now() + 60_000).toISOString(), limit: 50 })
      if (answer.newest_id != null) newestId.current = answer.newest_id
      if (!answer.items.length) return
      const scroller = listRef.current?.closest('.dv-panel-body, .dv-page') as HTMLElement | null
      const atTop = !scroller || scroller.scrollTop < 80
      if (atTop) {
        setFresh((current) => mergeItems(answer.items, current))
        setNewKeys(new Set(answer.items.map(itemKey)))
      } else {
        setWaiting((current) => mergeItems(answer.items, current))
      }
      setUpdatedAt(Date.now())
    } catch {
      // The next event or Refresh tries again.
    }
  }, DETAIL_REFRESH_MS)
  const firstRevision = useRef(revision)
  useEffect(() => {
    if (firstRevision.current === revision) return
    firstRevision.current = revision
    live.schedule()
  }, [live, revision])

  const items = useMemo(() => mergeItems(fresh, mergeItems(page?.items || [], older)), [fresh, older, page?.items])

  const loadOlder = async () => {
    if (!nextCursor) return
    setLoadingOlder(true)
    setOlderError(false)
    try {
      const answer: LoadedPage = fallback
        ? await fallbackPage(id, range.since, range.until, Number(nextCursor), url.show)
        : await fetchActivity(id, { show: url.show, topics: url.topic, since: range.since, until: range.until, before: nextCursor, limit: 50 })
      setOlder((current) => mergeItems(current, answer.items))
      setCursor(answer.next_cursor ?? null)
    } catch {
      setOlderError(true)
    } finally {
      setLoadingOlder(false)
    }
  }

  const refresh = async () => {
    setFresh([])
    setWaiting([])
    setOlder([])
    setCursor(undefined)
    await refetch()
  }

  const toggleTopic = (topic: string) => {
    const next = url.topic.includes(topic) ? url.topic.filter((value) => value !== topic) : [...url.topic, topic]
    setUrl({ topic: next })
  }
  const filtered = url.show === 'problems' || url.topic.length > 0
  const period = url.when === 'today' ? ACTIVITY_WORDS_TAB.periodToday : url.when === '7d' ? ACTIVITY_WORDS_TAB.periodWeek : ACTIVITY_WORDS_TAB.periodDay
  const summaryPeriod = url.when === 'today' ? ACTIVITY_WORDS_TAB.summaryPeriodToday : url.when === '7d' ? ACTIVITY_WORDS_TAB.summaryPeriodWeek : ACTIVITY_WORDS_TAB.summaryPeriodDay
  const counts = page?.counts
  const summary = counts
    ? counts.problems === 0
      ? ACTIVITY_WORDS_TAB.summaryNone(period, formatNumber(counts.routine))
      : ACTIVITY_WORDS_TAB.summarySome(summaryPeriod, `${formatNumber(counts.problems)} ${counts.problems === 1 ? 'problem' : 'problems'}`, `${formatNumber(counts.retried)} ${counts.retried === 1 ? 'thing was' : 'things were'}`)
    : null
  const days = keptDays()
  const dayValue = url.when === 'day' ? url.day : url.when

  // Day groups, newest first.
  const groups: Array<{ key: string; label: string; items: ActivityItem[] }> = []
  const today = pakistanDay(new Date())
  const yesterday = days[1]
  for (const item of items) {
    const key = pakistanDay(itemTime(item))
    let group = groups[groups.length - 1]
    if (!group || group.key !== key) {
      const prefix = key === today ? `${ACTIVITY_WORDS_TAB.today} · ` : key === yesterday ? `${ACTIVITY_WORDS_TAB.yesterday} · ` : ''
      group = { key, label: `${prefix}${day(itemTime(item))}`, items: [] }
      groups.push(group)
    }
    group.items.push(item)
  }

  const header = (
    <div className="dv-activity-bar">
      <SegmentedControl
        options={[{ id: 'plain', label: ACTIVITY_TAB.plainView }, { id: 'technical', label: ACTIVITY_TAB.technicalLog }]}
        value={url.view}
        onChange={(view) => setUrl({ view })}
        label={ACTIVITY_TAB.ariaLabel}
      />
      {realtime && <span className={`dv-live is-${realtime}`}><span aria-hidden="true">●</span> {LIVE_WORDS[realtime]}</span>}
    </div>
  )

  if (url.view === 'technical') {
    return (
      <section className="dv-activity" aria-label={ACTIVITY_TAB.technicalLog}>
        {header}
        <Suspense fallback={<Skeleton variant="row" count={6} label={ACTIVITY_TAB.technicalLog} />}>
          <TechnicalLog connectorId={id} />
        </Suspense>
      </section>
    )
  }

  return (
    <section className="dv-activity" aria-label={ACTIVITY_TAB.ariaLabel}>
      {header}
      {summary && <p className="dv-activity-summary">{summary}</p>}
      <div className="dv-activity-filters">
        <div className="dv-chip-row" role="group" aria-label={ACTIVITY_WORDS_TAB.topicsLabel}>
          <button type="button" className="dv-chip" aria-pressed={!filtered} disabled={first.isPending} onClick={() => setUrl({ show: 'all', topic: [] })}>{ACTIVITY_WORDS_TAB.all}</button>
          <button type="button" className="dv-chip" aria-pressed={url.show === 'problems'} disabled={first.isPending} onClick={() => setUrl({ show: url.show === 'problems' ? 'all' : 'problems' })}>
            {ACTIVITY_WORDS_TAB.problemsOnly}{counts && counts.problems > 0 ? <span className="dv-chip-count"> {formatNumber(counts.problems)}</span> : null}
          </button>
          {ACTIVITY_TOPICS.map((topic) => (
            <button key={topic} type="button" className="dv-chip" aria-pressed={url.topic.includes(topic)} disabled={first.isPending} onClick={() => toggleTopic(topic)}>
              {TOPIC_LABELS[topic]}{counts?.by_topic?.[topic] ? <span className="dv-chip-count"> {formatNumber(counts.by_topic[topic])}</span> : null}
            </button>
          ))}
        </div>
        <div className="dv-activity-day">
          <label>
            <span className="sr-only">{ACTIVITY_WORDS_TAB.dayLabel}</span>
            <select value={dayValue} onChange={(event) => {
              const value = event.target.value
              if (value === 'today' || value === 'yesterday' || value === '7d') setUrl({ when: value })
              else setUrl({ day: value })
            }}>
              <option value="today">{ACTIVITY_WORDS_TAB.today}</option>
              <option value="yesterday">{ACTIVITY_WORDS_TAB.yesterday}</option>
              <option value="7d">{ACTIVITY_WORDS_TAB.last7}</option>
              <optgroup label={ACTIVITY_WORDS_TAB.pickDay}>
                {days.slice(2).map((value) => <option key={value} value={value}>{day(`${value}T12:00:00+05:00`)}</option>)}
              </optgroup>
            </select>
          </label>
          <IconButton label={ACTIVITY_TAB.refresh} icon="refresh" busy={isFetching} onClick={() => void refresh()} />
          {updatedAt && <span className="dv-muted">{ACTIVITY_WORDS_TAB.updated(ago(updatedAt))}</span>}
        </div>
      </div>
      {fallback && <Callout tone="info">{ACTIVITY_TAB.simpler}</Callout>}
      <div ref={listRef} className={`dv-activity-list${first.isPlaceholderData ? ' is-updating' : ''}`} aria-busy={first.isPlaceholderData || undefined}>
        {waiting.length > 0 && (
          <button type="button" className="dv-new-pill" onClick={() => {
            setFresh((current) => mergeItems(waiting, current))
            setNewKeys(new Set(waiting.map(itemKey)))
            setWaiting([])
            const scroller = listRef.current?.closest('.dv-panel-body, .dv-page') as HTMLElement | null
            if (scroller) scroller.scrollTop = 0
          }}>{ACTIVITY_WORDS_TAB.newRows(waiting.length)}</button>
        )}
        {first.isPending ? <Skeleton variant="row" count={6} label={ACTIVITY_WORDS_TAB.loading} />
          : first.isError ? <ErrorState title={ACTIVITY_TAB.loadError} onRetry={() => refetch()} />
            : items.length === 0 ? (
              filtered
                ? <EmptyState icon="check" title={url.show === 'problems' ? ACTIVITY_WORDS_TAB.emptyProblems(period) : ACTIVITY_WORDS_TAB.emptyKind(period)} action={<Button onClick={() => setUrl({ show: 'all', topic: [] })}>{ACTIVITY_TAB.clearFilters}</Button>} />
                : <EmptyState icon="list" title={ACTIVITY_TAB.empty} />
            ) : groups.map((group) => (
              <section key={group.key} className="dv-day" aria-label={group.label}>
                <h4 className="dv-day-header">{group.label}</h4>
                <ol className="dv-activity-rows">
                  {group.items.map((item) => <ActivityRow key={itemKey(item)} item={item} connectorId={id} onOpen={setOpen} isNew={newKeys.has(itemKey(item))} />)}
                </ol>
              </section>
            ))}
        {!first.isPending && !first.isError && items.length > 0 && (
          nextCursor
            ? <Button fullWidth size="lg" busy={loadingOlder} busyLabel={ACTIVITY_WORDS_TAB.loading} onClick={() => void loadOlder()}>{ACTIVITY_TAB.loadOlder}</Button>
            : <p className="dv-muted dv-activity-end">{url.when === '7d' ? ACTIVITY_TAB.end : ACTIVITY_TAB.kept}</p>
        )}
        {olderError && <ErrorState compact title={ACTIVITY_TAB.loadError} onRetry={() => loadOlder()} />}
      </div>
      {open && <ActivityEventSheet item={open} onClose={() => setOpen(null)} />}
    </section>
  )
}

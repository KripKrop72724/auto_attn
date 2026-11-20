// Technical log (04-device §3): the raw rows of GET /logs for IT, with the
// old console's fixes only: no window dots, no inner scroller, full date and
// time in the row, a level column wide enough for CRITICAL, Level / Area / day
// filters and Load older by cursor. Everything here is technical text.
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api'
import { PAKISTAN_TIME_ZONE } from '../../lib/time'
import type { DeviceLog } from '../../types'
import { Button, ErrorState, Skeleton } from '../../ui'
import { ACTIVITY_TAB, ACTIVITY_WORDS_TAB, SUBSYSTEM_GROUPS } from '../../words/activity'
import { activityWindow, type ActivityWhen } from './activityData'
import { useDeviceUrl } from './useDeviceData'

const AREAS = [...Object.keys(SUBSYSTEM_GROUPS), 'add', 'firmware'].sort()
const LEVELS: Record<'warn' | 'error', string[]> = { warn: ['WARN', 'ERROR', 'CRITICAL'], error: ['ERROR', 'CRITICAL'] }
const stamp = new Intl.DateTimeFormat('en-GB', { timeZone: PAKISTAN_TIME_ZONE, day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })
const fullTime = (value: string) => stamp.format(new Date(value)).replace(',', '')
const levelTone = (level: string) => /ERR|FATAL|CRIT/i.test(level) ? 'problem' : /WARN/i.test(level) ? 'warning' : 'neutral'

function logsPath(connectorId: string, values: { level: '' | 'warn' | 'error'; area: string; since: string; until: string; cursor?: number | null }) {
  const params = new URLSearchParams()
  for (const level of values.level ? LEVELS[values.level] : []) params.append('level', level)
  if (values.area) params.set('subsystem', values.area)
  params.set('since', values.since)
  params.set('until', values.until)
  if (values.cursor) params.set('cursor', String(values.cursor))
  params.set('limit', '200')
  return `/api/v1/devices/${encodeURIComponent(connectorId)}/logs?${params.toString()}`
}

export function TechnicalLog({ connectorId }: { connectorId: string }) {
  const [url, setUrl] = useDeviceUrl()
  const range = useMemo(() => activityWindow(url.when as ActivityWhen, url.day), [url.when, url.day])
  const key = JSON.stringify({ level: url.level, area: url.area, ...range })
  const first = useQuery({
    queryKey: ['dv-logs', connectorId, key],
    queryFn: ({ signal }) => api<{ rows: DeviceLog[]; next_cursor: number | null }>(logsPath(connectorId, { level: url.level, area: url.area, ...range }), { signal }),
    retry: false,
    staleTime: 0,
  })
  const [older, setOlder] = useState<DeviceLog[]>([])
  const [cursor, setCursor] = useState<number | null | undefined>(undefined)
  const [busy, setBusy] = useState(false)
  useEffect(() => { setOlder([]); setCursor(undefined) }, [key])
  const rows = [...(first.data?.rows || []), ...older]
  const next = cursor === undefined ? first.data?.next_cursor ?? null : cursor
  const loadOlder = async () => {
    if (!next) return
    setBusy(true)
    try {
      const answer = await api<{ rows: DeviceLog[]; next_cursor: number | null }>(logsPath(connectorId, { level: url.level, area: url.area, ...range, cursor: next }))
      setOlder((current) => [...current, ...(answer.rows || [])])
      setCursor(answer.next_cursor ?? null)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="dv-technical-log" data-technical>
      <div className="dv-activity-filters">
        <label>
          <span>{ACTIVITY_WORDS_TAB.levelLabel}</span>
          <select value={url.level} onChange={(event) => setUrl({ level: event.target.value as '' | 'warn' | 'error' })}>
            <option value="">{ACTIVITY_WORDS_TAB.levelAny}</option>
            <option value="warn">{ACTIVITY_WORDS_TAB.levelWarn}</option>
            <option value="error">{ACTIVITY_WORDS_TAB.levelError}</option>
          </select>
        </label>
        <label>
          <span>{ACTIVITY_WORDS_TAB.areaLabel}</span>
          <select value={url.area} onChange={(event) => setUrl({ area: event.target.value })}>
            <option value="">{ACTIVITY_WORDS_TAB.areaAny}</option>
            {AREAS.map((area) => <option key={area} value={area}>{area}</option>)}
          </select>
        </label>
        <label>
          <span>{ACTIVITY_WORDS_TAB.dayLabel}</span>
          <select value={url.when === 'day' ? url.day : url.when} onChange={(event) => {
            const value = event.target.value
            if (value === 'today' || value === 'yesterday' || value === '7d') setUrl({ when: value })
            else setUrl({ day: value })
          }}>
            <option value="today">{ACTIVITY_WORDS_TAB.today}</option>
            <option value="yesterday">{ACTIVITY_WORDS_TAB.yesterday}</option>
            <option value="7d">{ACTIVITY_WORDS_TAB.last7}</option>
            {url.when === 'day' && <option value={url.day}>{url.day}</option>}
          </select>
        </label>
      </div>
      <h4>{ACTIVITY_TAB.technicalLog}</h4>
      {first.isPending ? <Skeleton variant="row" count={6} label={ACTIVITY_TAB.technicalLog} />
        : first.isError ? <ErrorState title={ACTIVITY_TAB.loadError} onRetry={() => first.refetch()} />
          : rows.length === 0 ? <p>{ACTIVITY_TAB.empty}</p> : (
            <ol className="dv-log-rows">
              {rows.map((row) => (
                <li key={row.id} className={`dv-log-row tone-${levelTone(row.level)}`}>
                  <time dateTime={row.received_at}>{fullTime(row.device_time || row.received_at)}</time>
                  <strong className="dv-log-level">{row.level}</strong>
                  <em className="dv-log-area">{row.subsystem}</em>
                  <span className="dv-log-message">{row.code ? `[${row.code}] ` : ''}{row.message}</span>
                </li>
              ))}
            </ol>
          )}
      {next && <Button fullWidth busy={busy} busyLabel={ACTIVITY_WORDS_TAB.loading} onClick={() => void loadOlder()}>{ACTIVITY_TAB.loadOlder}</Button>}
    </div>
  )
}

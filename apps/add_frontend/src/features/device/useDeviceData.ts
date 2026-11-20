// Data for one device page (DEV-4 data layer): one TanStack query per section,
// loaded only while its tab is open, refetched only when a live event names
// this device (the frame passes deviceRevision(id)), at most every 3 s.
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router-dom'
import { api, queryString } from '../../api'
import { deviceSnapshots, useDevice } from '../../deviceData'
import { useThrottledRunner } from '../../lib/throttle'
import { normalizedStatus } from '../../status'
import type { Alert, CommKeyState, ConnectionEvent, Device, ReconciliationPreflight } from '../../types'
import { DEVICE_TABS, type DeviceDetail, type DeviceTab } from '../../words/device'

export const DETAIL_REFRESH_MS = 3_000

const path = (id: string, rest = '') => `/api/v1/devices/${encodeURIComponent(id)}${rest}`

export const deviceKeys = {
  all: (id: string) => ['device', id] as const,
  detail: (id: string) => ['device', id, 'detail'] as const,
  alerts: (id: string, queue: 'ACTIVE' | 'RESOLVED') => ['device', id, 'alerts', queue] as const,
  commKey: (id: string) => ['device', id, 'comm-key'] as const,
  connectivity: (id: string) => ['device', id, 'connectivity'] as const,
  preflight: (id: string) => ['device', id, 'preflight'] as const,
}

const isResolved = (alert: Alert) => normalizedStatus(alert.state).toUpperCase() === 'RESOLVED'

/** The detail answer, kept in the shared device store so lists see it too. */
export function useDeviceDetail(seed: Device) {
  const id = seed.connector_id
  const query = useQuery({
    queryKey: deviceKeys.detail(id),
    queryFn: ({ signal }) => api<DeviceDetail>(path(id), { signal }),
    staleTime: 0,
    retry: false,
  })
  useEffect(() => {
    if (query.data) deviceSnapshots.put([query.data])
  }, [query.data])
  const live = useDevice(seed) as DeviceDetail
  // The store copy follows live heartbeats; the detail answer adds the fields only it has.
  const device = useMemo<DeviceDetail>(() => (query.data ? { ...query.data, ...live, health: live.health?.reasons ? live.health : query.data.health ?? live.health, attendance_today: query.data.attendance_today, status_since: query.data.status_since ?? live.status_since, offline_reason: query.data.offline_reason ?? live.offline_reason } : live), [live, query.data])
  return { device, query }
}

export function useActiveAlerts(id: string) {
  return useQuery({
    queryKey: deviceKeys.alerts(id, 'ACTIVE'),
    queryFn: async ({ signal }) => {
      const answer = await api<{ rows: Alert[] }>(path(id, `/alerts${queryString({ queue: 'ACTIVE', limit: 200 })}`), { signal })
      // Backends without queue filters return every row; split by state here too.
      return (answer.rows || []).filter((row) => !isResolved(row))
    },
    staleTime: 0,
    retry: false,
  })
}

export function useResolvedAlerts(id: string, enabled: boolean) {
  return useQuery({
    queryKey: deviceKeys.alerts(id, 'RESOLVED'),
    queryFn: async ({ signal }) => {
      const answer = await api<{ rows: Alert[] }>(path(id, `/alerts${queryString({ queue: 'RESOLVED', limit: 20 })}`), { signal })
      return (answer.rows || []).filter(isResolved).slice(0, 20)
    },
    enabled,
    staleTime: 0,
    retry: false,
  })
}

export function useCommKeyState(id: string, enabled: boolean) {
  return useQuery({
    queryKey: deviceKeys.commKey(id),
    queryFn: async ({ signal }) => {
      const answer = await api<CommKeyState>(path(id, '/comm-key'), { signal })
      // A missing or odd answer reads as "not managed", never as a crash.
      return answer && typeof answer === 'object' && 'management_state' in answer ? answer : null
    },
    enabled,
    staleTime: 0,
    retry: false,
  })
}

export function useConnectivity(id: string, enabled: boolean) {
  return useQuery({
    queryKey: deviceKeys.connectivity(id),
    queryFn: async ({ signal }) => (await api<{ rows: ConnectionEvent[] }>(path(id, '/connectivity?limit=40'), { signal })).rows || [],
    enabled,
    retry: false,
  })
}

export function usePreflight(id: string, enabled: boolean) {
  return useQuery({
    queryKey: deviceKeys.preflight(id),
    queryFn: ({ signal }) => api<ReconciliationPreflight>(path(id, '/reconciliations/preflight'), { signal }),
    enabled,
    staleTime: 0,
    retry: false,
  })
}

/**
 * Re-reads the open sections when this device's live revision moves, at most
 * every 3 s (trailing edge, so the last change is always picked up).
 */
export function useLiveRefresh(id: string, revision: number) {
  const client = useQueryClient()
  const runner = useThrottledRunner(() => client.invalidateQueries({ queryKey: deviceKeys.all(id) }), DETAIL_REFRESH_MS)
  const first = useRef(revision)
  useEffect(() => {
    if (first.current === revision) return
    first.current = revision
    runner.schedule()
  }, [revision, runner])
  return useCallback(() => runner.runNow(), [runner])
}

// ------------------------------------------------------------------ URL state

/** The device page's own URL keys (never cnic / password / reason / confirmation). */
export interface DeviceUrl {
  tab: DeviceTab
  advanced: boolean
  view: 'plain' | 'technical'
  show: 'all' | 'problems'
  topic: string[]
  when: 'today' | 'yesterday' | '7d' | 'day'
  day: string
  level: '' | 'warn' | 'error'
  area: string
}

export const DEVICE_TOPICS = ['machine', 'link_box', 'storage', 'hr_system', 'people', 'software'] as const

const readUrl = (search: string): DeviceUrl => {
  const params = new URLSearchParams(search)
  const tab = params.get('tab') as DeviceTab | null
  const day = params.get('day') || ''
  const when = params.get('when')
  const level = params.get('level')
  return {
    tab: tab && DEVICE_TABS.includes(tab) ? tab : 'summary',
    advanced: params.get('advanced') === '1',
    view: params.get('view') === 'technical' ? 'technical' : 'plain',
    show: params.get('show') === 'problems' ? 'problems' : 'all',
    topic: (params.get('topic') || '').split(',').filter((value): value is string => (DEVICE_TOPICS as readonly string[]).includes(value)),
    when: /^\d{4}-\d{2}-\d{2}$/.test(day) ? 'day' : when === 'yesterday' || when === '7d' ? when : 'today',
    day: /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : '',
    level: level === 'warn' || level === 'error' ? level : '',
    area: /^[a-z_]{1,40}$/.test(params.get('area') || '') ? params.get('area') || '' : '',
  }
}

const DEFAULTS: DeviceUrl = { tab: 'summary', advanced: false, view: 'plain', show: 'all', topic: [], when: 'today', day: '', level: '', area: '' }

/**
 * `?tab=…` and the Activity filters. Written with `replace` and the history
 * state kept, so Back closes the device (and the page behind it stays put).
 */
export function useDeviceUrl(): [DeviceUrl, (change: Partial<DeviceUrl>) => void] {
  const location = useLocation()
  const navigate = useNavigate()
  const values = useMemo(() => readUrl(location.search), [location.search])
  const latest = useRef({ location, values })
  latest.current = { location, values }
  const set = useCallback((change: Partial<DeviceUrl>) => {
    const { location: current } = latest.current
    const params = new URLSearchParams(current.search)
    const next = { ...readUrl(current.search), ...change }
    if (change.when && change.when !== 'day') next.day = ''
    if (change.day) next.when = 'day'
    for (const key of Object.keys(DEFAULTS) as Array<keyof DeviceUrl>) {
      const value = next[key]
      const fallback = DEFAULTS[key]
      const text = Array.isArray(value) ? value.join(',') : typeof value === 'boolean' ? (value ? '1' : '') : String(value)
      const empty = Array.isArray(fallback) ? '' : typeof fallback === 'boolean' ? '' : String(fallback)
      if (key === 'when' && next.when === 'day') params.delete('when')
      else if (text === empty || text === '') params.delete(key)
      else params.set(key, text)
    }
    const query = params.toString()
    navigate(`${current.pathname}${query ? `?${query}` : ''}${current.hash}`, { replace: true, state: current.state, preventScrollReset: true })
  }, [navigate])
  return [values, set]
}

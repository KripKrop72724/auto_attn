// The device Activity answer (GET /api/v1/devices/{id}/activity, be/device_activity.py)
// and the Pakistan-day window the filters ask for.
import { api, ApiError } from '../../api'
import { pakistanDay } from '../../lib/time'
import type { DeviceLog } from '../../types'

export type ActivityKind = 'problem' | 'review' | 'retried' | 'good' | 'routine' | 'action'

interface ItemBase {
  kind: ActivityKind
  topic: string
  title: string
  code?: string | null
  action?: string | null
  fixed_at?: string | null
}

export interface ActivityEvent extends ItemBase { type: 'event'; id: number; at: string; serious?: boolean; raw: DeviceLog | null }
export interface ActivityGroupItem extends ItemBase { type: 'group'; count: number; first_at: string; last_at: string; first_id: number; last_id: number }
export interface ActivityEscalation extends ItemBase { type: 'escalation'; count: number; first_at: string; last_at: string; first_id?: number; last_id?: number }
export interface ActivityAction extends ItemBase { type: 'action'; at: string; command_id: string; command_type: string; status: string; actor: string | null }
export interface ActivityProblemEvent extends ItemBase { type: 'problem_event'; at: string; alert_id: number; event: 'opened' | 'seen' | 'fixed' | 'cleared' | string; actor: string | null }

export type ActivityItem = ActivityEvent | ActivityGroupItem | ActivityEscalation | ActivityAction | ActivityProblemEvent

export interface ActivityCounts {
  problems: number
  retried: number
  routine: number
  by_topic: Record<string, number>
}

export interface ActivityPage {
  items: ActivityItem[]
  next_cursor: string | null
  newest_id: number | null
  counts: ActivityCounts | null
  window?: { since: string; until: string }
  retention_days?: number
}

export const itemTime = (item: ActivityItem) => ('at' in item ? item.at : item.last_at)
export const itemKey = (item: ActivityItem) => {
  switch (item.type) {
    case 'event': return `event-${item.id}`
    case 'group': return `group-${item.first_id}-${item.last_id}`
    case 'escalation': return `escalation-${item.first_id ?? item.first_at}-${item.last_id ?? item.last_at}`
    case 'action': return `action-${item.command_id}`
    default: return `problem-${item.alert_id}-${item.event}-${item.at}`
  }
}

/** Pakistan midnight of a "2026-11-06" day, as an ISO moment. */
export const pktMidnight = (day: string) => new Date(`${day}T00:00:00+05:00`).toISOString()

const addDays = (day: string, days: number) => {
  const date = new Date(`${day}T12:00:00+05:00`)
  date.setUTCDate(date.getUTCDate() + days)
  return pakistanDay(date)
}

export type ActivityWhen = 'today' | 'yesterday' | '7d' | 'day'

/** since / until (UTC ISO) of a day choice, in Pakistan days. */
export function activityWindow(when: ActivityWhen, day: string, now: Date = new Date()): { since: string; until: string } {
  const today = pakistanDay(now)
  if (when === 'day' && day) return { since: pktMidnight(day), until: pktMidnight(addDays(day, 1)) }
  if (when === 'yesterday') return { since: pktMidnight(addDays(today, -1)), until: pktMidnight(today) }
  if (when === '7d') return { since: pktMidnight(addDays(today, -6)), until: pktMidnight(addDays(today, 1)) }
  return { since: pktMidnight(today), until: pktMidnight(addDays(today, 1)) }
}

/** The last 14 Pakistan days, newest first (the day picker). */
export function keptDays(now: Date = new Date()): string[] {
  const today = pakistanDay(now)
  return Array.from({ length: 14 }, (_, index) => addDays(today, -index))
}

export interface ActivityQuery {
  show?: 'all' | 'problems'
  topics?: string[]
  since?: string
  until?: string
  before?: string | null
  after?: number | null
  limit?: number
}

export function activityPath(connectorId: string, query: ActivityQuery): string {
  const params = new URLSearchParams()
  if (query.show && query.show !== 'all') params.set('show', query.show)
  for (const topic of query.topics || []) params.append('topic', topic)
  if (query.since) params.set('since', query.since)
  if (query.until) params.set('until', query.until)
  if (query.before) params.set('before', query.before)
  if (query.after != null) params.set('after', String(query.after))
  params.set('limit', String(query.limit ?? 50))
  return `/api/v1/devices/${encodeURIComponent(connectorId)}/activity?${params.toString()}`
}

/** Thrown when the backend has no /activity yet (older deploy): fall back to /logs. */
export class ActivityMissing extends Error {}

export async function fetchActivity(connectorId: string, query: ActivityQuery, signal?: AbortSignal): Promise<ActivityPage> {
  try {
    const answer = await api<Partial<ActivityPage>>(activityPath(connectorId, query), { signal })
    if (!answer || !Array.isArray(answer.items)) {
      return { items: [], next_cursor: null, newest_id: null, counts: null }
    }
    return {
      items: answer.items,
      next_cursor: answer.next_cursor ?? null,
      newest_id: answer.newest_id ?? null,
      counts: answer.counts ?? null,
      window: answer.window,
      retention_days: answer.retention_days,
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && !/device|connector/i.test(error.message)) throw new ActivityMissing('activity missing')
    throw error
  }
}

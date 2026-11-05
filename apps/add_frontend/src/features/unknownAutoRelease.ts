import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../api'
import { queryClient } from '../data'

// Automatic release of "Unknown identity · CNIC on synced user" punches. The
// server keeps the switch, the saved backlog count and the progress totals; a
// poll reads only those (no backlog scan), so every 10 s is cheap.
export const AUTO_RELEASE_PATH = '/api/v2/unknown-identity-auto-release'
export const autoReleaseQueryKey = ['unknown-identity-auto-release'] as const
export const AUTO_RELEASE_POLL_MS = 10_000

export type AutoReleaseSkip = { code: string; reason: string; count: number }
export type AutoReleaseOutcome = 'STARTED' | 'ALREADY_RUNNING' | 'ALREADY_AUTOMATIC'

export type AutoReleaseStatus = {
  allowed: boolean
  enabled: boolean
  active: boolean
  changed_by: string | null
  changed_at: string | null
  reason: string | null
  drain_active: boolean
  drain_requested_at: string | null
  drain_requested_by: string | null
  drain_completed_at: string | null
  /** After a full pass: older held punches are read again from then (new ones meanwhile). */
  resting_until: string | null
  backlog: { count: number | null; counted_at: string | null; remaining_estimate: number | null }
  progress: {
    ready: number
    waiting_oracle: number
    in_flight: number
    queued_total: number
    confirmed_total: number
    confirmed_last_hour: number
    skipped_total: number
    skipped_window_hours: number
    skipped_by_reason: AutoReleaseSkip[]
    needs_review: number
    checked_total: number
    counted_at: string | null
  }
  rate_per_min: number | null
  eta_minutes: number | null
  last_batch_at: string | null
  last_batch_size: number
  last_batch_job_id: string | null
  batches_created: number
  punches_submitted: number
  batch_size: number
  min_interval_seconds: number
  in_flight_limit: number
  backpressure: boolean
  last_error_code: string | null
  last_error_at: string | null
  server_time: string | null
  outcome?: AutoReleaseOutcome
}

type Loose = Record<string, unknown>
const record = (value: unknown): Loose => (value && typeof value === 'object' && !Array.isArray(value) ? value as Loose : {})
const text = (value: unknown) => (typeof value === 'string' && value ? value : null)
const maybeNumber = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : null)
const count = (value: unknown) => Math.max(0, maybeNumber(value) ?? 0)

/** The server's answer, or null when it is not an automatic release status at all. */
export function normalizeAutoReleaseStatus(raw: unknown): AutoReleaseStatus | null {
  const value = record(raw)
  if (typeof value.enabled !== 'boolean') return null
  const backlog = record(value.backlog)
  const progress = record(value.progress)
  const ready = count(progress.ready)
  const waiting = count(progress.waiting_oracle)
  const outcome = value.outcome
  return {
    allowed: value.allowed !== false,
    enabled: value.enabled,
    active: value.active === true,
    changed_by: text(value.changed_by),
    changed_at: text(value.changed_at),
    reason: text(value.reason),
    drain_active: value.drain_active === true,
    drain_requested_at: text(value.drain_requested_at),
    drain_requested_by: text(value.drain_requested_by),
    drain_completed_at: text(value.drain_completed_at),
    resting_until: text(value.resting_until),
    backlog: {
      count: maybeNumber(backlog.count),
      counted_at: text(backlog.counted_at),
      remaining_estimate: maybeNumber(backlog.remaining_estimate),
    },
    progress: {
      ready,
      waiting_oracle: waiting,
      in_flight: maybeNumber(progress.in_flight) ?? ready + waiting,
      queued_total: count(progress.queued_total),
      confirmed_total: count(progress.confirmed_total),
      confirmed_last_hour: count(progress.confirmed_last_hour),
      skipped_total: count(progress.skipped_total),
      skipped_window_hours: maybeNumber(progress.skipped_window_hours) ?? 24,
      skipped_by_reason: (Array.isArray(progress.skipped_by_reason) ? progress.skipped_by_reason : [])
        .map(record)
        .filter((item) => typeof item.code === 'string')
        .map((item) => ({ code: String(item.code), reason: text(item.reason) || String(item.code), count: count(item.count) })),
      needs_review: count(progress.needs_review),
      checked_total: count(progress.checked_total),
      counted_at: text(progress.counted_at),
    },
    rate_per_min: maybeNumber(value.rate_per_min),
    eta_minutes: maybeNumber(value.eta_minutes),
    last_batch_at: text(value.last_batch_at),
    last_batch_size: count(value.last_batch_size),
    last_batch_job_id: text(value.last_batch_job_id),
    batches_created: count(value.batches_created),
    punches_submitted: count(value.punches_submitted),
    batch_size: count(value.batch_size),
    min_interval_seconds: count(value.min_interval_seconds),
    in_flight_limit: count(value.in_flight_limit),
    backpressure: value.backpressure === true,
    last_error_code: text(value.last_error_code),
    last_error_at: text(value.last_error_at),
    server_time: text(value.server_time),
    outcome: outcome === 'STARTED' || outcome === 'ALREADY_RUNNING' || outcome === 'ALREADY_AUTOMATIC' ? outcome : undefined,
  }
}

async function fetchAutoReleaseStatus(signal?: AbortSignal) {
  try {
    return normalizeAutoReleaseStatus(await api<unknown>(AUTO_RELEASE_PATH, { signal }))
  } catch (failure) {
    // Not an administrator, or an older ADD without this switch: show nothing.
    if (failure instanceof ApiError && (failure.status === 403 || failure.status === 404)) return null
    throw failure
  }
}

/** Polls the switch and its progress every 10 s while `enabled` (paused in a hidden tab). */
export function useUnknownAutoReleaseStatus(enabled = true) {
  return useQuery({
    queryKey: autoReleaseQueryKey,
    queryFn: ({ signal }) => fetchAutoReleaseStatus(signal),
    enabled,
    refetchInterval: AUTO_RELEASE_POLL_MS,
    staleTime: 5_000,
  }, queryClient)
}

export const storeAutoReleaseStatus = (status: AutoReleaseStatus | null) => {
  if (status) queryClient.setQueryData(autoReleaseQueryKey, status)
}

// Ages are measured against the server's clock, so a skewed PC clock cannot
// make a fresh count look old.
export function secondsSince(value: string | null, now: string | null) {
  if (!value) return null
  const end = now ? Date.parse(now) : Date.now()
  const start = Date.parse(value)
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null
  return Math.max(0, Math.round((end - start) / 1000))
}

export function formatAgo(seconds: number | null) {
  if (seconds === null) return 'not yet'
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${seconds} s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 48 * 3600) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86_400)} days ago`
}

export function formatDuration(minutes: number) {
  if (minutes < 1) return 'under a minute'
  if (minutes < 60) return `${Math.round(minutes)} min`
  const totalMinutes = Math.round(minutes)
  const hours = Math.floor(totalMinutes / 60)
  if (hours < 48) {
    const rest = totalMinutes % 60
    return rest ? `${hours} h ${rest} min` : `${hours} h`
  }
  const days = Math.floor(hours / 24)
  const restHours = hours % 24
  return restHours ? `${days} days ${restHours} h` : `${days} days`
}

export const punchCount = (value: number) => `${value.toLocaleString()} ${value === 1 ? 'punch' : 'punches'}`

/** What is still to release: the server's estimate, else the raw held count. */
export const remainingOf = (status: AutoReleaseStatus) => status.backlog.remaining_estimate ?? status.backlog.count

export function etaText(status: AutoReleaseStatus) {
  const remaining = remainingOf(status)
  if (remaining === 0 || status.eta_minutes === 0) return 'nothing left to release'
  if (status.eta_minutes === null) return status.rate_per_min === null ? 'speed is measured after the first count' : 'no confirmations in the last hour yet'
  return `about ${formatDuration(status.eta_minutes)} left`
}

export type AutoReleaseTone = 'positive' | 'warning' | 'neutral'

/** One line for the Fleet tile: state, tone and the few numbers that matter. */
export function autoReleaseHeadline(status: AutoReleaseStatus): { value: string; tone: AutoReleaseTone; detail: string } {
  const remaining = remainingOf(status)
  const left = remaining === null ? 'counting' : `${remaining.toLocaleString()} left`
  const flow = `${status.progress.in_flight.toLocaleString()} waiting for Oracle · ${status.progress.confirmed_last_hour.toLocaleString()} confirmed last hour`
  if (!status.allowed) {
    return { value: 'Blocked', tone: 'warning', detail: 'Turned off for this ADD deployment; nothing is released automatically.' }
  }
  if (status.enabled) {
    return { value: 'ON', tone: 'positive', detail: `${left} · ${flow}${status.eta_minutes ? ` · ${etaText(status)}` : ''}` }
  }
  if (status.drain_active) {
    return { value: 'Releasing all', tone: 'neutral', detail: `Switch OFF, one-time release running · ${left} · ${flow}` }
  }
  const since = status.changed_at ? ` ${formatAgo(secondsSince(status.changed_at, status.server_time))}` : ''
  return {
    value: 'OFF',
    tone: 'warning',
    detail: `Turned off${status.changed_by ? ` by ${status.changed_by}` : ''}${since} · ${remaining === null ? 'backlog not counted yet' : `${remaining.toLocaleString()} held`}`,
  }
}

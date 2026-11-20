import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { idempotency } from '../App'
import { DivisionTag } from '../ui'
import { deviceName } from '../lib/deviceNames'
import type { Device } from '../types'
import { checkins, skipWords } from './attendance/attendanceWords'

export type UnknownSyncedSummary = {
  count: number
  devices: Array<{ connector_id: string; display_name: string; zone_id: string; count: number }>
}
type QuickRun = {
  job_id: string; selected: number; ready: number; skipped: number;
  not_matching: number; skipped_by_reason: Record<string, number>;
}
export type QuickOutcome = { sent: number; skipped: number; notMatching: number; reasons: Record<string, number>; failed: string }

const root = '/api/v2/attendance-direct-ords'
// Each request is one audited run; ADD checks every punch before it is queued.
const CHUNK = 200
// While ADD's worker queues earlier runs it can answer ADD_BUSY (or a proxy a
// 502-504); the same key is sent again, and ADD returns a run it already saved.
const BUSY_RETRIES = 5
const BUSY_DELAY_MS = 3000
const retryable = (failure: unknown) => failure instanceof ApiError &&
  (failure.code === 'ADD_BUSY' || [502, 503, 504].includes(failure.status))
const pause = (milliseconds: number) => new Promise((resolve) => setTimeout(resolve, milliseconds))
// A stop message from the server is not shown: these are the plain reasons.
const stopText = (failure: unknown) => {
  if (failure instanceof ApiError && [502, 503, 504].includes(failure.status)) return "the server didn't answer"
  if (failure instanceof ApiError && failure.status === 403) return "you don't have permission"
  return "the server couldn't save it"
}

/**
 * One click sends a chosen set of "not matched · CNIC found on the machine"
 * check-ins (the owner's one-click rule, 23 Oct): no reason, no password. The
 * server's own checks still run per check-in; anything that needs a person is
 * not sent and stays for "Send by hand". The request body is exactly
 * `{event_ids, idempotency_key}`, and a busy retry reuses the same key.
 */
export function useQuickRelease({ clearSelection, refresh }: { clearSelection: () => void; refresh: () => void }) {
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [outcome, setOutcome] = useState<QuickOutcome | null>(null)

  const release = async (ids: number[]) => {
    if (!ids.length || progress) return
    const result: QuickOutcome = { sent: 0, skipped: 0, notMatching: 0, reasons: {}, failed: '' }
    setOutcome(null); setProgress({ done: 0, total: ids.length })
    for (let start = 0; start < ids.length; start += CHUNK) {
      const chunk = ids.slice(start, start + CHUNK)
      const key = idempotency('quick-release')
      const send = async (attempt: number): Promise<QuickRun> => {
        try {
          return await api<QuickRun>(`${root}/quick-release`, {
            method: 'POST', body: JSON.stringify({ event_ids: chunk, idempotency_key: key }),
          })
        } catch (failure) {
          if (attempt >= BUSY_RETRIES || !retryable(failure)) throw failure
          await pause(BUSY_DELAY_MS)
          return send(attempt + 1)
        }
      }
      try {
        const run = await send(1)
        result.sent += run.ready
        result.skipped += run.skipped
        result.notMatching += run.not_matching
        Object.entries(run.skipped_by_reason || {}).forEach(([code, count]) => { result.reasons[code] = (result.reasons[code] || 0) + count })
      } catch (failure) {
        // A chunk whose punches were all released meanwhile is not a failure.
        if (failure instanceof ApiError && failure.code === 'NOTHING_TO_RELEASE') result.notMatching += chunk.length
        else { result.failed = stopText(failure); break }
      }
      setProgress({ done: Math.min(ids.length, start + chunk.length), total: ids.length })
    }
    setProgress(null); setOutcome(result); clearSelection(); refresh()
  }

  return { release, progress, outcome, dismiss: () => setOutcome(null) }
}

/** "968 on the way to the HR system · 64 not sent" and why. */
export function QuickReleaseOutcome({ outcome }: { outcome: QuickOutcome }) {
  return <div className="attendance-direct-progress" role="status" aria-live="polite">
    <div className="attendance-direct-counts"><span><strong>{outcome.sent.toLocaleString()}</strong> on the way to the HR system</span><span><strong>{outcome.skipped.toLocaleString()}</strong> not sent</span>{outcome.notMatching > 0 && <span><strong>{outcome.notMatching.toLocaleString()}</strong> already sent</span>}</div>
    {outcome.skipped > 0 && <p>Not sent (still not matched): {Object.entries(outcome.reasons).map(([code, value]) => `${checkins(value)} ${skipWords(code)}`).join('; ')}. Open a check-in to choose what to do.</p>}
    {outcome.failed && <p className="attention-copy">Stopped: {outcome.failed}. Check-ins already on the way will still arrive.</p>}
  </div>
}

type Place = { id: string; name: string; division: 'life' | 'health'; count: number }

/** The per-place count, grouped "Life: … · Health: …" (loaded on demand: it is a heavy count). */
export function UnknownSyncedPlaces({ devices, active }: { devices: Device[]; active: boolean }) {
  const [summary, setSummary] = useState<UnknownSyncedSummary | null>(null)
  const [error, setError] = useState('')
  const load = useCallback(async () => {
    try {
      const value = await api<UnknownSyncedSummary>(`${root}/unknown-synced`)
      setSummary({ count: typeof value.count === 'number' ? value.count : 0, devices: Array.isArray(value.devices) ? value.devices : [] })
      setError('')
    } catch { setError("Couldn't count these check-ins. Try again.") }
  }, [])
  useEffect(() => { if (active) void load() }, [active, load])
  if (error) return <p className="message pattern-blocked" role="alert">{error} <button className="text-button" type="button" onClick={() => void load()}>Try again</button></p>
  if (!summary) return active ? <p className="attendance-unknown-synced-devices" role="status">Counting…</p> : null
  const byId = new Map(devices.map((device) => [device.connector_id, device]))
  const places: Place[] = summary.devices.map((item) => {
    const known = byId.get(item.connector_id)
    const parsed = deviceName(known?.display_name || item.display_name || item.zone_id)
    return {
      id: item.connector_id,
      name: known?.friendly_name || (parsed.unit ? `${parsed.location} · ${parsed.unit}` : parsed.location),
      division: known?.division === 'life' || known?.division === 'health' ? known.division : parsed.zone,
      count: item.count,
    }
  })
  const shown = places.slice(0, 8)
  const groups = (['life', 'health'] as const).map((division) => ({ division, items: shown.filter((place) => place.division === division) })).filter((group) => group.items.length)
  return <div className="attendance-unknown-synced-devices">
    <p><strong>{checkins(summary.count)}</strong> not matched with a CNIC found on the machine.</p>
    {groups.length > 0 && <ul>{groups.map((group) => <li key={group.division}><DivisionTag division={group.division} /> {group.items.map((place) => `${place.name} (${place.count.toLocaleString()})`).join(', ')}</li>)}</ul>}
    {places.length > 8 && <small>and {places.length - 8} more places</small>}
  </div>
}

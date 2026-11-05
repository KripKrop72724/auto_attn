import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { idempotency } from '../App'
import { humanizeStatus } from '../status'
import { UnknownIdentityAutoRelease } from './UnknownIdentityAutoRelease'

export type UnknownSyncedSummary = {
  count: number
  devices: Array<{ connector_id: string; display_name: string; zone_id: string; count: number }>
}
type QuickRun = {
  job_id: string; selected: number; ready: number; skipped: number;
  not_matching: number; skipped_by_reason: Record<string, number>;
}
type Outcome = { sent: number; skipped: number; notMatching: number; reasons: Record<string, number>; failed: string }

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
const punches = (value: number) => `${value.toLocaleString()} ${value === 1 ? 'punch' : 'punches'}`
const SKIP_TEXT: Record<string, string> = {
  CNIC_CHOICE_REQUIRED: 'need a CNIC decision',
  CNIC_CONFLICT: 'another CNIC or person on record',
  PUNCH_TIME_CNIC_HELD_BY_OTHER: 'punch-time CNIC held by another user',
  IDENTITY_CONFLICT_UNRESOLVED: 'duplicate CNIC not resolved',
  CLOCK_INVALID: 'invalid terminal clock',
  TIME_IMPOSSIBLE: 'impossible punch time',
  INVALID_EVENT_UID: 'older punch ID needs repair',
  ALREADY_CONFIRMED: 'already in Oracle',
  ALREADY_DELIVERING: 'already being delivered',
  UNKNOWN_USER: 'terminal user not found',
  CNIC_MISSING: 'synced user has no CNIC',
}
const skipLabel = (code: string) => SKIP_TEXT[code] || humanizeStatus(code).toLowerCase()

/**
 * One click lists every held "Unknown identity · CNIC on synced user" punch.
 * Releasing all of them is server-side (the automatic release switch, or its
 * one-shot "Release all now"); a manual selection is still sent from here. No
 * reason or password: ADD's direct send checks still run per punch, and
 * anything that needs a person stays held for the per-punch "Send to ORDS".
 *
 * The switch and its progress always show: they read only the worker's saved
 * counts. The heavy live filter (and its count) runs only when the operator
 * turns the filter on.
 */
export function UnknownSyncedRelease({ active, onToggle, selectedIds, clearSelection, refresh, toast }: {
  active: boolean; onToggle: () => void; selectedIds: number[]; clearSelection: () => void; refresh: () => void;
  toast?: { notice: (message: string) => void; error: (message: string) => void };
}) {
  const [summary, setSummary] = useState<UnknownSyncedSummary | null>(null)
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [error, setError] = useState('')
  const load = useCallback(async () => {
    try {
      const value = await api<UnknownSyncedSummary>(`${root}/unknown-synced`)
      setSummary({ count: typeof value.count === 'number' ? value.count : 0, devices: Array.isArray(value.devices) ? value.devices : [] })
      setError('')
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'ADD could not count these punches.') }
  }, [])
  // Counted when the operator asks: the count is too heavy for every ledger view.
  useEffect(() => { if (active) void load() }, [active, load])

  const release = async (ids: number[]) => {
    if (!ids.length || progress) return
    const result: Outcome = { sent: 0, skipped: 0, notMatching: 0, reasons: {}, failed: '' }
    setOutcome(null); setError(''); setProgress({ done: 0, total: ids.length })
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
        else { result.failed = failure instanceof Error ? failure.message : 'ADD could not save this release.'; break }
      }
      setProgress({ done: Math.min(ids.length, start + chunk.length), total: ids.length })
    }
    setProgress(null); setOutcome(result); clearSelection(); refresh(); void load()
  }

  const count = summary?.count ?? 0
  return <section className={`attendance-unknown-synced ${active ? 'is-active' : ''}`} aria-label="Unknown identity with a CNIC on the synced user">
    <div className="attendance-unknown-synced-bar">
      <button className={`button ${active ? 'primary' : 'secondary'}`} type="button" aria-pressed={active} onClick={onToggle}>
        Unknown identity · CNIC on synced user{active && <span className="attendance-unknown-synced-count">{summary ? count.toLocaleString() : '…'}</span>}
      </button>
      {active && (selectedIds.length > 0 || progress) && <div className="attendance-direct-actions">
        <button className="button secondary" type="button" disabled={Boolean(progress)} onClick={() => void release(selectedIds)}>{progress ? `Releasing ${progress.done.toLocaleString()} of ${progress.total.toLocaleString()}…` : `Release ${punches(selectedIds.length)} selected`}</button>
      </div>}
    </div>
    {active && summary && summary.devices.length > 0 && <p className="attendance-unknown-synced-devices">{summary.devices.slice(0, 8).map((device) => `${device.display_name}: ${device.count.toLocaleString()}`).join(' · ')}{summary.devices.length > 8 ? ` · and ${summary.devices.length - 8} more devices` : ''}</p>}
    <UnknownIdentityAutoRelease toast={toast} onChanged={refresh} />
    {error && <p className="message pattern-blocked" role="alert">{error}</p>}
    {outcome && <div className="attendance-direct-progress" role="status" aria-live="polite">
      <div className="attendance-direct-counts"><span><strong>{outcome.sent.toLocaleString()}</strong> queued for Oracle</span><span><strong>{outcome.skipped.toLocaleString()}</strong> skipped</span>{outcome.notMatching > 0 && <span><strong>{outcome.notMatching.toLocaleString()}</strong> already released</span>}</div>
      {outcome.skipped > 0 && <p>Skipped punches stay held: {Object.entries(outcome.reasons).map(([code, value]) => `${punches(value)} ${skipLabel(code)}`).join('; ')}. Use "Send to ORDS" on a punch to decide it.</p>}
      {outcome.failed && <p className="attention-copy">Stopped: {outcome.failed} Punches sent before this stay queued.</p>}
    </div>}
  </section>
}

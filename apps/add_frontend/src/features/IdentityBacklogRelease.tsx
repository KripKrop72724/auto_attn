import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { api, ApiError, queryString } from '../api'
import { dateTime, Dialog, idempotency, Metric } from '../App'
import { Icon } from '../Icon'
import type { Device } from '../types'
import './SafeAttendanceRepair.css'
import './IdentityBacklogRelease.css'

type Group = { connector_id: string; source: string; age: string; held: number; provable: number; next_snapshot: number }
type Scope = { zone_id: string; as_of: string; until_id: number; window_hours: number; connectors: string[] }
type Approval = { scope: Scope; expires_at: string; signature: string | null }
type PreviewPage = {
  zone_id: string; scanned: number; next_after_id: number | null; groups: Group[]; as_of: string; until_id: number
  window_hours: number; release_enabled: boolean; automatic_zone_enabled: boolean
  devices: Array<{ connector_id: string; name: string | null }>; held_total?: number; reheld_outside_check?: number
  approval?: Approval
}
type Check = {
  zone: string; groups: Group[]; windowHours: number; approval: Approval; releaseEnabled: boolean
  automatic: boolean; names: Record<string, string>; reheld: number
}
export type BacklogRun = {
  job_id: string; zone_id: string; status: string; paused: boolean; waiting_reason: string | null; actor: string
  reason: string; connectors: string[]; checked_at: string | null; approved: number; released: number; delivered: number
  waiting_oracle: number; kept_held: number; left_to_snapshot: number; no_longer_held?: number; needs_review: number; created_at: string
  updated_at: string; completed_at: string | null
}
type Toast = { notice: (text: string) => void; error: (text: string) => void }

const root = '/api/v2/attendance-identity-backlog'
const pageSize = 200
const busyRetries = 20
const sources: Record<string, string> = { DUMP_RECONNECT: 'Reconnect recovery', DUMP_STARTUP: 'Startup recovery' }
const ageOrder = ['AUTOMATIC_WINDOW', 'UP_TO_7_DAYS', 'UP_TO_30_DAYS', 'OLDER']
const ageLabel = (age: string, hours: number, automatic: boolean) => ({
  AUTOMATIC_WINDOW: `Under ${hours} hours${automatic ? ' (often released automatically)' : ''}`,
  UP_TO_7_DAYS: 'Up to 7 days', UP_TO_30_DAYS: '7 to 30 days', OLDER: 'Over 30 days',
}[age] || age)
const runLabel: Record<string, string> = {
  RELEASING: 'Releasing in batches', STOPPED_FOLLOWING: 'Stopped: following released punches to Oracle',
  COMPLETED: 'Finished', STOPPED: 'Stopped',
}
const active = new Set(['RELEASING', 'STOPPED_FOLLOWING'])
const waitingLabel: Record<string, string> = {
  WAITING_FOR_COMPLETE_ROSTER: 'Waiting for a complete user list from a chosen terminal before checking more punches.',
  WAITING_FOR_SERIAL_CONFIRMATION: 'Waiting for a chosen terminal’s serial number to be confirmed before checking more punches.',
  WAITING_FOR_ROSTER_UPDATE: 'A terminal sent a new user list during the last batch. ADD checks those punches again.',
}
const punches = (count: number) => `${count.toLocaleString()} ${count === 1 ? 'punch' : 'punches'}`
const message = (error: unknown) => error instanceof Error ? error.message : 'Could not reach ADD. Please try again.'
const groupKey = (group: Pick<Group, 'connector_id' | 'source' | 'age'>) => `${group.connector_id}|${group.source}|${group.age}`
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

export function IdentityBacklogRelease({ devices, toast }: { devices: Device[]; toast: Toast }) {
  const zones = useMemo(() => [...new Map(
    devices.filter((device) => device.firmware_family !== 'hikvision' && !device.is_spare)
      .map((device) => [device.zone_id, device.zone_name || device.zone_id] as const),
  ).entries()].sort((a, b) => a[1].localeCompare(b[1])), [devices])
  const [zone, setZone] = useState('')
  const [checking, setChecking] = useState(false)
  const [progress, setProgress] = useState({ scanned: 0, total: 0 })
  const [result, setResult] = useState<Check | null>(null)
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [run, setRun] = useState<BacklogRun | null>(null)
  const [dialog, setDialog] = useState<'START' | 'STOP' | null>(null)
  const [reason, setReason] = useState('')
  const [typed, setTyped] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const stopChecking = useRef(false)
  const approvalKey = useRef(idempotency('identity-backlog'))
  const confirmation = `RELEASE ${zone}`

  const loadRun = useCallback(async (zoneId: string) => {
    const response = await api<{ rows: BacklogRun[] }>(`${root}/releases${queryString({ zone_id: zoneId, limit: 1 })}`)
    setRun(response.rows[0] || null)
  }, [])
  useEffect(() => {
    setResult(null); setRun(null); setError(''); setChosen(new Set())
    if (zone) void loadRun(zone).catch((failure) => setError(message(failure)))
  }, [zone, loadRun])
  useEffect(() => {
    if (!run || !active.has(run.status)) return
    let stopped = false
    const timer = setInterval(() => {
      void api<BacklogRun>(`${root}/releases/${encodeURIComponent(run.job_id)}`)
        .then((value) => { if (!stopped) setRun(value) }).catch(() => undefined)
    }, 3000)
    return () => { stopped = true; clearInterval(timer) }
  }, [run?.job_id, run?.status])

  const page = async (params: Record<string, string | number>) => {
    // One zone check page runs at a time across ADD; wait for another to finish.
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await api<PreviewPage>(`${root}/preview${queryString({ zone_id: zone, limit: pageSize, ...params })}`)
      } catch (failure) {
        if (!(failure instanceof ApiError && failure.code === 'PREVIEW_BUSY') || attempt >= busyRetries) throw failure
        await pause(1500)
      }
    }
  }

  const check = async () => {
    if (!zone || checking) return
    setChecking(true); setError(''); setResult(null); setChosen(new Set()); stopChecking.current = false
    const totals = new Map<string, Group>()
    let after = 0; let scanned = 0; let total = 0
    try {
      const first = await page({ after_id: 0 })
      const frozen = { as_of: first.as_of, until_id: first.until_id, window_hours: first.window_hours }
      for (let current = first; ;) {
        if (typeof current.held_total === 'number') total = current.held_total
        current.groups.forEach((group) => {
          const old = totals.get(groupKey(group)) || { ...group, held: 0, provable: 0, next_snapshot: 0 }
          totals.set(groupKey(group), {
            ...old, held: old.held + group.held, provable: old.provable + group.provable,
            next_snapshot: old.next_snapshot + (group.next_snapshot || 0),
          })
        })
        scanned += current.scanned
        setProgress({ scanned, total })
        if (current.next_after_id === null || stopChecking.current) break
        after = current.next_after_id
        current = await page({ after_id: after, ...frozen })
      }
      if (!stopChecking.current && first.approval) {
        const names = Object.fromEntries(first.devices.map((device) => [device.connector_id, device.name || device.connector_id]))
        const groups = [...totals.values()].filter((group) => group.held > 0).sort((a, b) => (names[a.connector_id] || '').localeCompare(names[b.connector_id] || '')
          || a.source.localeCompare(b.source) || ageOrder.indexOf(a.age) - ageOrder.indexOf(b.age))
        setResult({
          zone, groups, windowHours: first.window_hours, approval: first.approval, releaseEnabled: first.release_enabled,
          automatic: first.automatic_zone_enabled, names, reheld: first.reheld_outside_check || 0,
        })
        setChosen(new Set(groups.filter((group) => group.provable > 0).map(groupKey)))
      }
    } catch (failure) { setError(message(failure)) } finally { setChecking(false) }
  }

  const held = result?.groups.reduce((sum, group) => sum + group.held, 0) ?? 0
  const provable = result?.groups.reduce((sum, group) => sum + group.provable, 0) ?? 0
  const nextSnapshot = result?.groups.reduce((sum, group) => sum + group.next_snapshot, 0) ?? 0
  const selected = result?.groups.filter((group) => chosen.has(groupKey(group))) ?? []
  const selectedHeld = selected.reduce((sum, group) => sum + group.held, 0)
  const selectedProvable = selected.reduce((sum, group) => sum + group.provable, 0)
  const releasing = run?.status === 'RELEASING'
  const running = Boolean(run && active.has(run.status))
  const canApprove = Boolean(result?.releaseEnabled && result.approval.signature && selectedProvable && !running)
  const closeDialog = () => { if (!busy) { setDialog(null); setPassword(''); setTyped(''); setError('') } }
  const toggle = (key: string) => setChosen((old) => {
    const next = new Set(old)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    return next
  })

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !dialog) return
    setBusy(true); setError('')
    try {
      const value = dialog === 'START' && result
        ? await api<BacklogRun>(`${root}/releases`, { method: 'POST', body: JSON.stringify({
          zone_id: zone, reason: reason.trim(), typed_confirmation: typed.trim(), password,
          idempotency_key: approvalKey.current, scope: result.approval.scope, expires_at: result.approval.expires_at,
          signature: result.approval.signature,
          groups: selected.map(({ connector_id, source, age }) => ({ connector_id, source, age })),
          approved_count: selectedProvable, previewed_held: selectedHeld,
        }) })
        : await api<BacklogRun>(`${root}/releases/${encodeURIComponent(run?.job_id || '')}/stop`, {
          method: 'POST', body: JSON.stringify({ password }),
        })
      setRun(value); setDialog(null); setPassword(''); setTyped(''); setReason('')
      approvalKey.current = idempotency('identity-backlog')
      toast.notice(dialog === 'START'
        ? 'Approval saved. ADD is releasing the proven punches in small batches.'
        : 'Release stopped. No more punches will be released; ADD keeps following the released ones to Oracle.')
    } catch (failure) { setError(message(failure)) } finally { setBusy(false) }
  }

  return <section className="panel safe-repair identity-backlog" aria-label="Release older recovery holds">
    <header className="safe-repair-header"><div>
      <h2>Older recovery holds</h2>
      <p>Punches from reconnect and startup recovery reads that reached ADD before the next user list. In zones you enable on the server, ADD releases recent ones automatically when it saw the terminal’s user list shortly before and after the punch. Older ones, and punches from a longer outage, need your approval.</p>
    </div><span className="badge"><Icon name="shield" /> Owner approval</span></header>
    <div className="safe-repair-body">
      <p>This release uses one proof only: the punch’s terminal user ID, UID and fingerprint match one saved CNIC history for that employee at punch time, and that CNIC is still the employee’s current CNIC. Punches another proof can release are left to the next user list. Every other punch stays held.</p>
      <div className="identity-backlog-controls">
        <label>Zone<select value={zone} onChange={(event) => setZone(event.target.value)}>
          <option value="">Select a zone</option>
          {zones.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select></label>
        <button className="button primary" type="button" disabled={!zone || checking} onClick={() => void check()}>
          {checking ? `Checking ${progress.scanned.toLocaleString()} of ${progress.total.toLocaleString()}` : 'Check zone'}
        </button>
        {checking && <button className="button secondary" type="button" onClick={() => { stopChecking.current = true }}>Stop checking</button>}
      </div>
      {error && !dialog && <p className="message pattern-blocked" role="alert">{error}</p>}
      {result && result.zone === zone && <>
        <div className="safe-repair-counts">
          <Metric label="Held recovery punches" value={held} icon="list" detail={`Checked ${dateTime(result.approval.scope.as_of)}`} />
          <Metric label="Can be released" value={provable} icon="shield" detail="Saved CNIC history matches" />
          {nextSnapshot > 0 && <Metric label="Next user list" value={nextSnapshot} icon="list" detail="Another proof releases these" />}
          <Metric label="Stay held" value={held - provable - nextSnapshot} icon="alert" detail="Need other evidence or review" tone={held - provable - nextSnapshot ? 'warning' : 'neutral'} />
        </div>
        {result.reheld > 0 && <p>{punches(result.reheld)} released earlier and held again are not part of this check. This release never takes them in.</p>}
        {!result.releaseEnabled && <p className="message pattern-blocked" role="alert">The release proof is turned off on this server. Nothing can be released.</p>}
        {result.groups.length > 0 ? <table className="identity-backlog-table">
          <caption>Choose the groups to release. Counts are frozen at the check.</caption>
          <thead><tr><th scope="col">Release</th><th scope="col">Terminal</th><th scope="col">Capture</th><th scope="col">Held for</th><th scope="col">Held</th><th scope="col">Can be released</th><th scope="col">Next user list</th></tr></thead>
          <tbody>{result.groups.map((group) => {
            const label = `${result.names[group.connector_id] || group.connector_id}, ${sources[group.source] || group.source}, ${ageLabel(group.age, result.windowHours, result.automatic)}`
            return <tr key={groupKey(group)}>
              <td><input type="checkbox" aria-label={`Release ${label}`} checked={chosen.has(groupKey(group))} disabled={!group.provable} onChange={() => toggle(groupKey(group))} /></td>
              <td>{result.names[group.connector_id] || group.connector_id}</td>
              <td>{sources[group.source] || group.source}</td><td>{ageLabel(group.age, result.windowHours, result.automatic)}</td>
              <td>{group.held.toLocaleString()}</td><td>{group.provable.toLocaleString()}</td><td>{group.next_snapshot.toLocaleString()}</td>
            </tr>
          })}</tbody>
        </table> : <p>No recovery punches are held in this zone.</p>}
        <button className="button primary" type="button" disabled={!canApprove} onClick={() => { setDialog('START'); setError('') }}>
          Release {punches(selectedProvable)}
        </button>
        {running && <p>A release for this zone is still running or following its punches to Oracle.</p>}
      </>}
      {run && <div className="safe-repair-run" role="status" aria-live="polite">
        <div className="safe-repair-heading"><h3>{run.paused ? 'Paused: release proof turned off' : runLabel[run.status] || run.status}</h3><span>Approved by {run.actor} · {dateTime(run.created_at)}</span></div>
        <p>{run.delivered.toLocaleString()} confirmed by Oracle · {run.waiting_oracle.toLocaleString()} waiting for Oracle · {run.released.toLocaleString()} of {run.approved.toLocaleString()} approved released · {run.kept_held.toLocaleString()} kept held{run.left_to_snapshot ? ` · ${run.left_to_snapshot.toLocaleString()} left to the next user list` : ''}{run.no_longer_held ? ` · ${run.no_longer_held.toLocaleString()} no longer held when reached` : ''}{run.needs_review ? ` · ${run.needs_review.toLocaleString()} need review` : ''}</p>
        {run.waiting_reason && <p>{waitingLabel[run.waiting_reason] || run.waiting_reason}</p>}
        <p>{run.reason}</p>
        {releasing && <div className="safe-repair-actions"><button className="button secondary" type="button" onClick={() => { setDialog('STOP'); setError('') }}>Stop</button><span>You can close this page. The release continues on ADD.</span></div>}
      </div>}
    </div>
    {dialog && <Dialog
      titleId="identity-backlog-title"
      title={dialog === 'START' ? `Release up to ${punches(selectedProvable)}` : 'Stop this release'}
      description={dialog === 'START'
        ? `Only punches of the ${selected.length === 1 ? 'chosen group' : `${selected.length} chosen groups`} that are held when you approve, and no more than ${punches(selectedProvable)}. ADD checks every punch again, with the same proof, as it releases it. A punch that no longer matches stays held.`
        : 'No more punches are released. Punches already released stay released: ADD keeps following them to Oracle and checks again any that delivery holds. The rest stay held.'}
      onClose={closeDialog}
    ><form className="dialog-body" onSubmit={submit}>
      {dialog === 'START' && <>
        <label>Reason for release<textarea required minLength={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
        <label>Type {confirmation} to confirm<input required value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" /></label>
      </>}
      <label>Administrator password<input type="password" required autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
      {error && <p className="message pattern-blocked" role="alert">{error}</p>}
      <footer className="dialog-actions">
        <button className="button secondary" type="button" disabled={busy} onClick={closeDialog}>Cancel</button>
        <button className="button primary" disabled={busy || !password || (dialog === 'START' && (reason.trim().length < 3 || typed.trim() !== confirmation))}>
          {busy ? 'Saving…' : dialog === 'START' ? `Approve ${punches(selectedProvable)}` : 'Stop release'}
        </button>
      </footer>
    </form></Dialog>}
  </section>
}

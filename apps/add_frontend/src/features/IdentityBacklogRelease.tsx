import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { api, queryString } from '../api'
import { dateTime, Dialog, idempotency, Metric } from '../App'
import { Icon } from '../Icon'
import type { Device } from '../types'
import './SafeAttendanceRepair.css'
import './IdentityBacklogRelease.css'

type Group = { source: string; age: string; held: number; provable: number }
type PreviewPage = {
  zone_id: string; scanned: number; next_after_id: number | null; groups: Group[]; window_hours: number; held_total?: number
}
export type BacklogRun = {
  job_id: string; zone_id: string; status: string; actor: string; reason: string; connectors: string[];
  held_at_approval: number; released: number; kept_held: number; created_at: string; updated_at: string; completed_at: string | null
}
type Toast = { notice: (text: string) => void; error: (text: string) => void }

const root = '/api/v2/attendance-identity-backlog'
const pageSize = 500
const sources: Record<string, string> = { DUMP_RECONNECT: 'Reconnect recovery', DUMP_STARTUP: 'Startup recovery' }
const ageOrder = ['AUTOMATIC_WINDOW', 'UP_TO_7_DAYS', 'UP_TO_30_DAYS', 'OLDER']
const ageLabel = (age: string, hours: number) => ({
  AUTOMATIC_WINDOW: `Under ${hours} hours (released automatically)`,
  UP_TO_7_DAYS: 'Up to 7 days', UP_TO_30_DAYS: '7 to 30 days', OLDER: 'Over 30 days',
}[age] || age)
const runLabel: Record<string, string> = { RELEASING: 'Releasing in batches', COMPLETED: 'Finished', STOPPED: 'Stopped' }
const punches = (count: number) => `${count.toLocaleString()} ${count === 1 ? 'punch' : 'punches'}`
const message = (error: unknown) => error instanceof Error ? error.message : 'Could not reach ADD. Please try again.'

export function IdentityBacklogRelease({ devices, toast }: { devices: Device[]; toast: Toast }) {
  const zones = useMemo(() => [...new Map(
    devices.filter((device) => device.firmware_family !== 'hikvision' && !device.is_spare)
      .map((device) => [device.zone_id, device.zone_name || device.zone_id] as const),
  ).entries()].sort((a, b) => a[1].localeCompare(b[1])), [devices])
  const [zone, setZone] = useState('')
  const [checking, setChecking] = useState(false)
  const [progress, setProgress] = useState({ scanned: 0, total: 0 })
  const [result, setResult] = useState<{ zone: string; groups: Group[]; windowHours: number } | null>(null)
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
    setResult(null); setRun(null); setError('')
    if (zone) void loadRun(zone).catch((failure) => setError(message(failure)))
  }, [zone, loadRun])
  useEffect(() => {
    if (!run || run.status !== 'RELEASING') return
    let stopped = false
    const timer = setInterval(() => {
      void api<BacklogRun>(`${root}/releases/${encodeURIComponent(run.job_id)}`)
        .then((value) => { if (!stopped) setRun(value) }).catch(() => undefined)
    }, 3000)
    return () => { stopped = true; clearInterval(timer) }
  }, [run?.job_id, run?.status])

  const check = async () => {
    if (!zone || checking) return
    setChecking(true); setError(''); setResult(null); stopChecking.current = false
    const totals = new Map<string, Group>()
    let after = 0; let scanned = 0; let total = 0; let windowHours = 72
    try {
      for (;;) {
        const page = await api<PreviewPage>(`${root}/preview${queryString({ zone_id: zone, after_id: after, limit: pageSize })}`)
        if (typeof page.held_total === 'number') total = page.held_total
        windowHours = page.window_hours
        page.groups.forEach((group) => {
          const key = `${group.source}:${group.age}`
          const old = totals.get(key) || { ...group, held: 0, provable: 0 }
          totals.set(key, { ...old, held: old.held + group.held, provable: old.provable + group.provable })
        })
        scanned += page.scanned
        setProgress({ scanned, total })
        if (page.next_after_id === null || stopChecking.current) break
        after = page.next_after_id
      }
      if (!stopChecking.current) {
        setResult({ zone, windowHours, groups: [...totals.values()].filter((group) => group.held > 0)
          .sort((a, b) => a.source.localeCompare(b.source) || ageOrder.indexOf(a.age) - ageOrder.indexOf(b.age)) })
      }
    } catch (failure) { setError(message(failure)) } finally { setChecking(false) }
  }

  const held = result?.groups.reduce((sum, group) => sum + group.held, 0) ?? 0
  const provable = result?.groups.reduce((sum, group) => sum + group.provable, 0) ?? 0
  const releasing = run?.status === 'RELEASING'
  const closeDialog = () => { if (!busy) { setDialog(null); setPassword(''); setTyped(''); setError('') } }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !dialog) return
    setBusy(true); setError('')
    try {
      const value = dialog === 'START'
        ? await api<BacklogRun>(`${root}/releases`, { method: 'POST', body: JSON.stringify({
          zone_id: zone, reason: reason.trim(), typed_confirmation: typed.trim(), password,
          idempotency_key: approvalKey.current,
        }) })
        : await api<BacklogRun>(`${root}/releases/${encodeURIComponent(run?.job_id || '')}/stop`, {
          method: 'POST', body: JSON.stringify({ password }),
        })
      setRun(value); setDialog(null); setPassword(''); setTyped(''); setReason('')
      approvalKey.current = idempotency('identity-backlog')
      toast.notice(dialog === 'START'
        ? 'Approval saved. ADD is releasing the proven punches in batches.'
        : 'Release stopped. Punches not yet released stay held.')
    } catch (failure) { setError(message(failure)) } finally { setBusy(false) }
  }

  return <section className="panel safe-repair identity-backlog" aria-label="Release older recovery holds">
    <header className="safe-repair-header"><div>
      <h2>Older recovery holds</h2>
      <p>Punches from reconnect and startup recovery reads that reached ADD before the next user list. ADD releases new ones automatically. Older ones need your approval.</p>
    </div><span className="badge"><Icon name="shield" /> Owner approval</span></header>
    <div className="safe-repair-body">
      <p>A punch is released only when its terminal user ID, UID and fingerprint match one saved CNIC history for that employee at punch time, and that CNIC is still the employee’s current CNIC. Every other punch stays held.</p>
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
          <Metric label="Held recovery punches" value={held} icon="list" detail="Checked in this zone" />
          <Metric label="Can be released" value={provable} icon="shield" detail="Saved CNIC history matches" />
          <Metric label="Stay held" value={held - provable} icon="alert" detail="Need other evidence or review" tone={held - provable ? 'warning' : 'neutral'} />
        </div>
        {result.groups.length > 0 ? <table className="identity-backlog-table">
          <caption>By capture type and how long ADD has held them</caption>
          <thead><tr><th scope="col">Capture</th><th scope="col">Held for</th><th scope="col">Held</th><th scope="col">Can be released</th></tr></thead>
          <tbody>{result.groups.map((group) => <tr key={`${group.source}:${group.age}`}>
            <td>{sources[group.source] || group.source}</td><td>{ageLabel(group.age, result.windowHours)}</td>
            <td>{group.held.toLocaleString()}</td><td>{group.provable.toLocaleString()}</td>
          </tr>)}</tbody>
        </table> : <p>No recovery punches are held in this zone.</p>}
        <button className="button primary" type="button" disabled={!provable || releasing} onClick={() => { setDialog('START'); setError('') }}>
          Release {punches(provable)}
        </button>
        {releasing && <p>A release for this zone is already running.</p>}
      </>}
      {run && <div className="safe-repair-run" role="status" aria-live="polite">
        <div className="safe-repair-heading"><h3>{runLabel[run.status] || run.status}</h3><span>Approved by {run.actor} · {dateTime(run.created_at)}</span></div>
        <p>{run.released.toLocaleString()} released · {run.kept_held.toLocaleString()} kept held · {run.held_at_approval.toLocaleString()} held at approval</p>
        <p>{run.reason}</p>
        {releasing && <div className="safe-repair-actions"><button className="button secondary" type="button" onClick={() => { setDialog('STOP'); setError('') }}>Stop</button><span>You can close this page. The release continues on ADD.</span></div>}
      </div>}
    </div>
    {dialog && <Dialog
      titleId="identity-backlog-title"
      title={dialog === 'START' ? `Release ${punches(provable)}` : 'Stop this release'}
      description={dialog === 'START'
        ? 'ADD checks every punch again, with the same proof, as it releases it. A punch that no longer matches stays held.'
        : 'Punches already released stay released. The rest stay held.'}
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
          {busy ? 'Saving…' : dialog === 'START' ? `Approve ${punches(provable)}` : 'Stop release'}
        </button>
      </footer>
    </form></Dialog>}
  </section>
}

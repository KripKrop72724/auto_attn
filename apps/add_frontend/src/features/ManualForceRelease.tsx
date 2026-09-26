import { useCallback, useEffect, useRef, useState } from 'react'
import { api, queryString } from '../api'
import { dateTime, Dialog, idempotency, Metric, pktInputToUtc } from '../App'
import { Icon } from '../Icon'
import type { AttendanceEvent, Device } from '../types'
import './SafeAttendanceRepair.css'
import './ManualForceRelease.css'

type Toast = { notice: (text: string) => void; error: (text: string) => void }
type Counts = { checked: number; ready: number; review: number; attention?: number; waiting: number; confirmed: number; stopped: number; skipped: number; already_confirmed: number; already_delivering: number; unavailable_devices: number }
export type ForceRun = {
  job_id: string; status: string; actor: string; counts: Counts; signature: string | null;
  execution_enabled: boolean; created_at: string; updated_at: string; expires_at: string | null; last_error: string | null;
  devices: Array<{ connector_id: string; name: string; serial: string; status: string; checked: number; ready: number; synced_at: string | null; error: string | null }>
}
type Item = { id: number; status: string; needs_attention?: boolean; reason: string; name: string | null; user_id: string; device_name: string; device_serial: string; time: string }
type Page = { rows: Item[]; next_cursor: number | null }
const punchCount = (count: number) => `${count.toLocaleString()} ${count === 1 ? 'punch' : 'punches'}`
const root = '/api/v2/attendance-force-releases'
const finished = new Set(['COMPLETED', 'COMPLETED_WITH_REVIEW', 'STOPPED', 'EXPIRED'])
const label: Record<string, string> = {
  CHECKING: 'Checking punches', CHECKED: 'Ready to review', RUNNING: 'Preparing delivery', WAITING_ORACLE: 'Waiting for Oracle',
  PAUSED: 'Paused', STOPPING: 'Stopping — accounting for queued deliveries', STOPPED: 'Stopped', EXPIRED: 'Check expired',
  COMPLETED: 'Finished', COMPLETED_WITH_REVIEW: 'Finished — some punches need attention', BASELINE: 'Waiting',
  SYNC_PENDING: 'Waiting', SYNCING: 'Syncing', RELEASING: 'Preparing delivery', DONE: 'Prepared', UNAVAILABLE: 'Couldn’t sync',
  SKIPPED: 'Skipped after recheck', READY: 'Ready to release', NEEDS_REVIEW: 'Cannot release', CONFIRMED: 'Confirmed by Oracle', EXCLUDED: 'Excluded',
}
const message = (error: unknown) => error instanceof Error ? error.message : 'Could not reach ADD. Please try again.'

export function ManualForceRelease({ devices, toast, initialConnectorId }: { devices: Device[]; toast: Toast; initialConnectorId?: string | null }) {
  const [enabled, setEnabled] = useState(false)
  const [scope, setScope] = useState<'SELECTED' | 'ALL_PAKISTAN'>('SELECTED')
  const [selected, setSelected] = useState<string[]>(initialConnectorId ? [initialConnectorId] : [])
  const [deviceSearch, setDeviceSearch] = useState('')
  const [allDates, setAllDates] = useState(true)
  const [from, setFrom] = useState(''); const [to, setTo] = useState('')
  const [run, setRun] = useState<ForceRun | null>(null)
  const [history, setHistory] = useState<ForceRun[]>([])
  const [historyCursor, setHistoryCursor] = useState<number | null>(null)
  const [items, setItems] = useState<Page>({ rows: [], next_cursor: null })
  const [filter, setFilter] = useState(''); const [search, setSearch] = useState('')
  const [cursor, setCursor] = useState(0); const [back, setBack] = useState<number[]>([])
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [disconnected, setDisconnected] = useState(false)
  const [confirm, setConfirm] = useState<'START' | 'PAUSE' | 'RESUME' | 'STOP' | null>(null)
  const [password, setPassword] = useState(''); const [reason, setReason] = useState('')
  const request = useRef<{ content: string; key: string } | null>(null)
  const approvalKey = useRef(idempotency('force-approval'))
  const active = useRef<string | null>(null)
  const open = useCallback((value: ForceRun) => {
    active.current = value.job_id; setRun(value); setCursor(0); setBack([])
    const url = new URL(window.location.href)
    url.searchParams.set('force_run', value.job_id); url.searchParams.set('view', 'needs-review')
    window.history.replaceState({}, '', url)
  }, [])
  const loadHistory = useCallback(async (before?: number) => {
    const result = await api<{ enabled: boolean; rows: ForceRun[]; next_cursor: number | null }>(root + queryString({ before }))
    setEnabled(result.enabled); setHistory(old => before ? [...old, ...result.rows] : result.rows); setHistoryCursor(result.next_cursor)
  }, [])
  useEffect(() => {
    void loadHistory().catch(err => setError(message(err)))
    const id = new URLSearchParams(window.location.search).get('force_run')
    if (id) void api<ForceRun>(`${root}/${encodeURIComponent(id)}`).then(open).catch(err => setError(message(err)))
  }, [loadHistory, open])
  useEffect(() => {
    if (!run || finished.has(run.status)) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const value = await api<ForceRun>(`${root}/${run.job_id}`)
        if (!stopped && active.current === value.job_id) { setRun(value); setDisconnected(false) }
      } catch { if (!stopped) setDisconnected(true) }
      if (!stopped) timer = setTimeout(poll, 3000)
    }
    timer = setTimeout(poll, 3000)
    return () => { stopped = true; clearTimeout(timer) }
  }, [run?.job_id, run?.status])
  useEffect(() => {
    if (!run) return
    const abort = new AbortController()
    const timer = setTimeout(() => void api<Page>(`${root}/${run.job_id}/items` + queryString({ cursor, state: filter, search, limit: 25 }), { signal: abort.signal })
      .then(setItems).catch(err => { if (!abort.signal.aborted) setError(message(err)) }), 250)
    return () => { clearTimeout(timer); abort.abort() }
  }, [run?.job_id, run?.updated_at, cursor, filter, search])

  const check = async () => {
    setBusy(true); setError('')
    const content = JSON.stringify({ scope, connector_ids: scope === 'ALL_PAKISTAN' ? [] : selected,
      from_time: allDates ? null : pktInputToUtc(from) || null, to_time: allDates ? null : pktInputToUtc(to) || null })
    if (request.current?.content !== content) request.current = { content, key: idempotency('force-check') }
    try {
      const value = await api<ForceRun>(root, { method: 'POST', body: JSON.stringify({ ...JSON.parse(content), idempotency_key: request.current.key }) })
      open(value); request.current = null; await loadHistory()
    } catch (err) { setError(message(err)) } finally { setBusy(false) }
  }
  const approve = async (event: React.FormEvent) => {
    event.preventDefault(); if (!run || !confirm) return
    setBusy(true); setError('')
    try {
      const body = confirm === 'START'
        ? { signature: run.signature, reason, password, idempotency_key: approvalKey.current }
        : { action: confirm, password, idempotency_key: approvalKey.current }
      const value = await api<ForceRun>(`${root}/${run.job_id}/${confirm === 'START' ? 'start' : 'control'}`, { method: 'POST', body: JSON.stringify(body) })
      setRun(value); setConfirm(null); setPassword(''); setReason(''); approvalKey.current = idempotency('force-approval')
      toast.notice(confirm === 'START' ? 'Approval saved. ADD is refreshing terminals before delivery.' : 'Your action is saved.')
    } catch (err) { setError(message(err)) } finally { setBusy(false) }
  }
  const showConfirm = (action: typeof confirm) => { setConfirm(action); setError(''); setPassword(''); approvalKey.current = idempotency('force-approval') }
  const readyDevices = run?.devices.filter(d => d.ready > 0) ?? []
  const syncInProgress = run?.devices.some(d => ['SYNCING', 'SYNC_PENDING'].includes(d.status))
  const groups = new Map<string, Item[]>()
  items.rows.forEach(item => { const key = `${item.device_serial} · ${item.user_id}`; groups.set(key, [...(groups.get(key) || []), item]) })
  return <section className="panel safe-repair manual-force" aria-label="Force release attendance">
    <header className="safe-repair-header"><div><h2>Force release attendance</h2><p>Sync current terminal users, review matching punches, then approve their release.</p></div><span className="badge"><Icon name="shield" /> Manual approval only</span></header>
    <div className="safe-repair-body">
      <h3>1. Select and sync</h3>
      <fieldset className="force-scope"><legend>Devices to check</legend><label><input type="radio" name="force-scope" checked={scope === 'SELECTED'} onChange={() => setScope('SELECTED')} /> Selected devices</label><label><input type="radio" name="force-scope" checked={scope === 'ALL_PAKISTAN'} onChange={() => setScope('ALL_PAKISTAN')} /> All Pakistan</label></fieldset>
      {scope === 'SELECTED' ? <><label className="force-device-search">Find a device<input type="search" value={deviceSearch} onChange={e => setDeviceSearch(e.target.value)} placeholder="Name or terminal serial" /></label><div className="force-device-picker" role="group" aria-label="Select devices">{devices.filter(d => !d.is_spare && `${d.display_name} ${d.zkt?.serial}`.toLowerCase().includes(deviceSearch.toLowerCase())).map(d => <label key={d.connector_id}><input type="checkbox" checked={selected.includes(d.connector_id)} onChange={e => setSelected(old => e.target.checked ? [...old, d.connector_id] : old.filter(id => id !== d.connector_id))} /><span><strong>{d.display_name}</strong><small>{d.zkt?.serial || 'Serial not reported'} · {d.hardware_id} · {d.connected ? 'Online' : 'Unavailable'}</small></span></label>)}</div><p className="force-selection-count">{selected.length} {selected.length === 1 ? 'device' : 'devices'} selected</p></> : <p>Checks all active devices across Pakistan. Spares are excluded. Unavailable devices will be listed separately.</p>}
      <label className="force-inline"><input type="checkbox" checked={allDates} onChange={e => setAllDates(e.target.checked)} /> All saved dates</label>
      {!allDates && <div className="safe-repair-scope"><label>From (PKT)<input type="datetime-local" value={from} onChange={e => setFrom(e.target.value)} /></label><label>Before (PKT)<input type="datetime-local" value={to} onChange={e => setTo(e.target.value)} /></label></div>}
      <button className="button primary" disabled={!enabled || busy || (scope === 'SELECTED' && !selected.length) || (!allDates && (!from || !to || from >= to))} onClick={() => void check()}>{busy && !confirm ? 'Saving check…' : 'Sync and check'}</button>
      {!enabled && <p role="status">Manual force release is awaiting production qualification.</p>}
      {disconnected && <p role="status" className="message pattern-waiting">Connection lost. Your run is saved and continues on ADD. Reconnecting…</p>}
      {error && !confirm && <p className="message pattern-blocked" role="alert">{error}</p>}
      {run && <div className="safe-repair-run">
        <div className="safe-repair-heading"><h3 aria-live="polite">{run.status === 'RUNNING' && syncInProgress ? 'Refreshing terminals' : label[run.status]}</h3><a href={`/attendance?view=needs-review&force_run=${run.job_id}`}>Saved run link</a></div>
        <ol className="safe-repair-steps" aria-label="Release progress">{['Sync and check', 'Review', 'Refreshing terminals', 'Preparing delivery', 'Waiting for Oracle', 'Finished'].map((step, i) => <li key={step} aria-current={(run.status === 'CHECKING' ? 0 : run.status === 'CHECKED' ? 1 : run.status === 'RUNNING' ? syncInProgress ? 2 : 3 : run.status === 'WAITING_ORACLE' ? 4 : run.status.startsWith('COMPLETED') ? 5 : -1) === i ? 'step' : undefined}>{step}</li>)}</ol>
        <div className="safe-repair-counts"><Metric label="Ready to release" value={run.counts.ready} icon="shield" detail="Matching current user and CNIC" /><Metric label="Cannot release" value={run.counts.review} icon="alert" detail="Saved safely for review" tone={run.counts.review ? 'warning' : 'neutral'} /><Metric label="Already confirmed" value={run.counts.already_confirmed} icon="check" detail="Excluded from this release" /><Metric label="Devices unavailable" value={run.counts.unavailable_devices} icon="server" detail="Ready devices can proceed" /></div>
        {!['CHECKING', 'CHECKED'].includes(run.status) && <p role="status">{run.counts.confirmed.toLocaleString()} confirmed · {(run.counts.waiting - ((run.counts.attention ?? run.counts.review) - run.counts.review)).toLocaleString()} waiting · {run.counts.skipped.toLocaleString()} skipped · {run.counts.stopped.toLocaleString()} stopped · {(run.counts.attention ?? run.counts.review).toLocaleString()} need attention</p>}
        {run.counts.already_delivering > 0 && <p>{run.counts.already_delivering} punches already have a delivery in progress and will not be sent again.</p>}
        <ul className="safe-repair-devices">{run.devices.map(d => <li key={d.connector_id}><strong>{d.name} · {d.serial || 'Serial not reported'}</strong><span>{d.status === 'CHECKED' ? 'Ready' : label[d.status]} · {d.checked.toLocaleString()} checked{d.synced_at ? ` · Last terminal sync ${dateTime(d.synced_at)}` : ''}</span>{d.error && <span>{d.error}</span>}</li>)}</ul>
        {run.status === 'CHECKED' && <div className="safe-repair-next"><h3>2. Review and approve</h3><p>Only the ready punches listed in this saved check can be released. Later punches need another manual run.</p><button className="button primary" disabled={!run.signature || !run.execution_enabled || !run.counts.ready} onClick={() => showConfirm('START')}>Force release {punchCount(run.counts.ready)}</button>{!run.signature && <p>This review expired or belongs to another administrator. Sync and check again.</p>}{!run.execution_enabled && run.counts.ready > 0 && <p>Release is not enabled for every ready device in this selection yet.</p>}<small>Review expires {dateTime(run.expires_at)}</small></div>}
        {run.last_error && <p role="status">{run.last_error}</p>}
        {['RUNNING', 'WAITING_ORACLE', 'PAUSED'].includes(run.status) && <div className="safe-repair-actions"><button className="button secondary" onClick={() => showConfirm(run.status === 'PAUSED' ? 'RESUME' : 'PAUSE')}>{run.status === 'PAUSED' ? 'Resume' : 'Pause'}</button><button className="button secondary" onClick={() => showConfirm('STOP')}>Stop</button><span>You can close this page. Your run is saved.</span></div>}
        <div className="safe-repair-filter"><label>Show punches<select value={filter} onChange={e => { setFilter(e.target.value); setCursor(0); setBack([]) }}><option value="">All results</option>{['READY', 'NEEDS_REVIEW', 'SKIPPED', 'EXCLUDED', 'WAITING_ORACLE', 'CONFIRMED'].map(s => <option key={s} value={s}>{label[s]}</option>)}</select></label><label>Find an employee<input value={search} onChange={e => { setSearch(e.target.value); setCursor(0); setBack([]) }} placeholder="Name or user ID" /></label></div>
        {[...groups].map(([key, rows]) => <div className="force-employee-group" key={key}><h4>{rows[0].device_name} · {rows[0].name || `User ${rows[0].user_id}`}</h4><small>{rows[0].device_serial} · User {rows[0].user_id}</small><ul className="safe-repair-items">{rows.map(item => <li key={item.id}><span>{dateTime(item.time)}</span><div><strong>{item.needs_attention ? 'Needs attention' : label[item.status] || item.status}</strong><span>{item.reason}</span></div></li>)}</ul></div>)}
        {!items.rows.length && <p>No punches in this view yet.</p>}
        <div className="safe-repair-actions"><button className="button secondary" disabled={!back.length} onClick={() => { setCursor(back.at(-1) || 0); setBack(old => old.slice(0, -1)) }}>Previous</button><button className="button secondary" disabled={!items.next_cursor} onClick={() => { setBack(old => [...old, cursor]); setCursor(items.next_cursor || 0) }}>Next</button></div>
      </div>}
      <details><summary>Saved checks and release runs</summary><ul className="safe-repair-history">{history.map(row => <li key={row.job_id}><button className="button secondary" onClick={() => open(row)}>{dateTime(row.created_at)} · {label[row.status]} · {row.devices.length} devices</button></li>)}</ul>{historyCursor && <button className="button secondary" onClick={() => void loadHistory(historyCursor).catch(err => setError(message(err)))}>Older runs</button>}</details>
    </div>
    {confirm && run && <Dialog titleId="force-approve-title" title={confirm === 'START' ? `Force release ${punchCount(run.counts.ready)}` : `${confirm[0]}${confirm.slice(1).toLowerCase()} this run`} description={confirm === 'START' ? `${readyDevices.length} ready devices. ${run.counts.review} punches cannot be released; ${run.counts.unavailable_devices} devices are unavailable.` : confirm === 'RESUME' ? 'ADD will refresh terminals before preparing the remaining approved punches.' : 'New records will stop entering delivery. Records already queued continue to be accounted for. Attendance is never deleted.'} onClose={() => { if (!busy) { setConfirm(null); setPassword(''); setError('') } }}><form className="dialog-body" onSubmit={approve}>
      {confirm === 'START' && <><p>You approve using the current matching terminal user and CNIC for these older punches. This records your decision; it does not create historical identity proof. ADD will refresh terminals again and skip changed matches.</p><label>Reason for release<textarea required minLength={3} maxLength={500} value={reason} onChange={e => setReason(e.target.value)} /></label></>}
      {confirm === 'RESUME' && <p>ADD will sync terminals again before preparing any remaining punches.</p>}
      <label>Administrator password<input type="password" required autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} /></label>{error && <p role="alert" className="message pattern-blocked">{error}</p>}
      <footer className="dialog-actions"><button className="button secondary" type="button" disabled={busy} onClick={() => { setConfirm(null); setPassword(''); setError('') }}>Cancel</button><button className="button primary" disabled={busy || !password || (confirm === 'START' && reason.trim().length < 3)}>{busy ? 'Saving…' : confirm === 'START' ? `Approve ${punchCount(run.counts.ready)}` : 'Confirm'}</button></footer></form></Dialog>}
  </section>
}

export function ForcedPill({ evidence }: { evidence: NonNullable<AttendanceEvent['force_release']> }) {
  const [open, setOpen] = useState(false)
  const direct = evidence.policy === 'manual-direct-ords-v1'
  return <><button type="button" className="force-pill" onClick={() => setOpen(true)} aria-label="Forced attendance — view administrator decision">Forced</button>{open && <Dialog titleId="forced-evidence-title" title={direct ? 'Administrator Oracle send' : 'Manual force release'} onClose={() => setOpen(false)}><div className="dialog-body"><p>Approved by {evidence.administrator} · {dateTime(evidence.approved_at)}</p><p>{evidence.reason}</p>{!direct && <p>Terminal sync command {evidence.sync_command_id} · Saved user list {evidence.snapshot_id}</p>}<p>Audit {evidence.audit_id}</p><a href={direct ? `/attendance?view=all-events&direct_run=${evidence.run_id}` : `/attendance?view=needs-review&force_run=${evidence.run_id}`}>Open saved run</a><p>This label records the administrator’s decision. Oracle delivery status is shown separately.</p></div></Dialog>}</>
}

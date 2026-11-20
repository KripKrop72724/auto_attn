import { useCallback, useEffect, useRef, useState } from 'react'
import { api, queryString } from '../api'
import { Dialog, idempotency, Metric, pktInputToUtc } from '../App'
import { Icon } from '../Icon'
import { DivisionTag, TechnicalDetails } from '../ui'
import { deviceDivision } from '../lib/division'
import { deviceName } from '../lib/deviceNames'
import { FORCE_RUN_ERROR } from '../words/attendanceReasons'
import type { AttendanceEvent, Device } from '../types'
import { longTime, rowTime } from './attendance/attendanceWords'
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
type Scope = 'SELECTED' | 'LIFE' | 'HEALTH' | 'ALL_PAKISTAN'
const checkinCount = (count: number) => `${count.toLocaleString()} ${count === 1 ? 'check-in' : 'check-ins'}`
const machines = (count: number) => `${count.toLocaleString()} ${count === 1 ? 'machine' : 'machines'}`
const root = '/api/v2/attendance-force-releases'
const finished = new Set(['COMPLETED', 'COMPLETED_WITH_REVIEW', 'STOPPED', 'EXPIRED'])
export const FORCE_RUN_WORDS: Record<string, string> = {
  CHECKING: 'Checking check-ins', CHECKED: 'Ready to send', RUNNING: 'Getting ready', WAITING_ORACLE: 'Waiting for the HR system',
  PAUSED: 'Paused', STOPPING: 'Stopping · counting the check-ins already on the way', STOPPED: 'Stopped', EXPIRED: 'Check expired',
  COMPLETED: 'Done', COMPLETED_WITH_REVIEW: 'Done · some check-ins need checking', BASELINE: 'Waiting',
  SYNC_PENDING: 'Waiting', SYNCING: 'Refreshing the list of people', RELEASING: 'Getting ready', DONE: 'Ready', UNAVAILABLE: "Couldn't refresh",
  SKIPPED: 'Not sent after the second check', READY: 'Can be sent', NEEDS_REVIEW: "Can't be sent", CONFIRMED: 'Sent', EXCLUDED: 'Left out',
}
const label = FORCE_RUN_WORDS
const message = (error: unknown) => error instanceof Error ? error.message : "Couldn't reach the dashboard. Try again."
const placeOf = (device: Pick<Device, 'friendly_name' | 'display_name' | 'zone_id'>) => {
  if (device.friendly_name) return device.friendly_name
  const name = deviceName(device.display_name || device.zone_id)
  return name.unit ? `${name.location} · ${name.unit}` : name.location
}
/** The place name for a machine in a saved check (by its id, else its saved name). */
const runPlace = (devices: Device[], connectorId: string, fallback: string) => {
  const known = devices.find((device) => device.connector_id === connectorId)
  return known ? placeOf(known) : placeOf({ friendly_name: null, display_name: fallback, zone_id: fallback })
}

export function ManualForceRelease({ devices, toast, initialConnectorId }: { devices: Device[]; toast: Toast; initialConnectorId?: string | null }) {
  const [enabled, setEnabled] = useState(false)
  const [scope, setScope] = useState<Scope>('SELECTED')
  const [selected, setSelected] = useState<string[]>(initialConnectorId ? [initialConnectorId] : [])
  const [deviceSearch, setDeviceSearch] = useState('')
  const [allDates, setAllDates] = useState(true)
  const [from, setFrom] = useState(''); const [to, setTo] = useState('')
  const [run, setRun] = useState<ForceRun | null>(null)
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
    url.searchParams.set('force_run', value.job_id); url.searchParams.set('view', 'fix-tools'); url.searchParams.set('tool', 'held-staff')
    window.history.replaceState({}, '', url)
  }, [])
  // The list of saved checks is under Fix tools › History; here it only says whether the tool is on.
  const loadHistory = useCallback(async () => {
    const result = await api<{ enabled: boolean; rows: ForceRun[]; next_cursor: number | null }>(root)
    setEnabled(result.enabled)
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
    // "All Life" / "All Health" are the chosen machines of that division: the API is unchanged.
    const ids = scope === 'ALL_PAKISTAN' ? [] : scope === 'SELECTED' ? selected : divisionIds(scope === 'LIFE' ? 'life' : 'health')
    const content = JSON.stringify({ scope: scope === 'ALL_PAKISTAN' ? 'ALL_PAKISTAN' : 'SELECTED', connector_ids: ids,
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
      toast.notice(confirm === 'START' ? 'Approval saved. The machines are refreshed before sending.' : 'Saved.')
    } catch (err) { setError(message(err)) } finally { setBusy(false) }
  }
  const showConfirm = (action: typeof confirm) => { setConfirm(action); setError(''); setPassword(''); approvalKey.current = idempotency('force-approval') }
  const inUse = devices.filter(d => !d.is_spare)
  const divisionIds = (division: 'life' | 'health') => inUse.filter(d => deviceDivision(d) === division).map(d => d.connector_id)
  const readyDevices = run?.devices.filter(d => d.ready > 0) ?? []
  const syncInProgress = run?.devices.some(d => ['SYNCING', 'SYNC_PENDING'].includes(d.status))
  const groups = new Map<string, Item[]>()
  items.rows.forEach(item => { const key = `${item.device_serial} · ${item.user_id}`; groups.set(key, [...(groups.get(key) || []), item]) })
  const found = inUse.filter(d => `${placeOf(d)} ${d.display_name}`.toLowerCase().includes(deviceSearch.toLowerCase()))
  const scopeCount = scope === 'LIFE' ? divisionIds('life').length : scope === 'HEALTH' ? divisionIds('health').length : selected.length
  const step = run ? (run.status === 'CHECKING' ? 0 : run.status === 'CHECKED' ? 1 : run.status === 'RUNNING' ? syncInProgress ? 2 : 3 : run.status === 'WAITING_ORACLE' ? 4 : run.status.startsWith('COMPLETED') ? 5 : -1) : -1
  const dialogTitle = confirm === 'START' ? `Send ${checkinCount(run?.counts.ready || 0)}` : confirm === 'PAUSE' ? 'Pause this task' : confirm === 'RESUME' ? 'Resume this task' : 'Stop this task'
  return <section className="panel safe-repair manual-force" aria-label="Re-check not-matched check-ins">
    <header className="safe-repair-header"><div><h2>Re-check not-matched check-ins</h2><p>Refresh the machines' people lists, see which not-matched check-ins now match a person with a CNIC, and send them after your approval.</p></div><span className="badge"><Icon name="shield" /> Needs your approval</span></header>
    <div className="safe-repair-body">
      <h3>1. Choose machines</h3>
      <fieldset className="force-scope"><legend>Machines</legend>
        <label><input type="radio" name="force-scope" checked={scope === 'SELECTED'} onChange={() => setScope('SELECTED')} /> Chosen machines</label>
        <label><input type="radio" name="force-scope" checked={scope === 'LIFE'} onChange={() => setScope('LIFE')} /> All Life</label>
        <label><input type="radio" name="force-scope" checked={scope === 'HEALTH'} onChange={() => setScope('HEALTH')} /> All Health</label>
        <label><input type="radio" name="force-scope" checked={scope === 'ALL_PAKISTAN'} onChange={() => setScope('ALL_PAKISTAN')} /> All machines</label>
      </fieldset>
      {scope === 'SELECTED' ? <><label className="force-device-search">Find a place<input type="search" value={deviceSearch} onChange={e => setDeviceSearch(e.target.value)} placeholder="Place name" /></label>
        <div className="force-device-picker" role="group" aria-label="Choose machines">{(['life', 'health'] as const).map(division => {
          const rows = found.filter(d => deviceDivision(d) === division).sort((a, b) => placeOf(a).localeCompare(placeOf(b), 'en', { numeric: true }))
          if (!rows.length) return null
          return <div key={division} className="force-device-group"><p className="force-device-group-title"><DivisionTag division={division} /></p>{rows.map(d => <label key={d.connector_id}><input type="checkbox" checked={selected.includes(d.connector_id)} onChange={e => setSelected(old => e.target.checked ? [...old, d.connector_id] : old.filter(id => id !== d.connector_id))} /><span><strong>{placeOf(d)}</strong><small><i className={`force-online-dot ${d.connected ? 'is-online' : ''}`} aria-hidden="true" />{d.connected ? 'Online' : 'Offline'}</small></span></label>)}</div>
        })}</div>
        <p className="force-selection-count">{machines(selected.length)} chosen</p></>
        : scope === 'ALL_PAKISTAN' ? <p>Checks every machine in use. Spares are left out. Machines that are offline are listed separately.</p>
          : <p>Checks {machines(scopeCount)} in use in {scope === 'LIFE' ? 'Life' : 'Health'}. Spares are left out.</p>}
      <label className="force-inline"><input type="checkbox" checked={allDates} onChange={e => setAllDates(e.target.checked)} /> All dates</label>
      {!allDates && <div className="safe-repair-scope"><label>From<input type="datetime-local" value={from} onChange={e => setFrom(e.target.value)} /></label><label>Up to<input type="datetime-local" value={to} onChange={e => setTo(e.target.value)} /></label><small>Pakistan time</small></div>}
      <button className="button primary" disabled={!enabled || busy || (scope !== 'ALL_PAKISTAN' && !scopeCount) || (!allDates && (!from || !to || from >= to))} onClick={() => void check()}>{busy && !confirm ? 'Checking…' : 'Re-check not-matched check-ins'}</button>
      {!enabled && <p role="status">This tool isn't switched on yet.</p>}
      {disconnected && <p role="status" className="message pattern-waiting">Connection lost. Sending continues; reconnecting…</p>}
      {error && !confirm && <p className="message pattern-blocked" role="alert">{error}</p>}
      {run && <div className="safe-repair-run">
        <div className="safe-repair-heading"><h3 aria-live="polite">{run.status === 'RUNNING' && syncInProgress ? 'Refreshing machines' : label[run.status] || 'In progress'}</h3><a href={`/attendance?view=fix-tools&tool=held-staff&force_run=${run.job_id}`}>Link to this check</a></div>
        <ol className="safe-repair-steps" aria-label="Progress">{['Check', 'Review', 'Refresh machines', 'Get ready', 'Waiting for HR system', 'Done'].map((name, i) => <li key={name} aria-current={step === i ? 'step' : undefined}>{name}</li>)}</ol>
        <div className="safe-repair-counts"><Metric label="Can be sent" value={run.counts.ready} icon="shield" detail="Same person and CNIC on the machine now" /><Metric label="Can't be sent" value={run.counts.review} icon="alert" detail="Kept safely for checking" tone={run.counts.review ? 'warning' : 'neutral'} /><Metric label="Already sent" value={run.counts.already_confirmed} icon="check" detail="Left out of this one" /><Metric label="Machines offline" value={run.counts.unavailable_devices} icon="server" detail="The others can go ahead" /></div>
        {!['CHECKING', 'CHECKED'].includes(run.status) && <p role="status">{run.counts.confirmed.toLocaleString()} sent · {(run.counts.waiting - ((run.counts.attention ?? run.counts.review) - run.counts.review)).toLocaleString()} waiting · {run.counts.skipped.toLocaleString()} not sent · {run.counts.stopped.toLocaleString()} stopped · {(run.counts.attention ?? run.counts.review).toLocaleString()} need checking</p>}
        {run.counts.already_delivering > 0 && <p>{checkinCount(run.counts.already_delivering)} are already on the way and won't be sent again.</p>}
        <ul className="safe-repair-devices">{run.devices.map(d => <li key={d.connector_id}><strong>{runPlace(devices, d.connector_id, d.name)}</strong><span>{d.status === 'CHECKED' ? 'Ready' : label[d.status] || 'In progress'} · {d.checked.toLocaleString()} checked{d.synced_at ? ` · List of people read ${rowTime(d.synced_at)}` : ''}</span>{d.error && <span data-technical className="force-device-error">{d.error}</span>}</li>)}</ul>
        {run.status === 'CHECKED' && <div className="safe-repair-next"><h3>2. Send</h3><p>Only the check-ins that can be sent in this saved check are sent. Later check-ins need another check.</p><button className="button primary" disabled={!run.signature || !run.execution_enabled || !run.counts.ready} onClick={() => showConfirm('START')}>Send {checkinCount(run.counts.ready)}</button>{!run.signature && <p>This check expired or belongs to another admin. Check again.</p>}{!run.execution_enabled && run.counts.ready > 0 && <p>Sending isn't switched on yet for every chosen machine.</p>}<small>This check expires {longTime(run.expires_at)}</small></div>}
        {run.last_error && <p role="status">{FORCE_RUN_ERROR}</p>}
        {['RUNNING', 'WAITING_ORACLE', 'PAUSED'].includes(run.status) && <div className="safe-repair-actions"><button className="button secondary" onClick={() => showConfirm(run.status === 'PAUSED' ? 'RESUME' : 'PAUSE')}>{run.status === 'PAUSED' ? 'Resume' : 'Pause'}</button><button className="button secondary" onClick={() => showConfirm('STOP')}>Stop</button><span>You can close this page. The task is saved.</span></div>}
        <div className="safe-repair-filter"><label>Show<select value={filter} onChange={e => { setFilter(e.target.value); setCursor(0); setBack([]) }}><option value="">All results</option>{['READY', 'NEEDS_REVIEW', 'SKIPPED', 'EXCLUDED', 'WAITING_ORACLE', 'CONFIRMED'].map(s => <option key={s} value={s}>{label[s]}</option>)}</select></label><label>Find a person<input value={search} onChange={e => { setSearch(e.target.value); setCursor(0); setBack([]) }} placeholder="Name or Employee ID" /></label></div>
        {[...groups].map(([key, rows]) => <div className="force-employee-group" key={key}><h4>{runPlace(devices, '', rows[0].device_name)} · {rows[0].name || 'Name not known'}</h4><small>Employee ID {rows[0].user_id}</small><ul className="safe-repair-items">{rows.map(item => <li key={item.id}><span>{rowTime(item.time)}</span><div><strong>{item.needs_attention ? 'Needs checking' : label[item.status] || 'In progress'}</strong><span>{item.reason}</span></div></li>)}</ul></div>)}
        {!items.rows.length && <p>No check-ins in this view yet.</p>}
        <div className="safe-repair-actions"><button className="button secondary" disabled={!back.length} onClick={() => { setCursor(back.at(-1) || 0); setBack(old => old.slice(0, -1)) }}>Previous</button><button className="button secondary" disabled={!items.next_cursor} onClick={() => { setBack(old => [...old, cursor]); setCursor(items.next_cursor || 0) }}>Next</button></div>
      </div>}
    </div>
    {confirm && run && <Dialog titleId="force-approve-title" title={dialogTitle} description={confirm === 'START' ? `${machines(readyDevices.length)} ready. ${checkinCount(run.counts.review)} can't be sent; ${machines(run.counts.unavailable_devices)} offline.` : confirm === 'RESUME' ? "The machines' people lists are read again before the rest are sent." : 'No more check-ins start sending. Check-ins already on the way are still counted. Attendance is never deleted.'} onClose={() => { if (!busy) { setConfirm(null); setPassword(''); setError('') } }}><form className="dialog-body" onSubmit={approve}>
      {confirm === 'START' && <><p>You approve using the person and CNIC on the machine now for these older check-ins. This saves your decision in the records. The machines are read again first, and any match that changed is not sent.</p><label>Why? (saved in the records)<textarea required minLength={3} maxLength={500} value={reason} onChange={e => setReason(e.target.value)} /></label></>}
      {confirm === 'RESUME' && <p>The machines' people lists are read again before any more check-ins are sent.</p>}
      <label>Your password<input type="password" required autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} /></label>{error && <p role="alert" className="message pattern-blocked">{error}</p>}
      <footer className="dialog-actions"><button className="button secondary" type="button" disabled={busy} onClick={() => { setConfirm(null); setPassword(''); setError('') }}>Cancel</button><button className="button primary" disabled={busy || !password || (confirm === 'START' && reason.trim().length < 3)}>{busy ? 'Saving…' : confirm === 'START' ? `Send ${checkinCount(run.counts.ready)}` : 'Confirm'}</button></footer></form></Dialog>}
  </section>
}

/** The sheet block for a check-in an admin sent (replaces the old "Forced" pill). */
export function AdminSendFacts({ evidence }: { evidence: NonNullable<AttendanceEvent['force_release']> }) {
  const direct = evidence.policy === 'manual-direct-ords-v1'
  const href = direct ? `/attendance?direct_run=${encodeURIComponent(evidence.run_id)}` : `/attendance?view=fix-tools&tool=held-staff&force_run=${encodeURIComponent(evidence.run_id)}`
  return <section className="attendance-sheet-block" aria-labelledby={`admin-send-${evidence.run_id}`}>
    <h3 id={`admin-send-${evidence.run_id}`}>Sent by an admin</h3>
    <p>Sent by <span data-user-text>{evidence.administrator}</span> on {longTime(evidence.approved_at)}</p>
    {evidence.reason && <p>Reason: <span data-user-text>{evidence.reason}</span></p>}
    <a href={href}>Open the sending task</a>
    <TechnicalDetails summary="Technical details">
      <dl className="attendance-tech-list">
        {evidence.audit_id != null && <div><dt>Record no.</dt><dd>{evidence.audit_id}</dd></div>}
        <div><dt>Task</dt><dd>{evidence.run_id}</dd></div>
        {evidence.policy && <div><dt>Policy</dt><dd>{evidence.policy}</dd></div>}
        {evidence.snapshot_id != null && <div><dt>Saved people list</dt><dd>{evidence.snapshot_id}</dd></div>}
        {evidence.sync_command_id != null && <div><dt>Machine request</dt><dd>{evidence.sync_command_id}</dd></div>}
      </dl>
    </TechnicalDetails>
  </section>
}

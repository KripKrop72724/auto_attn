import { SavedAttendanceCoverage } from './SavedAttendanceCoverage'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, queryString } from '../api'
import { dateTime, Dialog, idempotency, Metric } from '../App'
import type { Device, HistoricalIdentityCandidate, HistoricalIdentityReport } from '../types'
import { HistoricalIdentityResolutionDialog } from './Users'
import './SafeAttendanceRepair.css'

type Toast = { notice: (text: string) => void; error: (text: string) => void }
type Counts = { checked: number; ready: number; waiting: number; confirmed: number; review: number; stopped: number }
export type RepairRun = {
  automatic?: boolean; job_id: string; status: string; actor: string; counts: Counts; check_complete: boolean
  created_at: string; updated_at: string; expires_at: string | null; signature: string | null
  last_error: string | null; execution_enabled: boolean; eligible_at_check: number
  devices: Array<{ connector_id: string; name: string; serial: string | null; checked: number; status: string }>
}
type RepairItem = { id: number; event_id: number; event_uid: string; status: string; reason: string; name: string | null; user_id: string; connector_id: string; device_name: string; device_serial: string | null; time: string }
type ItemPage = { rows: RepairItem[]; next_cursor: number | null }
const labels: Record<string, string> = {
  CHECKING: 'Checking saved attendance', CHECKED: 'Ready to review', RUNNING: 'Repair in progress',
  WAITING_ORACLE: 'Waiting for Oracle', PAUSED: 'Paused', STOPPING: 'Stopping — checking deliveries already sent',
  STOPPED: 'Stopped', COMPLETED: 'Repair complete', COMPLETED_WITH_REVIEW: 'Finished — some records need your help', EXPIRED: 'Check expired',
  READY: 'Ready to repair', NEEDS_REVIEW: 'Needs your help', CONFIRMED: 'Confirmed by Oracle',
}
const terminal = new Set(['COMPLETED', 'COMPLETED_WITH_REVIEW', 'STOPPED', 'EXPIRED'])
const errorText = (error: unknown) => error instanceof Error ? error.message : 'Could not reach ADD. Please try again.'

export function SafeAttendanceRepair({ devices, toast }: { devices: Device[]; toast: Toast }) {
  const [enabled, setEnabled] = useState(false)
  const [opened, setOpened] = useState(() => Boolean(new URLSearchParams(window.location.search).get('repair_run')))
  const [scope, setScope] = useState('')
  const [run, setRun] = useState<RepairRun | null>(null)
  const [history, setHistory] = useState<RepairRun[]>([])
  const [historyCursor, setHistoryCursor] = useState<number | null>(null)
  const [items, setItems] = useState<ItemPage>({ rows: [], next_cursor: null })
  const [filter, setFilter] = useState('')
  const [itemCursor, setItemCursor] = useState<number | undefined>(undefined)
  const [cursorStack, setCursorStack] = useState<number[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [connectionError, setConnectionError] = useState('')
  const [confirm, setConfirm] = useState<'START' | 'PAUSE' | 'RESUME' | 'STOP' | null>(null)
  const [password, setPassword] = useState('')
  const [review, setReview] = useState<{ item: RepairItem; candidates: HistoricalIdentityCandidate[] } | null>(null)
  const [candidate, setCandidate] = useState<HistoricalIdentityCandidate | null>(null)
  const checkKey = useRef(idempotency('attendance-check'))
  const activeId = useRef<string | null>(null)
  const openRun = useCallback((value: RepairRun) => {
    activeId.current = value.job_id
    setRun(value)
    setItemCursor(undefined); setCursorStack([])
    setOpened(true)
    const url = new URL(window.location.href)
    url.searchParams.set('repair_run', value.job_id)
    window.history.replaceState({}, '', url)
  }, [])
  const loadHistory = useCallback(async (before?: number) => {
    const result = await api<{ enabled: boolean; rows: RepairRun[]; next_cursor: number | null }>('/api/v2/attendance-recovery/checks' + queryString({ before }))
    setEnabled(result.enabled)
    setHistory(old => before ? [...old, ...result.rows] : result.rows)
    setHistoryCursor(result.next_cursor)
  }, [])
  useEffect(() => {
    let mounted = true
    void loadHistory().catch(err => { if (mounted) setError(errorText(err)) })
    const id = new URLSearchParams(window.location.search).get('repair_run')
    if (id) void api<RepairRun>(`/api/v2/attendance-recovery/checks/${encodeURIComponent(id)}`).then(value => { if (mounted) openRun(value) }).catch(err => { if (mounted) setError(errorText(err)) })
    return () => { mounted = false }
  }, [loadHistory, openRun])

  useEffect(() => {
    if (!run) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const fresh = await api<RepairRun>(`/api/v2/attendance-recovery/checks/${run.job_id}`)
        if (!stopped && activeId.current === fresh.job_id) { setRun(fresh); setConnectionError('') }
      } catch { if (!stopped) setConnectionError('Connection interrupted. Your repair is saved and continues on ADD. Reconnecting…') }
      if (!stopped) timer = setTimeout(poll, 3000)
    }
    if (!terminal.has(run.status)) timer = setTimeout(poll, 3000)
    return () => { stopped = true; clearTimeout(timer) }
  }, [run?.job_id, run?.status])

  useEffect(() => {
    if (!run) return
    let stopped = false
    void api<ItemPage>(`/api/v2/attendance-recovery/jobs/${run.job_id}/items` + queryString({ state: filter, limit: 25, cursor: itemCursor }))
      .then(value => { if (!stopped) setItems(value) }).catch(err => { if (!stopped) setError(errorText(err)) })
    return () => { stopped = true }
  }, [run?.job_id, run?.status, run?.updated_at, filter, itemCursor])

  const check = async () => {
    setBusy(true); setError('')
    try {
      const value = await api<RepairRun>('/api/v2/attendance-recovery/checks', { method: 'POST', body: JSON.stringify({ connector_ids: scope ? [scope] : [], idempotency_key: checkKey.current }) })
      openRun(value)
      checkKey.current = idempotency('attendance-check')
      await loadHistory()
    } catch (err) { setError(errorText(err)) } finally { setBusy(false) }
  }
  const approve = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!run || !confirm) return
    setBusy(true); setError('')
    try {
      const start = confirm === 'START'
      const value = await api<RepairRun>(start ? '/api/v2/attendance-recovery/jobs' : `/api/v2/attendance-recovery/jobs/${run.job_id}/control`, {
        method: 'POST', body: JSON.stringify(start ? { action: 'SAFE_REPAIR', check_id: run.job_id, signature: run.signature, password } : { workflow: 'SAFE_REPAIR', action: confirm, password }),
      })
      openRun(value); setConfirm(null); setPassword('')
      toast.notice(start ? 'Repair started. You can close this page and return later.' : 'Your repair run has been updated.')
      await loadHistory()
    } catch (err) { setError(errorText(err)) } finally { setBusy(false) }
  }
  const openReview = async (item: RepairItem) => {
    setBusy(true); setError('')
    try {
      const report = await api<HistoricalIdentityReport>(`/api/v2/devices/${item.connector_id}/historical-identities`)
      const candidates = [...report.rows, ...(report.unassigned_groups || [])].filter(row => row.user_id === item.user_id)
      setReview({ item, candidates })
    } catch (err) { setError(errorText(err)) } finally { setBusy(false) }
  }
  const moreItems = () => {
    if (!items.next_cursor) return
    setCursorStack(old => [...old, itemCursor || 0]); setItemCursor(items.next_cursor)
  }
  const currentDevice = devices.find(device => device.connector_id === review?.item.connector_id)
  return <section className="safe-repair panel" aria-labelledby="safe-repair-title">
    <div className="safe-repair-header">
      <div><h2 id="safe-repair-title">Repair attendance</h2><p>Check saved punches across Pakistan, repair verified matches, and follow delivery to Oracle.</p></div>
      <button className="button primary" onClick={() => setOpened(!opened)} aria-expanded={opened}>{opened ? 'Hide repair' : 'Repair attendance'}</button>
    </div>
    {opened && <div className="safe-repair-body">
      <SavedAttendanceCoverage connectorId={scope || undefined} />
      <div className="safe-repair-scope">
        <label>Where to check<select value={scope} onChange={event => { setScope(event.target.value); checkKey.current = idempotency('attendance-check') }}><option value="">All devices across Pakistan</option>{devices.map(device => <option key={device.connector_id} value={device.connector_id}>{device.display_name} · {device.zkt?.serial || 'Serial not reported'}</option>)}</select></label>
        <button className="button secondary" disabled={busy || !enabled} onClick={() => void check()}>{busy && !confirm ? 'Please wait…' : run ? 'Run a new check' : 'Check saved attendance'}</button>
      </div>
      <p className="muted">All dates. This checks records already saved in ADD. <a href="/reconciliation">Check records still on a device</a>.</p>
      {!enabled && !error && <p role="status">Repair checks will become available after validation is complete.</p>}
      {connectionError && <p className="message pattern-waiting" role="status">{connectionError}</p>}
      {error && !confirm && <div className="message pattern-blocked" role="alert">{error}<button className="button secondary" onClick={() => { setError(''); void loadHistory().catch(err => setError(errorText(err))) }}>Try again</button></div>}
      {run && <div className="safe-repair-run">
        <div className="safe-repair-heading"><h3 aria-live="polite">{labels[run.status] || 'Checking repair status'}</h3><small>Last update {dateTime(run.updated_at)}</small></div>
        <ol className="safe-repair-steps" aria-label="Repair steps"><li aria-current={run.status === 'CHECKING' ? 'step' : undefined}>1. Check</li><li aria-current={run.status === 'CHECKED' ? 'step' : undefined}>2. Review</li><li aria-current={['RUNNING', 'WAITING_ORACLE'].includes(run.status) ? 'step' : undefined}>3. Repair and confirm</li></ol>
        <div className="safe-repair-counts">
          <Metric label="Checked" value={run.counts.checked.toLocaleString()} icon="list" detail={run.check_complete ? 'Saved records needing attention' : 'Check is still running'} />
          <Metric label="Ready to repair" value={run.counts.ready.toLocaleString()} icon="shield" detail="Verified matches" />
          <Metric label="Waiting for Oracle" value={run.counts.waiting.toLocaleString()} icon="clock" detail="Saved for delivery" />
          <Metric label="Confirmed" value={run.counts.confirmed.toLocaleString()} icon="check" detail="Oracle confirmation received" />
          <Metric label="Needs your help" value={run.counts.review.toLocaleString()} icon="alert" detail="Kept safely for review" />
        </div>
        {run.status === 'CHECKED' && <div className="safe-repair-next"><p>{run.counts.ready.toLocaleString()} punches can be repaired. {run.counts.review.toLocaleString()} need more evidence. Review the device list, then start.</p><button className="button primary" disabled={!run.signature || !run.execution_enabled || run.counts.ready === 0} onClick={() => { setError(''); setConfirm('START') }}>Start repair</button>{!run.execution_enabled && <p>Checks are available. Repair is not yet enabled for every device in this selection.</p>}{!run.signature && <p>This check expired or belongs to another admin. Run a new check to continue.</p>}</div>}
        {run.last_error && <p className="message pattern-waiting" role="status">{run.last_error}</p>}
        {run.status === 'PAUSED' && <p>New repairs are paused. Records already queued will continue to Oracle.</p>}
        {['RUNNING', 'WAITING_ORACLE', 'PAUSED'].includes(run.status) && <div className="safe-repair-actions"><button className="button secondary" onClick={() => { setError(''); setConfirm(run.status === 'PAUSED' ? 'RESUME' : 'PAUSE') }}>{run.status === 'PAUSED' ? 'Resume repair' : 'Pause repair'}</button><button className="button secondary" onClick={() => { setError(''); setConfirm('STOP') }}>Stop repair</button><span>You can leave this page. Progress is saved.</span></div>}
        {terminal.has(run.status) && <p role="status">{run.counts.confirmed.toLocaleString()} confirmed by Oracle · {run.counts.review.toLocaleString()} need your help · {run.counts.stopped.toLocaleString()} stopped before delivery.</p>}
        <details><summary>{run.devices.length.toLocaleString()} devices included</summary><ul className="safe-repair-devices">{run.devices.map(device => <li key={device.connector_id}><strong>{device.name}</strong><span>{device.serial || 'Serial not reported'} · {device.checked.toLocaleString()} checked</span></li>)}</ul></details>
        <label className="safe-repair-filter">Show records<select value={filter} onChange={event => { setFilter(event.target.value); setItemCursor(undefined); setCursorStack([]) }}><option value="">All records in this check</option><option value="NEEDS_REVIEW">Needs your help</option><option value="READY">Ready to repair</option><option value="WAITING_ORACLE">Waiting for Oracle</option><option value="CONFIRMED">Confirmed by Oracle</option></select></label>
        {run.automatic && <p>Automatic checks list records that can be repaired. Run a new check to review every held record.</p>}
        <ul className="safe-repair-items">{items.rows.map(item => <li key={item.id}><div><strong>{item.name || `Employee number ${item.user_id}`}</strong><p>{item.device_name} · {dateTime(item.time)}</p><p>{item.reason}</p></div><div><span>{labels[item.status] || item.status}</span>{item.status === 'NEEDS_REVIEW' && <button className="button secondary" disabled={busy} onClick={() => void openReview(item)}>Review this record</button>}</div></li>)}</ul>
        {cursorStack.length > 0 && <button className="button secondary" onClick={() => { setItemCursor(cursorStack.at(-1) || undefined); setCursorStack(old => old.slice(0, -1)) }}>Previous records</button>}
        {items.next_cursor && <button className="button secondary" disabled={busy} onClick={() => void moreItems()}>Show more records</button>}
      </div>}
      <details onToggle={event => { if (event.currentTarget.open) void loadHistory().catch(err => setError(errorText(err))) }}><summary>Previous checks and repairs</summary><ul className="safe-repair-history">{history.map(value => <li key={value.job_id}><button className="button secondary" onClick={() => openRun(value)}>{dateTime(value.created_at)} · {labels[value.status]} · {value.devices.length} devices</button></li>)}</ul>{historyCursor && <button className="button secondary" onClick={() => void loadHistory(historyCursor).catch(err => setError(errorText(err)))}>Show older runs</button>}</details>
    </div>}
    {confirm && run && <Dialog titleId="repair-confirm-title" title={confirm === 'START' ? 'Start attendance repair' : `${confirm[0]}${confirm.slice(1).toLowerCase()} repair`} description={confirm === 'START' ? `${run.counts.ready} verified punches across ${run.devices.length} devices. Records needing more evidence will stay safely held.` : confirm === 'STOP' ? 'Stop adding records to delivery. Records already queued will continue to Oracle.' : 'Attendance stays saved in ADD.'} onClose={() => { if (!busy) { setConfirm(null); setPassword(''); setError('') } }}><form className="dialog-body" onSubmit={event => void approve(event)}><label>Administrator password<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required /></label>{error && <p role="alert" className="message pattern-blocked">{error}</p>}<footer className="dialog-actions"><button className="button secondary" type="button" disabled={busy} onClick={() => { setConfirm(null); setPassword(''); setError('') }}>Cancel</button><button className="button primary" disabled={busy || !password}>{busy ? 'Saving…' : confirm === 'START' ? 'Start repair' : 'Confirm'}</button></footer></form></Dialog>}
    {review && !candidate && <Dialog titleId="repair-review-title" title="Help identify this attendance" description="Confirm who used this employee number when these punches were made. ADD checks the evidence before sending anything." onClose={() => setReview(null)}><div className="dialog-body"><p>{review.item.device_name} · {review.item.name || review.item.user_id}</p>{!review.candidates.length && <p>This record needs a device or delivery investigation. It remains safely saved; another employee’s details will not be substituted.</p>}{review.candidates.map((value, index) => <div className="safe-repair-review" key={value.group_token || value.source_user_key || index}><h3>{value.display_name}</h3><p>{value.event_count} punches · {dateTime(value.first_event_at)} to {dateTime(value.last_event_at)}</p>{currentDevice && value.operator_actionable && ['CURRENT_IDENTITY_EVIDENCE', 'HR_DIRECTORY_EVIDENCE', 'HR_DIRECTORY_EVENT_GROUP'].includes(value.resolution_path) ? <button className="button primary" onClick={() => setCandidate(value)}>Verify employee details</button> : <p>More evidence is needed to distinguish this employee’s old records. The records stay held.</p>}</div>)}</div></Dialog>}
    {candidate && currentDevice && <HistoricalIdentityResolutionDialog simple state={{ candidate }} device={currentDevice} toast={toast} onClose={() => { setCandidate(null); setReview(null) }} onComplete={async () => { setCandidate(null); setReview(null); toast.notice('Evidence saved. Run a new check to follow the repaired records.'); await loadHistory() }} />}
  </section>
}

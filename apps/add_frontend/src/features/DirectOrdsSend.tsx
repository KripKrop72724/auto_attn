import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { api, queryString } from '../api'
import { dateTime, Dialog, idempotency } from '../App'
import type { AttendanceEvent } from '../types'

export type DirectRun = {
  job_id: string; status: string; created_at: string; actor: string; selected: number;
  ready: number; waiting: number; confirmed: number; skipped: number; attention: number; legacy_recheckable?: number;
}
type DirectItem = { item_id: string; attendance_event_id: number; status: string; reason: string | null; error_code: string | null }
type ItemPage = { rows: DirectItem[]; next_cursor: number | null }
const root = '/api/v2/attendance-direct-ords'
const finished = new Set(['COMPLETED', 'COMPLETED_WITH_REVIEW'])
const count = (value: number) => `${value.toLocaleString()} ${value === 1 ? 'punch' : 'punches'}`
const message = (error: unknown) => error instanceof Error ? error.message : 'ADD could not save this request.'

export function DirectOrdsSend({ selected, clearSelection, refresh, openRequest }: {
  selected: AttendanceEvent[]; clearSelection: () => void; refresh: () => void; openRequest: number;
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [run, setRun] = useState<DirectRun | null>(null)
  const [history, setHistory] = useState<DirectRun[]>([])
  const [items, setItems] = useState<ItemPage>({ rows: [], next_cursor: null })
  const [cursor, setCursor] = useState(0)
  const [disconnected, setDisconnected] = useState(false)
  const [recheckOpen, setRecheckOpen] = useState(false)
  const [recheckPassword, setRecheckPassword] = useState('')
  const [rechecking, setRechecking] = useState(false)
  const syncedCnicCount = selected.filter((row) => row.direct_ords_identity?.cnic_source === 'SYNCED_USER').length
  const approvalKey = useRef(idempotency('direct-ords'))
  const requestSignature = useRef('')
  const activeId = useRef<string | null>(null)
  useEffect(() => { if (openRequest) { setOpen(true); setError('') } }, [openRequest])

  const loadHistory = useCallback(async () => {
    const response = await api<{ rows: DirectRun[] }>(root)
    setHistory(Array.isArray(response.rows) ? response.rows.filter((row) => row.job_id && typeof row.selected === 'number') : [])
  }, [])
  const openRun = useCallback((value: DirectRun) => {
    activeId.current = value.job_id
    setRun(value); setCursor(0); setItems({ rows: [], next_cursor: null })
    const url = new URL(window.location.href)
    url.searchParams.set('direct_run', value.job_id)
    url.searchParams.set('view', 'all-events')
    window.history.replaceState({}, '', url)
  }, [])
  useEffect(() => {
    const restore = () => {
      const id = new URLSearchParams(window.location.search).get('direct_run')
      if (id && id !== activeId.current) void api<DirectRun>(`${root}/${encodeURIComponent(id)}`).then(openRun).catch((failure) => setError(message(failure)))
    }
    restore()
    window.addEventListener('popstate', restore)
    return () => window.removeEventListener('popstate', restore)
  }, [openRun])
  useEffect(() => {
    if (!run || finished.has(run.status)) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const current = await api<DirectRun>(`${root}/${encodeURIComponent(run.job_id)}`)
        if (!cancelled && activeId.current === current.job_id) {
          setRun(current); setDisconnected(false); refresh()
        }
      } catch { if (!cancelled) setDisconnected(true) }
      if (!cancelled) timer = setTimeout(poll, 2500)
    }
    timer = setTimeout(poll, 2500)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [run?.job_id, run?.status, refresh])
  useEffect(() => {
    if (!run) return
    const controller = new AbortController()
    void api<ItemPage>(`${root}/${encodeURIComponent(run.job_id)}/items${queryString({ cursor, limit: 50 })}`, { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setItems(result) })
      .catch(() => undefined)
    return () => controller.abort()
  }, [run?.job_id, run?.ready, run?.waiting, run?.confirmed, run?.skipped, run?.attention, cursor])

  const approve = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !selected.length) return
    const signature = JSON.stringify([selected.map((row) => row.id), reason.trim()])
    if (requestSignature.current !== signature) {
      approvalKey.current = idempotency('direct-ords')
      requestSignature.current = signature
    }
    setBusy(true); setError('')
    try {
      const saved = await api<DirectRun>(root, { method: 'POST', body: JSON.stringify({
        event_ids: selected.map((row) => row.id), reason: reason.trim(), password,
        idempotency_key: approvalKey.current,
      }) })
      openRun(saved); setOpen(false); setPassword(''); setReason(''); clearSelection()
      approvalKey.current = idempotency('direct-ords'); requestSignature.current = ''
      await loadHistory(); refresh()
    } catch (failure) { setError(message(failure)) }
    finally { setBusy(false) }
  }
  const close = () => { if (!busy) { setOpen(false); setPassword(''); setError('') } }
  const recheck = async (event: FormEvent) => {
    event.preventDefault()
    if (!run || rechecking) return
    setRechecking(true); setError('')
    try {
      const saved = await api<DirectRun>(`${root}/${encodeURIComponent(run.job_id)}/recheck`, {
        method: 'POST', body: JSON.stringify({ password: recheckPassword }),
      })
      openRun(saved); setRecheckOpen(false); setRecheckPassword(''); refresh()
    } catch (failure) { setError(message(failure)) }
    finally { setRechecking(false) }
  }
  return <section className="attendance-direct-send" aria-label="Send selected punches to Oracle">
    <div className="attendance-direct-toolbar">
      <div><strong>{selected.length ? `${count(selected.length)} selected` : 'Send saved punches to Oracle'}</strong><small>Choose punches in the list. ADD saves one audited run; Oracle confirmation decides the final status.</small></div>
      <div>{selected.length > 0 && <button className="button secondary" type="button" onClick={clearSelection}>Clear selection</button>}<button className="button primary" type="button" disabled={!selected.length} onClick={() => { setOpen(true); setError('') }}>Send {selected.length ? count(selected.length) : 'selected punches'}</button></div>
    </div>
    {run && <div className="attendance-direct-progress" role="status" aria-live="polite">
      <div className="attendance-direct-heading"><strong>{run.status === 'RUNNING' ? 'Preparing delivery' : run.status === 'WAITING_ORACLE' ? 'Waiting for Oracle' : run.attention || run.skipped ? 'Finished with items to review' : 'Finished'}</strong><a href={`/attendance?view=all-events&direct_run=${encodeURIComponent(run.job_id)}`}>Saved run link</a></div>
      <div className="attendance-direct-counts"><span><strong>{run.ready}</strong> preparing</span><span><strong>{run.waiting}</strong> waiting</span><span><strong>{run.confirmed}</strong> confirmed</span><span><strong>{run.skipped}</strong> skipped</span><span><strong>{run.attention}</strong> need attention</span></div>
      {disconnected && <p>Connection lost. Your run is saved and continues on ADD. Reconnecting…</p>}
      {finished.has(run.status) && !!run.legacy_recheckable && <button className="button secondary" type="button" onClick={() => { setRecheckOpen(true); setError('') }}>Check {count(run.legacy_recheckable)} already in Oracle</button>}
      {run.skipped > 0 && <p>Unknown users, missing CNICs, older IDs Oracle cannot accept, already confirmed punches and deliveries already in progress are shown as skipped.</p>}
      {items.rows.length > 0 && <details><summary>See punch results</summary><ul>{items.rows.map((item) => <li key={item.item_id}>Punch {item.attendance_event_id}: <strong>{item.status.replaceAll('_', ' ').toLowerCase()}</strong>{item.reason ? ` · ${item.reason}` : ''}</li>)}</ul><div className="attendance-direct-pages"><button className="button secondary" type="button" disabled={!cursor} onClick={() => setCursor(0)}>First</button><button className="button secondary" type="button" disabled={!items.next_cursor} onClick={() => setCursor(items.next_cursor || 0)}>Next</button></div></details>}
    </div>}
    {error && !open && <p className="message pattern-blocked" role="alert">{error}</p>}
    <details className="attendance-direct-history" onToggle={(event) => { if (event.currentTarget.open) void loadHistory().catch((failure) => setError(message(failure))) }}><summary>Saved Oracle send runs</summary><ul>{history.map((item) => <li key={item.job_id}><button className="text-button" type="button" onClick={() => openRun(item)}>{dateTime(item.created_at)} · {count(item.selected)} · {item.confirmed} confirmed</button></li>)}</ul>{!history.length && <p>No saved runs yet.</p>}</details>
    {open && <Dialog titleId="direct-ords-title" title={`Send ${count(selected.length)} to Oracle`} description="ADD will submit these saved punches under your administrator decision and show actual Oracle results." onClose={close}><form className="dialog-body" onSubmit={approve}>
      <p>ADD uses a CNIC saved with the punch when available. If the punch has no usable CNIC, your approval uses the CNIC from the current synced user record. This does not prove who owned the terminal user ID when the older punch happened. Unknown current users and missing CNICs are skipped. Older damaged punch IDs are checked against records already in Oracle; they are never sent as new punches. Other ADD delivery holds are overridden. Oracle may still reject or flag a conflicting record; confirmed and in-progress punches are not sent twice.</p>
      {syncedCnicCount > 0 && <p className="message pattern-waiting">{count(syncedCnicCount)} will use the current synced user CNIC because the saved punch has none. The original punch remains unchanged.</p>}
      <details className="attendance-direct-review"><summary>Review selected punches</summary><ul>{selected.slice(0, 20).map((row) => <li key={row.id}>{row.display_name || `User ${row.user_id}`} · {row.device_serial || 'Terminal unknown'} · {dateTime(row.device_event_time)} · {row.ords_status.replaceAll('_', ' ')}</li>)}</ul>{selected.length > 20 && <p>And {selected.length - 20} more selected punches.</p>}</details>
      <label>Reason for sending<textarea required minLength={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
      <label>Administrator password<input type="password" required autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
      {error && <p className="message pattern-blocked" role="alert">{error}</p>}
      <footer className="dialog-actions"><button className="button secondary" type="button" disabled={busy} onClick={close}>Cancel</button><button className="button primary" disabled={busy || !password || reason.trim().length < 3}>{busy ? 'Saving approval…' : `Approve and send ${count(selected.length)}`}</button></footer>
    </form></Dialog>}
    {recheckOpen && run && <Dialog titleId="direct-ords-recheck-title" title="Check saved Oracle punches" description="ADD will compare the approved punches with rows already in Oracle. It will not insert another Oracle punch." onClose={() => { if (!rechecking) { setRecheckOpen(false); setRecheckPassword('') } }}><form className="dialog-body" onSubmit={recheck}>
      <p>Check {count(run.legacy_recheckable || 0)} from this saved run. Only an exact match of terminal, employee, time, CNIC, punch and original ID can become confirmed. Anything else stays for review.</p>
      <label>Administrator password<input type="password" required autoComplete="current-password" value={recheckPassword} onChange={(event) => setRecheckPassword(event.target.value)} /></label>
      {error && <p className="message pattern-blocked" role="alert">{error}</p>}
      <footer className="dialog-actions"><button className="button secondary" type="button" disabled={rechecking} onClick={() => { setRecheckOpen(false); setRecheckPassword('') }}>Cancel</button><button className="button primary" disabled={rechecking || !recheckPassword}>{rechecking ? 'Saving check…' : 'Check Oracle records'}</button></footer>
    </form></Dialog>}
  </section>
}

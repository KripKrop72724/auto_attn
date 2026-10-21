import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { api, queryString } from '../api'
import { dateTime, Dialog, idempotency } from '../App'
import { humanizeStatus } from '../status'
import type { AttendanceEvent, DirectOrdsCnicChoice, DirectOrdsPreview, DirectOrdsPreviewRow } from '../types'

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
const previewRows = (value: Partial<DirectOrdsPreview> | null | undefined): DirectOrdsPreviewRow[] =>
  Array.isArray(value?.rows) ? value.rows.filter((row) => typeof row?.event_id === 'number') : []
const rowLabel = (row: DirectOrdsPreviewRow) =>
  `${row.display_name || `User ${row.user_id}`} · user ${row.user_id} · ${dateTime(row.device_event_time)}`

/** Decisions an administrator makes per punch before a direct Oracle send. */
export function directSendDecisions(rows: DirectOrdsPreviewRow[], choices: Record<number, DirectOrdsCnicChoice>, useCurrent: boolean, sendNow: boolean) {
  const sendable = rows.filter((row) => !row.exclusion)
  const differs = sendable.filter((row) => row.cnic_choice_required && row.cnic_choices.includes('PUNCH_TIME'))
  const currentOnly = sendable.filter((row) => row.cnic_choice_required && !row.cnic_choices.includes('PUNCH_TIME'))
  const autoSoon = sendable.filter((row) => row.auto_release_at_next_snapshot || row.awaiting_next_snapshot)
  const cnicChoices = [
    ...differs.map((row) => ({ event_id: row.event_id, choice: choices[row.event_id] || row.default_cnic_choice || 'PUNCH_TIME' as DirectOrdsCnicChoice })),
    ...(useCurrent ? currentOnly.map((row) => ({ event_id: row.event_id, choice: 'CURRENT' as DirectOrdsCnicChoice })) : []),
  ]
  const skipped = new Set([
    ...rows.filter((row) => row.exclusion).map((row) => row.event_id),
    ...(useCurrent ? [] : currentOnly.map((row) => row.event_id)),
    ...(sendNow ? [] : autoSoon.map((row) => row.event_id)),
  ])
  return {
    refused: rows.filter((row) => row.exclusion), differs, currentOnly, autoSoon, cnicChoices,
    sendNowIds: sendNow ? autoSoon.map((row) => row.event_id) : [], skipped: skipped.size,
  }
}

export function DirectOrdsSend({ selected, clearSelection, refresh, openRequest, selectControl }: {
  selected: AttendanceEvent[]; clearSelection: () => void; refresh: () => void; openRequest: number; selectControl?: ReactNode;
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
  const [checks, setChecks] = useState<DirectOrdsPreviewRow[] | null>(null)
  const [checksError, setChecksError] = useState('')
  const [choices, setChoices] = useState<Record<number, DirectOrdsCnicChoice>>({})
  const [useCurrent, setUseCurrent] = useState(false)
  const [sendNow, setSendNow] = useState(false)
  const approvalKey = useRef(idempotency('direct-ords'))
  const requestSignature = useRef('')
  const activeId = useRef<string | null>(null)
  const selectedIds = useMemo(() => selected.map((row) => row.id), [selected])
  const selectionKey = selectedIds.join(',')
  const decisions = directSendDecisions(checks || [], choices, useCurrent, sendNow)
  useEffect(() => { if (openRequest) { setOpen(true); setError('') } }, [openRequest])
  useEffect(() => {
    // Every open re-reads the server's checks; a decision never carries over to another selection.
    setChecks(null); setChecksError(''); setChoices({}); setUseCurrent(false); setSendNow(false)
    const ids = selectionKey ? selectionKey.split(',').map(Number) : []
    if (!open || !ids.length) return
    const controller = new AbortController()
    void api<DirectOrdsPreview>(`${root}/preview`, {
      method: 'POST', body: JSON.stringify({ event_ids: ids }), signal: controller.signal,
    }).then((result) => { if (!controller.signal.aborted) setChecks(previewRows(result)) })
      .catch((failure) => { if (!controller.signal.aborted) { setChecks([]); setChecksError(message(failure)) } })
    return () => controller.abort()
  }, [open, selectionKey])

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
    if (busy || !selected.length || checks === null) return
    const signature = JSON.stringify([selectedIds, reason.trim(), decisions.cnicChoices, decisions.sendNowIds])
    if (requestSignature.current !== signature) {
      approvalKey.current = idempotency('direct-ords')
      requestSignature.current = signature
    }
    setBusy(true); setError('')
    try {
      const saved = await api<DirectRun>(root, { method: 'POST', body: JSON.stringify({
        event_ids: selectedIds, reason: reason.trim(), password,
        idempotency_key: approvalKey.current,
        cnic_choices: decisions.cnicChoices, send_now_event_ids: decisions.sendNowIds,
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
  return <section className={`attendance-direct-send ${selected.length ? 'has-selection' : ''}`} aria-label="Send selected punches to Oracle">
    <div className="attendance-direct-toolbar">
      {selectControl}
      <div className="attendance-direct-summary"><strong>{selected.length ? `${count(selected.length)} selected` : 'Send saved punches to Oracle'}</strong><small>{selected.length ? 'ADD saves one audited run; Oracle confirmation decides the final status.' : 'Select up to 500 punches. Unknown users are checked by ADD before delivery.'}</small></div>
      <div className="attendance-direct-actions">{selected.length > 0 && <button className="button secondary" type="button" onClick={clearSelection}>Clear selection</button>}<button className="button primary" type="button" disabled={!selected.length} onClick={() => { setOpen(true); setError('') }}>Send {selected.length ? count(selected.length) : 'selected punches'}</button></div>
    </div>
    {run && <div className="attendance-direct-progress" role="status" aria-live="polite">
      <div className="attendance-direct-heading"><strong>{run.status === 'RUNNING' ? 'Preparing delivery' : run.status === 'WAITING_ORACLE' ? 'Waiting for Oracle' : run.attention || run.skipped ? 'Finished with items to review' : 'Finished'}</strong><a href={`/attendance?view=all-events&direct_run=${encodeURIComponent(run.job_id)}`}>Saved run link</a></div>
      <div className="attendance-direct-counts"><span><strong>{run.ready}</strong> preparing</span><span><strong>{run.waiting}</strong> waiting</span><span><strong>{run.confirmed}</strong> confirmed</span><span><strong>{run.skipped}</strong> skipped</span><span><strong>{run.attention}</strong> need attention</span></div>
      {disconnected && <p>Connection lost. Your run is saved and continues on ADD. Reconnecting…</p>}
      {finished.has(run.status) && !!run.legacy_recheckable && <button className="button secondary" type="button" onClick={() => { setRecheckOpen(true); setError('') }}>Check {count(run.legacy_recheckable)} already in Oracle</button>}
      {run.skipped > 0 && <p>Unknown users, missing CNICs, invalid punch times, CNIC choices not made, punches ADD releases automatically, older IDs Oracle cannot accept, already confirmed punches and deliveries already in progress are shown as skipped.</p>}
      {items.rows.length > 0 && <details><summary>See punch results</summary><ul>{items.rows.map((item) => <li key={item.item_id}>Punch {item.attendance_event_id}: <strong>{humanizeStatus(item.status)}</strong>{item.reason ? ` · ${item.reason}` : ''}</li>)}</ul><div className="attendance-direct-pages"><button className="button secondary" type="button" disabled={!cursor} onClick={() => setCursor(0)}>First</button><button className="button secondary" type="button" disabled={!items.next_cursor} onClick={() => setCursor(items.next_cursor || 0)}>Next</button></div></details>}
    </div>}
    {error && !open && <p className="message pattern-blocked" role="alert">{error}</p>}
    <details className="attendance-direct-history" onToggle={(event) => { if (event.currentTarget.open) void loadHistory().catch((failure) => setError(message(failure))) }}><summary>Saved Oracle send runs</summary><ul>{history.map((item) => <li key={item.job_id}><button className="text-button" type="button" onClick={() => openRun(item)}>{dateTime(item.created_at)} · {count(item.selected)} · {item.confirmed} confirmed</button></li>)}</ul>{!history.length && <p>No saved runs yet.</p>}</details>
    {open && <Dialog titleId="direct-ords-title" title={`Send ${count(selected.length)} to Oracle`} description="ADD will submit these saved punches under your administrator decision and show actual Oracle results." onClose={close}><form className="dialog-body" onSubmit={approve}>
      <p>ADD uses a CNIC saved with the punch when available. Otherwise it compares the CNIC the terminal held for this user ID at punch time with today's CNIC, and asks you when they differ or when it has no record: today's CNIC alone does not prove who owned the terminal user ID when an older punch happened. Punches with an invalid clock or impossible date are never sent. Unknown current users and missing CNICs are skipped. Older damaged punch IDs are checked against records already in Oracle; they are never sent as new punches. Other ADD delivery holds are overridden. Oracle may still reject or flag a conflicting record; confirmed and in-progress punches are not sent twice.</p>
      {checks === null && <p className="message pattern-waiting" role="status">Checking punch times, CNICs and automatic releases…</p>}
      {checksError && <p className="message pattern-blocked" role="alert">ADD could not load the punch checks ({checksError}). If you approve, ADD still skips every punch that needs a decision.</p>}
      {decisions.refused.length > 0 && <section className="attendance-direct-check pattern-blocked" aria-label="Punches that cannot be sent"><strong>{count(decisions.refused.length)} cannot be sent</strong><ul>{decisions.refused.slice(0, 20).map((row) => <li key={row.event_id}>{rowLabel(row)} · {row.exclusion_reason || humanizeStatus(row.exclusion || '')}</li>)}</ul></section>}
      {decisions.differs.length > 0 && <section className="attendance-direct-check pattern-waiting" aria-label="CNIC changed since the punch"><strong>CNIC changed since {decisions.differs.length === 1 ? 'this punch' : 'these punches'}</strong><small>The terminal held a different CNIC for this user ID at punch time. ADD sends the punch-time CNIC unless you choose today's.</small>{decisions.differs.slice(0, 50).map((row) => <fieldset key={row.event_id}><legend>{rowLabel(row)}</legend><label><input type="radio" name={`cnic-${row.event_id}`} checked={(choices[row.event_id] || row.default_cnic_choice || 'PUNCH_TIME') === 'PUNCH_TIME'} onChange={() => setChoices((current) => ({ ...current, [row.event_id]: 'PUNCH_TIME' }))} /> CNIC at punch time {row.punch_time_cnic_masked}{row.punch_time_display_name ? ` (${row.punch_time_display_name})` : ''} · recommended</label><label><input type="radio" name={`cnic-${row.event_id}`} checked={choices[row.event_id] === 'CURRENT'} onChange={() => setChoices((current) => ({ ...current, [row.event_id]: 'CURRENT' }))} /> Today's CNIC {row.current_cnic_masked}</label></fieldset>)}{decisions.differs.length > 50 && <small>And {decisions.differs.length - 50} more use the punch-time CNIC.</small>}</section>}
      {decisions.currentOnly.length > 0 && <label className="check-field attendance-direct-check"><input type="checkbox" checked={useCurrent} onChange={(event) => setUseCurrent(event.target.checked)} /><span><strong>Use today's CNIC for {count(decisions.currentOnly.length)}</strong><small>ADD has no single identity record for {decisions.currentOnly.length === 1 ? 'this user ID' : 'these user IDs'} at punch time ({decisions.currentOnly.slice(0, 3).map((row) => `user ${row.user_id}`).join(', ')}{decisions.currentOnly.length > 3 ? ', …' : ''}). Tick only after HR confirmed the same employee held the user ID then. Unticked punches are skipped.</small></span></label>}
      {decisions.autoSoon.length > 0 && <label className="check-field attendance-direct-check"><input type="checkbox" checked={sendNow} onChange={(event) => setSendNow(event.target.checked)} /><span><strong>Send {count(decisions.autoSoon.length)} now anyway</strong><small>ADD's own identity proof releases {decisions.autoSoon.filter((row) => row.auto_release_at_next_snapshot).length} at the next user snapshot and re-checks {decisions.autoSoon.filter((row) => !row.auto_release_at_next_snapshot).length} then (normally within 15 minutes). Unticked punches are skipped and keep waiting for that release.</small></span></label>}
      {checks !== null && decisions.skipped > 0 && <p className="message pattern-waiting">{count(decisions.skipped)} will be skipped; {count(Math.max(0, selected.length - decisions.skipped))} will be approved.</p>}
      <details className="attendance-direct-review"><summary>Review selected punches</summary><ul>{selected.slice(0, 20).map((row) => <li key={row.id}>{row.display_name || `User ${row.user_id}`} · {row.device_serial || 'Terminal unknown'} · {dateTime(row.device_event_time)} · {humanizeStatus(row.ords_status)}</li>)}</ul>{selected.length > 20 && <p>And {selected.length - 20} more selected punches.</p>}</details>
      <label>Reason for sending<textarea required minLength={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
      <label>Administrator password<input type="password" required autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
      {error && <p className="message pattern-blocked" role="alert">{error}</p>}
      <footer className="dialog-actions"><button className="button secondary" type="button" disabled={busy} onClick={close}>Cancel</button><button className="button primary" disabled={busy || checks === null || !password || reason.trim().length < 3}>{busy ? 'Saving approval…' : `Approve and send ${count(selected.length)}`}</button></footer>
    </form></Dialog>}
    {recheckOpen && run && <Dialog titleId="direct-ords-recheck-title" title="Check saved Oracle punches" description="ADD will compare the approved punches with rows already in Oracle. It will not insert another Oracle punch." onClose={() => { if (!rechecking) { setRecheckOpen(false); setRecheckPassword('') } }}><form className="dialog-body" onSubmit={recheck}>
      <p>Check {count(run.legacy_recheckable || 0)} from this saved run. Only an exact match of terminal, employee, time, CNIC, punch and original ID can become confirmed. Anything else stays for review.</p>
      <label>Administrator password<input type="password" required autoComplete="current-password" value={recheckPassword} onChange={(event) => setRecheckPassword(event.target.value)} /></label>
      {error && <p className="message pattern-blocked" role="alert">{error}</p>}
      <footer className="dialog-actions"><button className="button secondary" type="button" disabled={rechecking} onClick={() => { setRecheckOpen(false); setRecheckPassword('') }}>Cancel</button><button className="button primary" disabled={rechecking || !recheckPassword}>{rechecking ? 'Saving check…' : 'Check Oracle records'}</button></footer>
    </form></Dialog>}
  </section>
}

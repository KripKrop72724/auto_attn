import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { api, queryString } from '../api'
import { dateTime, Dialog, idempotency } from '../App'
import { humanizeStatus } from '../status'
import type { AttendanceEvent, DirectOrdsCnicChoice, DirectOrdsCnicDecision, DirectOrdsPreview, DirectOrdsPreviewRow } from '../types'

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
  `${row.display_name || `User ${row.user_id}`} · user ${row.user_id}${row.device_serial ? ` · ${row.device_serial}` : ''} · ${dateTime(row.device_event_time)}`
const SIGNAL_TEXT: Record<string, string> = {
  REVOKED_RECORD: 'the identity record at punch time was revoked',
  OTHER_UID_RECORD: 'a record at punch time belongs to another terminal UID',
  CAPTURED_CNIC_DIFFERS: 'the terminal captured another CNIC with the punch',
  PREVIOUS_HOLDER_DELETED: 'a deleted terminal user held this user ID',
  PUNCH_LINKED_TO_OTHER_USER: 'the punch was linked to another terminal user',
  UID_DIFFERS: "the punch's terminal UID is not today's user's UID",
  IDENTITY_CONFLICT_UNRESOLVED: 'this user has an unresolved identity conflict',
  CNIC_CHANGED_NEAR_PUNCH: 'a record just before or after the punch holds another CNIC',
  RECORDS_TRUNCATED: 'too many identity records to check',
  PUNCH_TIME_CNIC_HELD_BY_OTHER: 'another active terminal user has the punch-time CNIC today',
  PUNCH_BEFORE_FIRST_OBSERVATION: "the punch is older than ADD's first record of this user ID",
  GAP_BRIDGED_SAME_CNIC: 'no record covers the punch; the records just before and after it are one terminal user with the same CNIC',
  NAME_DIFFERS: "the name saved with the punch is not today's user's name",
  RECORD_HOLDER_DELETED_BEFORE_PUNCH: 'the terminal user on the record before the punch was deleted before it',
}
/** Plain-language evidence for one punch; masked CNICs only. */
export function evidenceText(row: DirectOrdsPreviewRow) {
  const notes = (row.evidence_signals || []).filter((signal) => SIGNAL_TEXT[signal]).map((signal) => SIGNAL_TEXT[signal])
  if (row.identity_conflict_code) notes.push(`conflict: ${humanizeStatus(row.identity_conflict_code)}`)
  if (row.other_cnics_masked?.length) notes.push(`other CNIC on record: ${row.other_cnics_masked.join(', ')}`)
  if (row.punch_display_name) notes.push(`name saved with the punch: ${row.punch_display_name}`)
  return notes.join('; ')
}
const groupOf = (row: DirectOrdsPreviewRow): [string, 'CHOOSE_CNIC' | 'CONFIRM_TODAY'] => {
  const kind = row.decision_kind || (row.cnic_choices.includes('PUNCH_TIME') ? 'CHOOSE_CNIC' : 'CONFIRM_TODAY')
  return [row.decision_group || `row|${row.event_id}`, kind]
}
export type DirectSendGroup = { key: string; kind: 'CHOOSE_CNIC' | 'CONFIRM_TODAY'; rows: DirectOrdsPreviewRow[] }

/**
 * Decisions an administrator makes before a direct Oracle send. Punches with
 * identical evidence (one terminal user ID with no record, or the same
 * punch-time CNIC) share one decision; punches whose records disagree are
 * decided one at a time. Every group is shown; nothing is decided unseen.
 */
export function directSendDecisions(rows: DirectOrdsPreviewRow[], choices: Record<string, DirectOrdsCnicChoice>, confirmed: Record<string, boolean>, sendNow: boolean) {
  const sendable = rows.filter((row) => !row.exclusion)
  const groups = new Map<string, DirectSendGroup>()
  for (const row of sendable.filter((item) => item.cnic_choice_required && item.cnic_choices.length)) {
    const [key, kind] = groupOf(row)
    const group = groups.get(key) || { key, kind, rows: [] }
    group.rows.push(row)
    groups.set(key, group)
  }
  const choose = [...groups.values()].filter((group) => group.kind === 'CHOOSE_CNIC')
  const confirm = [...groups.values()].filter((group) => group.kind === 'CONFIRM_TODAY')
  // A withheld default (another active user holds the punch-time CNIC) stays
  // unset: only an explicit click on that group's radio chooses it.
  const chosen = (group: DirectSendGroup): DirectOrdsCnicChoice | null => choices[group.key] || group.rows[0].default_cnic_choice || null
  // Each decision echoes the evidence the dialog showed; ADD skips it if that changed.
  const decision = (row: DirectOrdsPreviewRow, choice: DirectOrdsCnicChoice): DirectOrdsCnicDecision[] => row.evidence_digest
    ? [{ event_id: row.event_id, choice, evidence_digest: row.evidence_digest, held_by_other_confirmed: choice === 'PUNCH_TIME' && Boolean(row.punch_time_cnic_held_by_other) }]
    : []
  const cnicChoices = [
    ...choose.flatMap((group) => {
      const choice = chosen(group)
      return choice ? group.rows.filter((row) => row.cnic_choices.includes(choice)).flatMap((row) => decision(row, choice)) : []
    }),
    ...confirm.filter((group) => confirmed[group.key]).flatMap((group) => group.rows.filter((row) => row.cnic_choices.includes('CURRENT')).flatMap((row) => decision(row, 'CURRENT'))),
  ].sort((left, right) => left.event_id - right.event_id)
  const decided = new Set(cnicChoices.map((item) => item.event_id))
  const autoSoon = sendable.filter((row) => row.auto_release_at_next_snapshot || row.awaiting_next_snapshot)
  const skipped = new Set([
    ...rows.filter((row) => row.exclusion).map((row) => row.event_id),
    ...sendable.filter((row) => row.cnic_choice_required && !decided.has(row.event_id)).map((row) => row.event_id),
    ...(sendNow ? [] : autoSoon.map((row) => row.event_id)),
  ])
  return {
    refused: rows.filter((row) => row.exclusion), choose, confirm, chosen, autoSoon, cnicChoices,
    sendNowIds: sendNow ? autoSoon.map((row) => row.event_id) : [], skipped: skipped.size,
    replacing: sendable.filter((row) => row.replaces_stopped_approval).length,
  }
}
const span = (rows: DirectOrdsPreviewRow[]) => {
  const times = rows.map((row) => row.device_event_time).sort()
  return times.length === 1 ? dateTime(times[0]) : `${dateTime(times[0])} – ${dateTime(times[times.length - 1])}`
}
const groupLabel = (group: DirectSendGroup) => {
  const row = group.rows[0]
  return `${row.display_name || `User ${row.user_id}`} · user ${row.user_id}${row.device_serial ? ` · ${row.device_serial}` : ''} · ${count(group.rows.length)} · ${span(group.rows)}`
}
/** When the records around an uncovered punch end and start; never which one is "nearest". */
const recordEdges = (row: DirectOrdsPreviewRow) => [
  row.record_before_ended_at ? `the old CNIC's record ended ${dateTime(row.record_before_ended_at)}` : '',
  row.record_after_started_at ? `today's CNIC was first recorded ${dateTime(row.record_after_started_at)}` : '',
].filter(Boolean).join('; ')
const confirmReason = (row: DirectOrdsPreviewRow) => {
  if (row.punch_time_cnic === 'CHANGED_NEAR_PUNCH') {
    const edges = recordEdges(row)
    return `No record covers the punch time; the CNIC changed around it${edges ? ` (${edges}, punch ${dateTime(row.device_event_time)})` : ''}. Only a record within 30 minutes of the punch is offered.`
  }
  if (row.punch_time_cnic !== 'NONE') return "ADD's records at the punch time do not prove today's CNIC."
  if (row.record_position === 'BEFORE_FIRST_RECORD') {
    return `These punches are older than ADD's first record of this user ID${row.first_observed_at ? ` (${dateTime(row.first_observed_at)})` : ''}; a user ID reused before then cannot be detected.`
  }
  if (row.record_position === 'BETWEEN_RECORDS') return "No record covers the punch time, and the records around it are not one terminal user record holding today's CNIC throughout."
  if (row.record_position === 'AFTER_LAST_RECORD') return "The punch is after ADD's last record of this user ID."
  return 'ADD has no identity record for this user ID.'
}
const otherCnicLabel = (row: DirectOrdsPreviewRow) => row.punch_time_cnic_basis === 'RECORD_BEFORE_PUNCH'
  ? `CNIC on today's terminal user record until ${row.record_before_ended_at ? dateTime(row.record_before_ended_at) : 'just before the punch'}`
  : row.punch_time_cnic_basis === 'BRIDGED_GAP'
    ? 'CNIC at punch time (records before and after it)' : 'CNIC at punch time'
const todayLabel = (row: DirectOrdsPreviewRow) => row.punch_time_cnic === 'CHANGED_NEAR_PUNCH' && row.record_after_started_at
  ? `Today's CNIC ${row.current_cnic_masked} (recorded from ${dateTime(row.record_after_started_at)})`
  : `Today's CNIC ${row.current_cnic_masked}`

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
  const [choices, setChoices] = useState<Record<string, DirectOrdsCnicChoice>>({})
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({})
  const [sendNow, setSendNow] = useState(false)
  const approvalKey = useRef(idempotency('direct-ords'))
  const requestSignature = useRef('')
  const activeId = useRef<string | null>(null)
  const selectedIds = useMemo(() => selected.map((row) => row.id), [selected])
  const selectionKey = selectedIds.join(',')
  const decisions = directSendDecisions(checks || [], choices, confirmed, sendNow)
  useEffect(() => { if (openRequest) { setOpen(true); setError('') } }, [openRequest])
  useEffect(() => {
    // Every open re-reads the server's checks; a decision never carries over to another selection.
    setChecks(null); setChecksError(''); setChoices({}); setConfirmed({}); setSendNow(false)
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
      {run.skipped > 0 && <p>Unknown users, missing CNICs, invalid punch times, CNIC choices not made or made on records that changed since, punches ADD releases automatically, older IDs Oracle cannot accept, already confirmed punches and deliveries already in progress are shown as skipped.</p>}
      {items.rows.length > 0 && <details><summary>See punch results</summary><ul>{items.rows.map((item) => <li key={item.item_id}>Punch {item.attendance_event_id}: <strong>{humanizeStatus(item.status)}</strong>{item.reason ? ` · ${item.reason}` : ''}</li>)}</ul><div className="attendance-direct-pages"><button className="button secondary" type="button" disabled={!cursor} onClick={() => setCursor(0)}>First</button><button className="button secondary" type="button" disabled={!items.next_cursor} onClick={() => setCursor(items.next_cursor || 0)}>Next</button></div></details>}
    </div>}
    {error && !open && <p className="message pattern-blocked" role="alert">{error}</p>}
    <details className="attendance-direct-history" onToggle={(event) => { if (event.currentTarget.open) void loadHistory().catch((failure) => setError(message(failure))) }}><summary>Saved Oracle send runs</summary><ul>{history.map((item) => <li key={item.job_id}><button className="text-button" type="button" onClick={() => openRun(item)}>{dateTime(item.created_at)} · {count(item.selected)} · {item.confirmed} confirmed</button></li>)}</ul>{!history.length && <p>No saved runs yet.</p>}</details>
    {open && <Dialog titleId="direct-ords-title" title={`Send ${count(selected.length)} to Oracle`} description="ADD will submit these saved punches under your administrator decision and show actual Oracle results." onClose={close}><form className="dialog-body" onSubmit={approve}>
      <p>ADD uses a CNIC saved with the punch when available. Otherwise it compares the CNIC the terminal held for this user ID at punch time with today's CNIC, and asks you when they differ or when it has no record: today's CNIC alone does not prove who owned the terminal user ID when an older punch happened. A punch whose records show another CNIC or person is never sent by hand. Punches with an invalid clock or impossible date are never sent. Unknown current users and missing CNICs are skipped. Older damaged punch IDs are checked against records already in Oracle; they are never sent as new punches. Other ADD delivery holds are overridden. Oracle may still reject or flag a conflicting record; confirmed and in-progress punches are not sent twice.</p>
      {checks === null && <p className="message pattern-waiting" role="status">Checking punch times, CNICs and automatic releases…</p>}
      {checksError && <p className="message pattern-blocked" role="alert">ADD could not load the punch checks ({checksError}). If you approve, ADD still skips every punch that needs a decision.</p>}
      {decisions.refused.length > 0 && <section className="attendance-direct-check pattern-blocked" aria-label="Punches that cannot be sent"><strong>{count(decisions.refused.length)} cannot be sent</strong><ul>{decisions.refused.slice(0, 50).map((row) => <li key={row.event_id}>{rowLabel(row)} · {row.exclusion_reason || humanizeStatus(row.exclusion || '')}{row.exclusion === 'CNIC_CONFLICT' && evidenceText(row) ? ` (${evidenceText(row)})` : ''}</li>)}</ul>{decisions.refused.length > 50 && <small>And {count(decisions.refused.length - 50)} more that cannot be sent.</small>}</section>}
      {decisions.choose.length > 0 && <section className="attendance-direct-check pattern-waiting" aria-label="CNIC changed since the punch"><strong>CNIC changed since {decisions.choose.reduce((total, group) => total + group.rows.length, 0) === 1 ? 'this punch' : 'these punches'}</strong><small>The terminal held a different CNIC for this user ID at or near punch time. ADD sends the punch-time CNIC unless you choose today's. Today's CNIC is offered only for the same terminal user record. Where no record covers the punch, only a record within 30 minutes of it is offered and nothing is preselected.</small>{decisions.choose.map((group) => { const row = group.rows[0]; const choice = decisions.chosen(group); const notes = evidenceText(row); return <fieldset key={group.key}><legend>{groupLabel(group)}</legend>{row.punch_time_cnic_held_by_other && <small className="attention-copy">Another active terminal user has the punch-time CNIC today. Choosing it confirms that HR verified this employee, not that user, punched.</small>}{row.punch_time_cnic === 'CHANGED_NEAR_PUNCH' && <small>{confirmReason(row)}{notes ? ` Evidence: ${notes}.` : ''}</small>}{row.cnic_choices.includes('PUNCH_TIME') && <label><input type="radio" name={`cnic-${group.key}`} checked={choice === 'PUNCH_TIME'} onChange={() => setChoices((current) => ({ ...current, [group.key]: 'PUNCH_TIME' }))} /> {otherCnicLabel(row)} {row.punch_time_cnic_masked}{row.punch_time_display_name ? ` (${row.punch_time_display_name})` : ''}{row.default_cnic_choice === 'PUNCH_TIME' ? ' · recommended' : ''}</label>}{row.cnic_choices.includes('CURRENT') && <label><input type="radio" name={`cnic-${group.key}`} checked={choice === 'CURRENT'} onChange={() => setChoices((current) => ({ ...current, [group.key]: 'CURRENT' }))} /> {todayLabel(row)}</label>}{!choice && <small>No CNIC chosen; {group.rows.length === 1 ? 'this punch is' : 'these punches are'} skipped.</small>}</fieldset> })}</section>}
      {decisions.confirm.length > 0 && <section className="attendance-direct-check pattern-waiting" aria-label="Confirm today's CNIC"><strong>Confirm today's CNIC</strong><small>Tick only after HR confirmed the same employee held the terminal user ID at the punch time. Unticked punches are skipped.</small>{decisions.confirm.map((group) => { const row = group.rows[0]; const notes = evidenceText(row); return <label key={group.key} className="check-field"><input type="checkbox" checked={Boolean(confirmed[group.key])} onChange={(event) => { const checked = event.target.checked; setConfirmed((current) => ({ ...current, [group.key]: checked })) }} /><span><strong>Use today's CNIC {row.current_cnic_masked} for {groupLabel(group)}</strong><small>{confirmReason(row)}{notes ? ` Evidence: ${notes}.` : ''}</small></span></label> })}</section>}
      {decisions.autoSoon.length > 0 && <label className="check-field attendance-direct-check"><input type="checkbox" checked={sendNow} onChange={(event) => setSendNow(event.target.checked)} /><span><strong>Send {count(decisions.autoSoon.length)} now anyway</strong><small>ADD's own identity proof releases {decisions.autoSoon.filter((row) => row.auto_release_at_next_snapshot).length} at the next user snapshot and re-checks {decisions.autoSoon.filter((row) => !row.auto_release_at_next_snapshot).length} then (normally within 15 minutes). Unticked punches are skipped and keep waiting for that release.</small></span></label>}
      {decisions.replacing > 0 && <p className="message pattern-waiting">{count(decisions.replacing)} replace an earlier approval that stopped before anything was sent to Oracle.</p>}
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

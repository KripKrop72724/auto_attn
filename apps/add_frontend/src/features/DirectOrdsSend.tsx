import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { api, queryString } from '../api'
import { Dialog, idempotency } from '../App'
import { Disclosure } from '../ui'
import { plainReason } from '../words/attendanceReasons'
import { GROUP_CHIP, rowGroup, rowTime } from './attendance/attendanceWords'
import type { AttendanceEvent, DirectOrdsCnicChoice, DirectOrdsCnicDecision, DirectOrdsPreview, DirectOrdsPreviewRow } from '../types'
import { plainErrorText } from '../lib/plainApiError'
import { STEP_UP } from '../words/glossary'

export type DirectRun = {
  job_id: string; status: string; created_at: string; actor: string; selected: number;
  ready: number; waiting: number; confirmed: number; skipped: number; attention: number; legacy_recheckable?: number;
}
type DirectItem = { item_id: string; attendance_event_id: number; status: string; reason: string | null; error_code: string | null }
type ItemPage = { rows: DirectItem[]; next_cursor: number | null }
const root = '/api/v2/attendance-direct-ords'
const finished = new Set(['COMPLETED', 'COMPLETED_WITH_REVIEW'])
const count = (value: number) => `${value.toLocaleString()} ${value === 1 ? 'check-in' : 'check-ins'}`
// A plain sentence only; the backend text never shows (writing rule 6).
const message = (error: unknown) => plainErrorText(error, "Couldn't save this. Try again.")
const previewRows = (value: Partial<DirectOrdsPreview> | null | undefined): DirectOrdsPreviewRow[] =>
  Array.isArray(value?.rows) ? value.rows.filter((row) => typeof row?.event_id === 'number') : []
const who = (row: { display_name: string | null; user_id: string }) => `${row.display_name || 'Name not known'} · Employee ID ${row.user_id}`
const rowLabel = (row: DirectOrdsPreviewRow) => `${who(row)} · ${rowTime(row.device_event_time)}`
const SIGNAL_TEXT: Record<string, string> = {
  REVOKED_RECORD: "the person's record at that time was removed",
  OTHER_UID_RECORD: 'a record at that time belongs to another person on the machine',
  CAPTURED_CNIC_DIFFERS: 'the machine saved another CNIC with the check-in',
  PREVIOUS_HOLDER_DELETED: 'someone deleted from the machine had this Employee ID',
  PUNCH_LINKED_TO_OTHER_USER: 'the check-in was linked to another person on the machine',
  UID_DIFFERS: "the check-in's machine record isn't today's person's record",
  IDENTITY_CONFLICT_UNRESOLVED: "this person has a conflict that isn't sorted out",
  CNIC_CHANGED_NEAR_PUNCH: 'a record just before or after the check-in has another CNIC',
  RECORDS_TRUNCATED: 'too many records to check them all',
  PUNCH_TIME_CNIC_HELD_BY_OTHER: 'another person on the machine has the old CNIC today',
  PUNCH_BEFORE_FIRST_OBSERVATION: "the check-in is older than the dashboard's first record of this Employee ID",
  GAP_BRIDGED_SAME_CNIC: 'no record covers the check-in; the records just before and after it are the same person with the same CNIC',
  NAME_DIFFERS: "the name saved with the check-in isn't today's person's name",
  RECORD_HOLDER_DELETED_BEFORE_PUNCH: 'the person on the record before the check-in was deleted before it',
}
/** Plain-language evidence for one punch; masked CNICs only. */
export function evidenceText(row: DirectOrdsPreviewRow) {
  const notes = (row.evidence_signals || []).filter((signal) => SIGNAL_TEXT[signal]).map((signal) => SIGNAL_TEXT[signal])
  if (row.identity_conflict_code) notes.push('a conflict is recorded for this person')
  if (row.other_cnics_masked?.length) notes.push(`other CNIC on record: ${row.other_cnics_masked.join(', ')}`)
  if (row.punch_display_name) notes.push(`name saved with the check-in: ${row.punch_display_name}`)
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
  return times.length === 1 ? rowTime(times[0]) : `${rowTime(times[0])} – ${rowTime(times[times.length - 1])}`
}
const groupLabel = (group: DirectSendGroup) => `${who(group.rows[0])} · ${count(group.rows.length)} · ${span(group.rows)}`
/** When the records around an uncovered check-in end and start; never which one is "nearest". */
const recordEdges = (row: DirectOrdsPreviewRow) => [
  row.record_before_ended_at ? `the old CNIC's record ended ${rowTime(row.record_before_ended_at)}` : '',
  row.record_after_started_at ? `today's CNIC was first recorded ${rowTime(row.record_after_started_at)}` : '',
].filter(Boolean).join('; ')
const confirmReason = (row: DirectOrdsPreviewRow) => {
  if (row.punch_time_cnic === 'CHANGED_NEAR_PUNCH') {
    const edges = recordEdges(row)
    return `No record covers the check-in time; the CNIC changed around it${edges ? ` (${edges}, check-in ${rowTime(row.device_event_time)})` : ''}. Only a record within 30 minutes of the check-in is offered.`
  }
  if (row.punch_time_cnic !== 'NONE') return "The records at the check-in time don't prove today's CNIC."
  if (row.record_position === 'BEFORE_FIRST_RECORD') {
    return `These check-ins are older than the dashboard's first record of this Employee ID${row.first_observed_at ? ` (${rowTime(row.first_observed_at)})` : ''}; an Employee ID used by someone else before then can't be seen.`
  }
  if (row.record_position === 'BETWEEN_RECORDS') return "No record covers the check-in time, and the records around it don't show the same person with today's CNIC all along."
  if (row.record_position === 'AFTER_LAST_RECORD') return "The check-in is after the dashboard's last record of this Employee ID."
  return 'The dashboard has no record of this Employee ID.'
}
const otherCnicLabel = (row: DirectOrdsPreviewRow) => row.punch_time_cnic_basis === 'RECORD_BEFORE_PUNCH'
  ? `CNIC on today's person's record until ${row.record_before_ended_at ? rowTime(row.record_before_ended_at) : 'just before the check-in'}`
  : row.punch_time_cnic_basis === 'BRIDGED_GAP'
    ? 'CNIC at check-in time (records before and after it)' : 'CNIC at check-in time'
const todayLabel = (row: DirectOrdsPreviewRow) => row.punch_time_cnic === 'CHANGED_NEAR_PUNCH' && row.record_after_started_at
  ? `Today's CNIC ${row.current_cnic_masked} (recorded from ${rowTime(row.record_after_started_at)})`
  : `Today's CNIC ${row.current_cnic_masked}`
const ITEM_WORDS: Record<string, string> = {
  CONFIRMED: 'Sent', SKIPPED: 'Not sent', WAITING_ORACLE: 'Waiting for the HR system', READY: 'Getting ready',
  NEEDS_REVIEW: 'Needs checking', ATTENTION: 'Needs checking', CHECKING_COPY: 'Checking HR records', SUPERSEDED: 'Replaced',
}
const itemWords = (status: string) => ITEM_WORDS[status] || 'In progress'

/**
 * "Send by hand": the dialog that sends chosen check-ins to the HR system and
 * the progress of the last sending task. The selection bar that opens it is
 * on the Check-ins list (`openRequest`). Reason and password stay (owner).
 */
export function DirectOrdsSend({ selected, clearSelection, refresh, openRequest, onCopied }: {
  selected: AttendanceEvent[]; clearSelection: () => void; refresh: () => void; openRequest: number; onCopied?: (text: string) => void;
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [run, setRun] = useState<DirectRun | null>(null)
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

  const openRun = useCallback((value: DirectRun) => {
    activeId.current = value.job_id
    setRun(value); setCursor(0); setItems({ rows: [], next_cursor: null })
    const url = new URL(window.location.href)
    url.searchParams.set('direct_run', value.job_id)
    url.searchParams.delete('view')
    url.searchParams.delete('tool')
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
      refresh()
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
  const runLink = run ? `${window.location.origin}/attendance?direct_run=${encodeURIComponent(run.job_id)}` : ''
  const copyLink = async () => {
    try { await navigator.clipboard?.writeText(runLink); onCopied?.('Link copied') } catch { onCopied?.('Copy the link from the address bar.') }
  }
  return <section className="attendance-direct-send" aria-label="Send by hand">
    {run && <div className="attendance-direct-progress" role="status" aria-live="polite">
      <div className="attendance-direct-heading"><strong>{run.status === 'RUNNING' ? 'Getting ready' : run.status === 'WAITING_ORACLE' ? 'Waiting for the HR system' : run.attention || run.skipped ? 'Done · some need checking' : 'Done'}</strong><button className="text-button" type="button" onClick={() => void copyLink()}>Copy link</button></div>
      <div className="attendance-direct-counts"><span><strong>{run.ready}</strong> getting ready</span><span><strong>{run.waiting}</strong> waiting</span><span><strong>{run.confirmed}</strong> sent</span><span><strong>{run.skipped}</strong> not sent</span><span><strong>{run.attention}</strong> need checking</span></div>
      {disconnected && <p>Connection lost. Sending continues; reconnecting…</p>}
      {finished.has(run.status) && !!run.legacy_recheckable && <button className="button secondary" type="button" onClick={() => { setRecheckOpen(true); setError('') }}>Check {count(run.legacy_recheckable)} in HR records</button>}
      {run.skipped > 0 && <p>Some check-ins were not sent. Open the list to see why.</p>}
      {items.rows.length > 0 && <details><summary>See each check-in</summary><ul>{items.rows.map((item) => <li key={item.item_id}><strong>{itemWords(item.status)}</strong>{item.error_code ? ` · ${plainReason('direct', item.error_code).sentence}` : ''} <span className="attendance-direct-ref" data-technical>record {item.attendance_event_id} · {item.status}{item.error_code ? ` · ${item.error_code}` : ''}</span></li>)}</ul><div className="attendance-direct-pages"><button className="button secondary" type="button" disabled={!cursor} onClick={() => setCursor(0)}>First</button><button className="button secondary" type="button" disabled={!items.next_cursor} onClick={() => setCursor(items.next_cursor || 0)}>Next</button></div></details>}
    </div>}
    {error && !open && <p className="message pattern-blocked" role="alert">{error}</p>}
    {open && <Dialog titleId="direct-ords-title" title={`Send ${count(selected.length)} to the HR system (Oracle)`} description="You are approving these check-ins. You'll see what the HR system says." onClose={close}><form className="dialog-body" onSubmit={approve}>
      <ul className="attendance-direct-rules">
        <li>We use the CNIC saved with the check-in.</li>
        <li>If the CNIC changed, you choose which one to send.</li>
        <li>Check-ins with a wrong time, or someone else's Employee ID, are never sent.</li>
      </ul>
      <Disclosure summary="More about these checks"><p>When no CNIC was saved with the check-in, the dashboard compares the CNIC the machine had for this Employee ID at the check-in time with today's CNIC, and asks you when they differ or when it has no record: today's CNIC alone does not prove who had the Employee ID when an older check-in happened. A check-in whose records show another CNIC or person is never sent by hand. Check-ins with a wrong machine time or an impossible date are never sent. People not on the machine any more and missing CNICs are not sent. Older damaged record numbers are checked against the HR system records; they are never sent as new check-ins. The HR system may still refuse or flag a record that conflicts; check-ins already sent or on the way are not sent twice.</p></Disclosure>
      {checks === null && <p className="message pattern-waiting" role="status">Checking times, CNICs and automatic sending…</p>}
      {checksError && <p className="message pattern-blocked" role="alert">Couldn't load the checks. If you send, every check-in that needs a decision is still not sent.</p>}
      {decisions.refused.length > 0 && <section className="attendance-direct-check pattern-blocked" aria-label="Check-ins that can't be sent"><strong>{count(decisions.refused.length)} can't be sent</strong><ul>{decisions.refused.slice(0, 50).map((row) => <li key={row.event_id}>{rowLabel(row)} · {plainReason('direct', row.exclusion).sentence}{row.exclusion === 'CNIC_CONFLICT' && evidenceText(row) ? ` (${evidenceText(row)})` : ''}</li>)}</ul>{decisions.refused.length > 50 && <small>And {count(decisions.refused.length - 50)} more that can't be sent.</small>}</section>}
      {decisions.choose.length > 0 && <section className="attendance-direct-check pattern-waiting" aria-label="CNIC changed since the check-in"><strong>The CNIC changed for {count(decisions.choose.reduce((total, group) => total + group.rows.length, 0))}</strong><small>The machine had a different CNIC for this Employee ID at or near the check-in time. The CNIC from the check-in time is sent unless you choose today's. Today's CNIC is offered only for the same person's record. Where no record covers the check-in, only a record within 30 minutes of it is offered and nothing is chosen for you.</small>{decisions.choose.map((group) => { const row = group.rows[0]; const choice = decisions.chosen(group); const notes = evidenceText(row); return <fieldset key={group.key}><legend>{groupLabel(group)}</legend>{row.punch_time_cnic_held_by_other && <small className="attention-copy">Another person on the machine has the old CNIC today. Choosing it means HR confirmed this person, not that one, checked in.</small>}{row.punch_time_cnic === 'CHANGED_NEAR_PUNCH' && <small>{confirmReason(row)}{notes ? ` Records: ${notes}.` : ''}</small>}{row.cnic_choices.includes('PUNCH_TIME') && <label><input type="radio" name={`cnic-${group.key}`} checked={choice === 'PUNCH_TIME'} onChange={() => setChoices((current) => ({ ...current, [group.key]: 'PUNCH_TIME' }))} /> {otherCnicLabel(row)} {row.punch_time_cnic_masked}{row.punch_time_display_name ? ` (${row.punch_time_display_name})` : ''}{row.default_cnic_choice === 'PUNCH_TIME' ? ' · recommended' : ''}</label>}{row.cnic_choices.includes('CURRENT') && <label><input type="radio" name={`cnic-${group.key}`} checked={choice === 'CURRENT'} onChange={() => setChoices((current) => ({ ...current, [group.key]: 'CURRENT' }))} /> {todayLabel(row)}</label>}{!choice && <small>No CNIC chosen, so {group.rows.length === 1 ? 'this check-in is' : 'these check-ins are'} not sent.</small>}</fieldset> })}</section>}
      {decisions.confirm.length > 0 && <section className="attendance-direct-check pattern-waiting" aria-label="Use the current CNIC?"><strong>Use the current CNIC?</strong><small>Tick only if HR confirmed this person checked in. Unticked check-ins are not sent.</small>{decisions.confirm.map((group) => { const row = group.rows[0]; const notes = evidenceText(row); return <label key={group.key} className="check-field"><input type="checkbox" checked={Boolean(confirmed[group.key])} onChange={(event) => { const checked = event.target.checked; setConfirmed((current) => ({ ...current, [group.key]: checked })) }} /><span><strong>Use today's CNIC {row.current_cnic_masked} for {groupLabel(group)}</strong><small>{confirmReason(row)}{notes ? ` Records: ${notes}.` : ''}</small></span></label> })}</section>}
      {decisions.autoSoon.length > 0 && <label className="check-field attendance-direct-check"><input type="checkbox" checked={sendNow} onChange={(event) => setSendNow(event.target.checked)} /><span><strong>Send {count(decisions.autoSoon.length)} now anyway</strong><small>The dashboard sends {decisions.autoSoon.filter((row) => row.auto_release_at_next_snapshot).length} by itself at the next read of the machine's list of people, and checks {decisions.autoSoon.filter((row) => !row.auto_release_at_next_snapshot).length} again then (normally within 15 minutes). Unticked check-ins are not sent now and keep waiting for that.</small></span></label>}
      {decisions.replacing > 0 && <p className="message pattern-waiting">{count(decisions.replacing)} replace an earlier approval that stopped before anything was sent.</p>}
      {checks !== null && decisions.skipped > 0 && <p className="message pattern-waiting">{count(decisions.skipped)} will not be sent; {count(Math.max(0, selected.length - decisions.skipped))} will be sent.</p>}
      <details className="attendance-direct-review"><summary>Review the chosen check-ins</summary><ul>{selected.slice(0, 20).map((row) => <li key={row.id}>{row.display_name || 'Name not known'} · Employee ID {row.user_id} · {rowTime(row.device_event_time)} · {GROUP_CHIP[rowGroup(row as Parameters<typeof rowGroup>[0])].label}</li>)}</ul>{selected.length > 20 && <p>And {count(selected.length - 20)} more.</p>}</details>
      <label>{STEP_UP.why}<textarea required minLength={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} /></label><small className="step-up-help">{STEP_UP.whyHelp(3)}</small>
      <label>{STEP_UP.password}<input type="password" required autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label><small className="step-up-help">{STEP_UP.passwordHelp}</small>
      {error && <p className="message pattern-blocked" role="alert">{error}</p>}
      <footer className="dialog-actions"><button className="button secondary" type="button" disabled={busy} onClick={close}>Cancel</button><button className="button primary" disabled={busy || checks === null || !password || reason.trim().length < 3}>{busy ? 'Sending…' : `Send ${count(selected.length)}`}</button></footer>
    </form></Dialog>}
    {recheckOpen && run && <Dialog titleId="direct-ords-recheck-title" title="Check HR records" description="We'll look for these check-ins in the HR system. Nothing new is sent." onClose={() => { if (!rechecking) { setRecheckOpen(false); setRecheckPassword('') } }}><form className="dialog-body" onSubmit={recheck}>
      <p>Check {count(run.legacy_recheckable || 0)} from this sending task. Only an exact match of machine, person, time, CNIC, type and record number counts as sent. Anything else stays for checking.</p>
      <label>{STEP_UP.password}<input type="password" required autoComplete="current-password" value={recheckPassword} onChange={(event) => setRecheckPassword(event.target.value)} /></label><small className="step-up-help">{STEP_UP.passwordHelp}</small>
      {error && <p className="message pattern-blocked" role="alert">{error}</p>}
      <footer className="dialog-actions"><button className="button secondary" type="button" disabled={rechecking} onClick={() => { setRecheckOpen(false); setRecheckPassword('') }}>Cancel</button><button className="button primary" disabled={rechecking || !recheckPassword}>{rechecking ? 'Checking…' : 'Check now'}</button></footer>
    </form></Dialog>}
  </section>
}

// Unreadable record (08-advanced 1.6, today "Terminal source exception"). The
// sheet explains the record's real problem, who looked at it, and offers Mark
// as seen in a sticky footer. Its safeguard is unchanged: a note of at least
// 10 letters and your password, checked by the server for every record. Show
// raw record lives under Technical details with its own note and password, and
// hides itself after 60 s.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import { Button, Callout, DivisionTag, ErrorState, Sheet, Skeleton, StatusChip, TechnicalDetails } from '../../ui'
import { idempotency } from '../../lib/idempotency'
import { dayTime } from '../../lib/time'
import { EMPLOYEE_ID } from '../../words/people'
import { RECORD_SHEET, recordExplanation, seenToast } from '../../words/recover'
import type { SourceException, SourceExceptionReveal } from '../../types'
import type { Toast } from '../../ui/Toast'
import type { Namer } from './names'
import { useRevealTimer } from './useRevealTimer'

const newestCondition = (row: SourceException) =>
  row.source_condition === 'TERMINAL_NEWEST_RECORD_UNSETTLED' || row.source_condition === 'TERMINAL_NEWEST_RECORD_UNCONFIRMED'

const readWords = (receipt: NonNullable<SourceException['slot_receipts']>[number]) =>
  receipt.punch_received === false ? 'Looks like a check-in the dashboard has not received.'
    : receipt.punch_received ? 'Looks like a check-in the dashboard already has from another read.'
      : receipt.decodes_as_punch ? "Its saved reading can't be used; treated as a check-in the dashboard has not received."
        : 'Not a check-in.'

export function UnreadableRecordSheet({ seed, namer, remaining, hasNext, onNext, onClose, onChanged, toast }: {
  seed: SourceException
  namer: Namer
  /** Records still to look at in this recovery, when the list is scoped to one. */
  remaining: number | null
  hasNext: boolean
  onNext: () => void
  onClose: () => void
  onChanged: () => Promise<void>
  toast: Toast
}) {
  const [row, setRow] = useState<SourceException | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [justSeen, setJustSeen] = useState(false)
  const [rawReason, setRawReason] = useState('')
  const [rawPassword, setRawPassword] = useState('')
  const [rawBusy, setRawBusy] = useState(false)
  const [revealed, setRevealed] = useState<SourceExceptionReveal | null>(null)
  const noteRef = useRef<HTMLTextAreaElement>(null)
  const left = useRevealTimer(revealed, () => setRevealed(null))
  const load = useCallback(async () => {
    setLoadError(null)
    try {
      setRow(await api<SourceException>(`/api/v1/source-exceptions/${seed.id}`))
    } catch (reasonValue) {
      setLoadError(reasonValue)
    }
  }, [seed.id])
  useEffect(() => {
    setRow(null)
    setFormOpen(false)
    setJustSeen(false)
    setReason('')
    setPassword('')
    setRevealed(null)
    void load()
  }, [load])
  useEffect(() => () => setRevealed(null), [])
  useEffect(() => { if (formOpen) noteRef.current?.focus() }, [formOpen])

  const current = row || seed
  const place = namer({ connector_id: current.connector_id, display_name: current.display_name, zone_id: current.zone_id })
  const explanation = recordExplanation(current)
  const seen = current.review_state === 'REVIEWED'
  const canSave = reason.trim().length >= 10 && Boolean(password) && !busy

  const markSeen = async () => {
    if (!row) return
    if (!formOpen) { setFormOpen(true); return }
    if (!canSave) return
    setBusy(true)
    try {
      const updated = await api<SourceException>(`/api/v1/source-exceptions/${row.id}/review`, {
        method: 'POST',
        body: JSON.stringify({ reason: reason.trim(), password, idempotency_key: idempotency('source-exception-review') }),
      })
      setRow(updated)
      setJustSeen(true)
      setFormOpen(false)
      setPassword('')
      setReason('')
      toast.notice(seenToast(row, remaining === null ? null : Math.max(0, remaining - 1)))
      await onChanged()
    } catch (error) {
      setPassword('')
      toast.error(error instanceof Error && error.message ? error.message : RECORD_SHEET.failed)
    } finally {
      setBusy(false)
    }
  }
  const showRaw = async () => {
    if (!row) return
    setRawBusy(true)
    try {
      setRevealed(await api<SourceExceptionReveal>(`/api/v1/source-exceptions/${row.id}/reveal`, {
        method: 'POST',
        body: JSON.stringify({ reason: rawReason.trim(), password: rawPassword, idempotency_key: idempotency('source-exception-reveal') }),
      }))
      setRawPassword('')
      setRawReason('')
      toast.notice(RECORD_SHEET.rawShown)
    } catch (error) {
      setRawPassword('')
      toast.error(error instanceof Error && error.message ? error.message : RECORD_SHEET.failed)
    } finally {
      setRawBusy(false)
    }
  }

  const footer = row && (
    <div className="recover-record-footer">
      {current.disposition === 'IDENTITY_UNRESOLVED' && !seen && <p className="recover-muted">{RECORD_SHEET.unmatchedFooter}</p>}
      {formOpen && !seen && (
        <form className="recover-form" onSubmit={(event) => { event.preventDefault(); void markSeen() }}>
          <label>
            <span>{RECORD_SHEET.note}</span>
            <textarea ref={noteRef} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} placeholder={RECORD_SHEET.notePlaceholder} />
          </label>
          <small className="recover-counter" aria-live="polite">{`${Math.min(reason.trim().length, 10)} / 10`}</small>
          <label>
            <span>{RECORD_SHEET.password}</span>
            <input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
          </label>
        </form>
      )}
      <div className="recover-record-actions">
        {seen ? (
          <>
            <StatusChip tone="working" label={justSeen ? RECORD_SHEET.seen : 'Seen'} />
            {hasNext && <Button variant="primary" onClick={onNext}>{RECORD_SHEET.next}</Button>}
          </>
        ) : (
          <>
            {formOpen && <Button key="cancel" variant="ghost" onClick={() => { setFormOpen(false); setReason(''); setPassword('') }}>Cancel</Button>}
            <Button key="mark" variant="primary" busy={busy} busyLabel={RECORD_SHEET.saving} disabled={formOpen && !canSave} onClick={() => { if (formOpen) void markSeen(); else setFormOpen(true) }}>
              {RECORD_SHEET.markSeen}
            </Button>
          </>
        )}
      </div>
    </div>
  )

  return (
    <Sheet
      title={RECORD_SHEET.title}
      description={`${place.name} · record ${current.ordinal.toLocaleString()}`}
      onClose={onClose}
      dirty={Boolean(reason || password || rawReason || rawPassword)}
      footer={footer || undefined}
      className="recover-sheet recover-record-sheet"
    >
      {!row && !loadError && <Skeleton variant="row" count={3} label="Loading the record" />}
      {Boolean(loadError) && <ErrorState compact title="Couldn't load this record." error={loadError} onRetry={load} />}
      {row && (
        <>
          <section className="recover-detail-block">
            <p className="recover-record-place"><strong>{place.name}</strong> <DivisionTag division={place.division} /></p>
            <h3>{explanation.title}</h3>
            <p>{explanation.body}</p>
            <Callout tone={current.disposition === 'IDENTITY_UNRESOLVED' ? 'warning' : 'info'} title={current.disposition === 'IDENTITY_UNRESOLVED' ? 'Not matched to an employee' : RECORD_SHEET.notSent}>
              {current.disposition === 'IDENTITY_UNRESOLVED' ? 'It is not sent to the HR system until the employee is fixed.' : RECORD_SHEET.seenChanges}
            </Callout>
            <p className="recover-muted">{current.observed_user_id ? `${EMPLOYEE_ID} ${current.observed_user_id}` : `${EMPLOYEE_ID} not known`}</p>
          </section>
          <section className="recover-detail-block">
            <h3>{RECORD_SHEET.whoLooked}</h3>
            {(row.reviews || []).map((review) => (
              <div key={review.review_id} className="recover-review">
                <strong data-user-text>{review.actor}</strong> · {dayTime(review.created_at)}
                <p data-user-text>{review.reason}</p>
              </div>
            ))}
            {!row.reviews?.length && <p className="recover-muted">{RECORD_SHEET.nobody}</p>}
          </section>
          <TechnicalDetails>
            <dl className="recover-tech">
              <div><dt>Device</dt><dd>{row.display_name || row.connector_id || 'Unknown'}</dd></div>
              <div><dt>Machine serial number</dt><dd>{row.terminal_serial}</dd></div>
              <div><dt>Generation / record number</dt><dd>{row.terminal_generation} / {row.ordinal.toLocaleString()}</dd></div>
              <div><dt>Record source</dt><dd>{row.source_kind}</dd></div>
              <div><dt>Problem code</dt><dd>{row.error_code || row.disposition}{row.source_condition ? ` · ${row.source_condition}` : ''}</dd></div>
              <div><dt>Machine record</dt><dd>Attendance UID {row.observed_uid || '—'} · User {row.observed_user_id || '—'} · timestamp {row.raw_timestamp ?? 'invalid'}</dd></div>
              <div><dt>Records read past it</dt><dd>{row.cursor_advanced ? `Committed through ${row.source_committed_cursor.toLocaleString()}` : 'Awaiting durable commit'}</dd></div>
              <div><dt>SHA-256 digest</dt><dd><code>{row.raw_record_digest}</code></dd></div>
              <div><dt>Record key</dt><dd><code>{row.terminal_record_key}</code></dd></div>
              {row.operator_reason && <div><dt>Server note</dt><dd>{row.operator_reason}</dd></div>}
              {newestCondition(row) && <div><dt>Note</dt><dd>Marking it as seen does not settle the newest record or read its check-in; only a later full recovery does.</dd></div>}
            </dl>
            {!!row.slot_receipts?.length && (
              <section aria-label="Later reads of this record">
                <p><strong>Later reads of this record</strong></p>
                <ul className="recover-reads">
                  {row.slot_receipts.map((receipt) => (
                    <li key={receipt.manifest_id}>{dayTime(receipt.observed_at)} · {readWords(receipt)} <code>{receipt.raw_record_digest.slice(0, 16)}</code></li>
                  ))}
                </ul>
              </section>
            )}
            {revealed && (
              <section className="recover-raw" aria-live="polite">
                <p><strong>{RECORD_SHEET.showRaw}</strong> · {RECORD_SHEET.hiddenIn(left)}</p>
                <p>Hex <code>{revealed.raw_record_hex}</code></p>
                <p>Base64 <code>{revealed.raw_record_b64}</code></p>
                <Button size="sm" onClick={() => setRevealed(null)}>{RECORD_SHEET.hideNow}</Button>
              </section>
            )}
            <form className="recover-form" aria-label={RECORD_SHEET.showRaw} onSubmit={(event) => { event.preventDefault(); void showRaw() }}>
              <p><strong>{RECORD_SHEET.showRaw}</strong></p>
              <label>
                <span>Note (why you need the raw record)</span>
                <textarea value={rawReason} onChange={(event) => setRawReason(event.target.value)} maxLength={500} placeholder={RECORD_SHEET.notePlaceholder} />
              </label>
              <label>
                <span>Your password to show it</span>
                <input type="password" autoComplete="current-password" value={rawPassword} onChange={(event) => setRawPassword(event.target.value)} />
              </label>
              <Button type="submit" busy={rawBusy} busyLabel={RECORD_SHEET.checkingPassword} disabled={rawReason.trim().length < 10 || !rawPassword}>{RECORD_SHEET.showRaw}</Button>
            </form>
          </TechnicalDetails>
        </>
      )}
    </Sheet>
  )
}

// "Record changed on the machine" (today "Terminal-history change"). Reached
// from Recovery details › Technical details. Showing the raw record keeps its
// safeguard exactly: a note of at least 10 letters and your password.
import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api'
import { Button, ErrorState, Sheet, Skeleton, TechnicalDetails } from '../../ui'
import { idempotency } from '../../lib/idempotency'
import { dayTime } from '../../lib/time'
import { problemShort, RECORD_SHEET } from '../../words/recover'
import type { ReconciliationDivergenceDetail, ReconciliationDivergenceReveal } from '../../types'
import type { Toast } from '../../ui/Toast'
import { useRevealTimer } from './useRevealTimer'

const stateWords = (state: string) => {
  switch (state) {
    case 'PROBING': return 'Reading it again to be sure'
    case 'CONFIRMED': return 'The change is confirmed'
    case 'TRANSIENT': return 'It matched when read again'
    default: return 'Being checked'
  }
}

export function RecordChangedSheet({ divergenceId, onClose, toast }: { divergenceId: string; onClose: () => void; toast: Toast }) {
  const [row, setRow] = useState<ReconciliationDivergenceDetail | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [revealed, setRevealed] = useState<ReconciliationDivergenceReveal | null>(null)
  const [busy, setBusy] = useState(false)
  const remaining = useRevealTimer(revealed, () => setRevealed(null))
  const load = useCallback(async () => {
    setError(null)
    try {
      setRow(await api<ReconciliationDivergenceDetail>(`/api/v1/reconciliation-divergences/${divergenceId}`))
    } catch (reasonValue) {
      setError(reasonValue)
    }
  }, [divergenceId])
  useEffect(() => { void load() }, [load])
  const reveal = async () => {
    setBusy(true)
    try {
      setRevealed(await api<ReconciliationDivergenceReveal>(`/api/v1/reconciliation-divergences/${divergenceId}/reveal`, {
        method: 'POST',
        body: JSON.stringify({ reason: reason.trim(), password, idempotency_key: idempotency('source-divergence-reveal') }),
      }))
      setPassword('')
      setReason('')
      toast.notice(RECORD_SHEET.rawShown)
    } catch (failure) {
      setPassword('')
      toast.error(failure instanceof Error && failure.message ? failure.message : RECORD_SHEET.failed)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Sheet
      title="Record changed on the machine"
      description={row ? `Record ${row.ordinal.toLocaleString()} · ${stateWords(row.state)}` : 'Loading…'}
      onClose={onClose}
      dirty={Boolean(reason || password)}
      className="recover-sheet"
    >
      {!row && !error && <Skeleton variant="row" count={3} label="Loading the record" />}
      {Boolean(error) && <ErrorState compact title="Couldn't load this record." error={error} onRetry={load} />}
      {row && (
        <>
          <section className="recover-detail-block">
            <h3>Read again to confirm</h3>
            <p>This record looked different when the machine was read again. It is read again a few times to be sure. Nothing already sent to the HR system is changed.</p>
            <p>Before: {problemShort(row.old_disposition)} · Now: {problemShort(row.new_disposition)}</p>
            <p className="recover-muted">{`Read ${row.observations.length} times.`}</p>
          </section>
          <TechnicalDetails>
            <dl className="recover-tech">
              <div><dt>Recovery</dt><dd>{row.job_id || 'Unknown'}</dd></div>
              <div><dt>Ordinal</dt><dd>{row.ordinal.toLocaleString()}</dd></div>
              <div><dt>State</dt><dd>{row.state}</dd></div>
              <div><dt>Original disposition</dt><dd>{row.old_disposition || 'Unknown'}</dd></div>
              <div><dt>Observed disposition</dt><dd>{row.new_disposition || 'Unknown'}</dd></div>
              <div><dt>Original SHA-256</dt><dd><code>{row.old_raw_digest}</code></dd></div>
              <div><dt>Observed SHA-256</dt><dd><code>{row.new_raw_digest}</code></dd></div>
            </dl>
            <ol className="recover-reads">
              {row.observations.map((item, index) => (
                <li key={`${item.observed_at}-${index}`}>{dayTime(item.observed_at)} · {item.kind} · <code>{item.raw_record_digest}</code></li>
              ))}
            </ol>
            {revealed && (
              <section className="recover-raw" aria-live="polite">
                <p><strong>{RECORD_SHEET.showRaw}</strong> · {RECORD_SHEET.hiddenIn(remaining)}</p>
                <p>Hex <code>{revealed.raw_record_hex}</code></p>
                <p>Base64 <code>{revealed.raw_record_b64}</code></p>
                <Button size="sm" onClick={() => setRevealed(null)}>{RECORD_SHEET.hideNow}</Button>
              </section>
            )}
            {row.evidence_available && (
              <form className="recover-form" onSubmit={(event) => { event.preventDefault(); void reveal() }}>
                <p><strong>{RECORD_SHEET.showRaw}</strong></p>
                <label>
                  <span>Note (why you need the raw record)</span>
                  <textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} placeholder={RECORD_SHEET.notePlaceholder} />
                </label>
                <small className="recover-counter" aria-live="polite">{`${Math.min(reason.trim().length, 10)} / 10`}</small>
                <label>
                  <span>Your password to show it</span>
                  <input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
                </label>
                <Button type="submit" busy={busy} busyLabel={RECORD_SHEET.checkingPassword} disabled={reason.trim().length < 10 || !password}>{RECORD_SHEET.showRaw}</Button>
              </form>
            )}
          </TechnicalDetails>
        </>
      )}
    </Sheet>
  )
}

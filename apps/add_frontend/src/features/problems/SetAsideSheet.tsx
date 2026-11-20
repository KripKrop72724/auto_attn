// Check-ins set aside for checking (PRB-7; was the "Attendance quarantine"
// panel). Same endpoints and request-key prefixes as before; "Show original
// record" and "Mark as checked" keep the reason and the password (D-21) and
// each has its own busy state. The original record hides after 60 s or when
// the page goes to the background.
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { api, ApiError, queryString } from '../../api'
import { idempotency } from '../../lib/idempotency'
import { dayTime } from '../../lib/time'
import { Button, Disclosure, DivisionTag, EmptyState, ErrorState, Sheet, Skeleton, StatusChip, TechnicalDetails, formatCount, useToast } from '../../ui'
import { SET_ASIDE } from '../../words/problems'
import type { AttendanceQuarantineItem, AttendanceQuarantineResponse, AttendanceQuarantineReveal } from '../../types'
import './problems.css'

const REVEAL_SECONDS = 60

const FIELD_KEYS: Array<[RegExp, keyof typeof SET_ASIDE.fields]> = [
  [/^(user_?id|pin|employee_?(id|no)|enroll(ment)?_?(id|no))$/i, 'employee'],
  [/^(timestamp|time|punch_?time|event_?time|check_?time|recorded_at)$/i, 'time'],
  [/^(record_?(id|no|number)|serial(_?no)?|log_?id|machine_?record)$/i, 'record'],
  [/^(punch(_?type)?|status|verify_?type|check_?type|type)$/i, 'type'],
]

/** The known fields of an original record as label / value pairs (personal data, shown only after the password). */
export function recordFields(payload: unknown): Array<{ label: string; value: string }> {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return []
  const fields: Array<{ label: string; value: string }> = []
  for (const [key, value] of Object.entries(payload as Record<string, unknown>)) {
    if (!['string', 'number', 'boolean'].includes(typeof value)) continue
    const match = FIELD_KEYS.find(([pattern]) => pattern.test(key))
    if (match) fields.push({ label: SET_ASIDE.fields[match[1]], value: String(value) })
  }
  return fields
}

const plainError = (error: unknown) => (error instanceof ApiError && error.status === 403 ? "That password isn't right. Type it again." : SET_ASIDE.failed)

function ReviewForm({ row, place, onDone, onBack }: {
  row: AttendanceQuarantineItem
  place: string
  onDone: () => Promise<void>
  onBack: () => void
}) {
  const id = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const toast = useToast()
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [revealed, setRevealed] = useState<AttendanceQuarantineReveal | null>(null)
  const [left, setLeft] = useState(REVEAL_SECONDS)
  const [busy, setBusy] = useState<'reveal' | 'review' | null>(null)
  const [error, setError] = useState<{ text: string; technical: string | null } | null>(null)

  useEffect(() => {
    if (!revealed) return
    setLeft(REVEAL_SECONDS)
    const started = Date.now()
    const tick = window.setInterval(() => {
      const remaining = REVEAL_SECONDS - Math.floor((Date.now() - started) / 1000)
      if (remaining <= 0) setRevealed(null)
      else setLeft(remaining)
    }, 1000)
    const hideOnBackground = () => { if (document.hidden) setRevealed(null) }
    document.addEventListener('visibilitychange', hideOnBackground)
    return () => {
      window.clearInterval(tick)
      document.removeEventListener('visibilitychange', hideOnBackground)
    }
  }, [revealed])
  useEffect(() => () => setRevealed(null), [])

  const act = async (action: 'review' | 'reveal') => {
    setBusy(action)
    setError(null)
    const secret = password
    try {
      const body = JSON.stringify({ reason: reason.trim(), password: secret, idempotency_key: idempotency(`attendance-quarantine-${action}`) })
      if (action === 'review') {
        await api(`/api/v1/attendance-quarantine/${row.id}/review`, { method: 'POST', body })
        setPassword('')
        setReason('')
        setRevealed(null)
        await onDone()
      } else {
        const value = await api<AttendanceQuarantineReveal>(`/api/v1/attendance-quarantine/${row.id}/reveal`, { method: 'POST', body })
        setPassword('')
        setReason('')
        setRevealed(value)
        toast.show({ tone: 'info', text: SET_ASIDE.revealed })
      }
    } catch (reasonError) {
      setPassword('')
      setError({ text: plainError(reasonError), technical: reasonError instanceof Error ? reasonError.message : null })
    } finally {
      setBusy(null)
    }
  }
  const ready = reason.trim().length >= 10 && Boolean(password) && !busy
  const missing = Math.max(0, 10 - reason.trim().length)
  const fields = revealed ? recordFields(revealed.payload) : []

  return (
    <section className="set-aside-review" aria-labelledby={`review-${id}`}>
      <Button variant="link" icon="chevronLeft" onClick={onBack}>{SET_ASIDE.back}</Button>
      <h3 id={`review-${id}`}>{SET_ASIDE.reviewTitle}</h3>
      <p className="set-aside-sub">{SET_ASIDE.reviewSubtitle(place, dayTime(row.observed_at))}</p>
      <p className="set-aside-chip-line"><StatusChip tone="attention" label={SET_ASIDE.chip} labelPrefix={null} size="sm" /> <span>{SET_ASIDE.others}</span></p>
      <p>{SET_ASIDE.explain}</p>
      <TechnicalDetails>
        <dl className="problem-facts">
          <div><dt>{SET_ASIDE.facts.code}</dt><dd>{row.error_code || 'VALIDATION_ERROR'}</dd></div>
          <div><dt>{SET_ASIDE.facts.path}</dt><dd>{row.error_path || '—'}</dd></div>
          <div><dt>{SET_ASIDE.facts.item}</dt><dd>{row.item_index}</dd></div>
          <div><dt>{SET_ASIDE.facts.receipt}</dt><dd>{row.receipt_id}</dd></div>
          <div><dt>{SET_ASIDE.facts.digest}</dt><dd>{row.payload_digest}</dd></div>
        </dl>
      </TechnicalDetails>
      <div className="mark-fixed-field">
        <label className="mark-fixed-label" htmlFor={`reason-${id}`}>{SET_ASIDE.reason}</label>
        <textarea id={`reason-${id}`} rows={3} value={reason} maxLength={500} placeholder={SET_ASIDE.placeholder} aria-describedby={`count-${id}`} onChange={(event) => setReason(event.target.value)} />
        <p id={`count-${id}`} className="mark-fixed-count" aria-live="polite">{missing > 0 ? `${missing} more ${missing === 1 ? 'letter' : 'letters'} needed` : ''}</p>
      </div>
      <div className="mark-fixed-field">
        <label className="mark-fixed-label" htmlFor={`password-${id}`}>{SET_ASIDE.password}</label>
        <input id={`password-${id}`} type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
      </div>
      {error && (
        <div className="message pattern-blocked mark-fixed-error" role="alert">
          <div>
            <strong>{error.text}</strong>
            {error.technical && <TechnicalDetails><p>{error.technical}</p></TechnicalDetails>}
          </div>
        </div>
      )}
      {revealed && (
        <article className="set-aside-record" aria-label={SET_ASIDE.original}>
          <div className="set-aside-record-head">
            <strong>{SET_ASIDE.original}</strong>
            <Button variant="secondary" size="sm" icon="eyeOff" onClick={() => setRevealed(null)}>{SET_ASIDE.hide}</Button>
          </div>
          <p className="set-aside-sub" aria-live="polite">{SET_ASIDE.hidesIn(left)}</p>
          {fields.length > 0 && (
            <dl className="problem-facts" data-user-text="">
              {fields.map((field) => <div key={field.label}><dt>{field.label}</dt><dd>{field.value}</dd></div>)}
            </dl>
          )}
          <Disclosure summary={SET_ASIDE.showRaw} technical defaultOpen={!fields.length}>
            <pre className="set-aside-raw">{JSON.stringify(revealed.payload, null, 2)}</pre>
          </Disclosure>
        </article>
      )}
      <div className="set-aside-actions">
        <Button variant="secondary" icon="eye" busy={busy === 'reveal'} busyLabel={SET_ASIDE.revealing} disabled={!ready || !row.evidence_available || busy === 'review'} onClick={() => void act('reveal')}>{SET_ASIDE.reveal}</Button>
        <Button variant="primary" icon="check" busy={busy === 'review'} busyLabel={SET_ASIDE.reviewing} disabled={!ready || busy === 'reveal'} onClick={() => void act('review')}>{SET_ASIDE.review}</Button>
      </div>
    </section>
  )
}

export interface SetAsideSheetProps {
  /** One device (from its problem card) or every device (the page link). */
  deviceId?: string | null
  place?: string | null
  division?: 'life' | 'health' | null
  onClose: () => void
  /** After a check: the problems list reloads (the last check closes the problem). */
  onChanged?: () => Promise<void> | void
}

export function SetAsideSheet({ deviceId, place, division, onClose, onChanged }: SetAsideSheetProps) {
  const toast = useToast()
  const [rows, setRows] = useState<AttendanceQuarantineItem[]>([])
  const [total, setTotal] = useState<number | null>(null)
  const [cursor, setCursor] = useState<number | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [moreBusy, setMoreBusy] = useState(false)
  const [picked, setPicked] = useState<AttendanceQuarantineItem | null>(null)
  const [allChecked, setAllChecked] = useState(false)
  const request = useRef<AbortController | null>(null)
  const label = place || SET_ASIDE.allPlaces

  const load = useCallback(async (after?: number | null) => {
    if (!after) {
      request.current?.abort()
    }
    const controller = new AbortController()
    if (!after) request.current = controller
    try {
      const response = await api<AttendanceQuarantineResponse>(`/api/v1/attendance-quarantine${queryString({
        device_id: deviceId || undefined,
        review_state: 'OPEN',
        limit: 50,
        cursor: after ?? undefined,
      })}`, { signal: controller.signal })
      if (!response || !Array.isArray(response.rows)) throw new Error('Unexpected answer.')
      setRows((current) => (after ? [...current, ...response.rows.filter((row) => !current.some((item) => item.id === row.id))] : response.rows))
      setTotal(response.filtered_total ?? response.totals?.open ?? response.rows.length)
      setCursor(response.next_cursor ?? null)
      setStatus('ready')
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      if (!after) setStatus('error')
    }
  }, [deviceId])

  useEffect(() => {
    void load()
    return () => request.current?.abort()
  }, [load])

  const reviewed = async () => {
    const wasLast = rows.length <= 1 && !cursor
    setPicked(null)
    await load()
    setAllChecked(wasLast)
    toast.show({ tone: 'success', text: wasLast ? SET_ASIDE.allChecked : SET_ASIDE.reviewed })
    void Promise.resolve(onChanged?.()).catch(() => undefined)
  }

  return (
    <Sheet
      title={SET_ASIDE.title}
      description={total === null ? label : SET_ASIDE.subtitle(label, formatCount(total))}
      onClose={onClose}
      className="set-aside-sheet"
      headerExtra={division ? <DivisionTag division={division} /> : undefined}
    >
      {picked
        ? <ReviewForm row={picked} place={label} onDone={reviewed} onBack={() => setPicked(null)} />
        : (
          <div className="set-aside-list">
            <p>{SET_ASIDE.intro}</p>
            {status === 'loading' && <Skeleton variant="row" count={3} label={SET_ASIDE.title} />}
            {status === 'error' && <ErrorState title={SET_ASIDE.loadError} onRetry={() => load()} compact />}
            {status === 'ready' && !rows.length && (
              <EmptyState icon="check" title={allChecked ? SET_ASIDE.allChecked : SET_ASIDE.empty} />
            )}
            {rows.length > 0 && (
              <ul className="set-aside-rows">
                {rows.map((row) => (
                  <li key={row.id} className="set-aside-row">
                    <div>
                      <strong>{SET_ASIDE.received(dayTime(row.observed_at))}</strong>
                      <span>{SET_ASIDE.rowProblem}</span>
                      {!place && <span data-device-code={row.zone_id}>{row.display_name}</span>}
                    </div>
                    <Button variant="secondary" icon="search" onClick={() => setPicked(row)}>{SET_ASIDE.check}</Button>
                  </li>
                ))}
              </ul>
            )}
            {cursor !== null && (
              <Button variant="secondary" busy={moreBusy} busyLabel="Loading…" onClick={async () => { setMoreBusy(true); await load(cursor); setMoreBusy(false) }}>{SET_ASIDE.showMore}</Button>
            )}
          </div>
        )}
    </Sheet>
  )
}

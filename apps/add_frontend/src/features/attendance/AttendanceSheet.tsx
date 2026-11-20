// One check-in's sheet (ATT-10): who, when, where, one status, its journey
// from the machine to the HR system, the one thing to do about it, phone
// message and the technical values in a closed "Technical details".
import { useCallback, useEffect, useState, type RefObject } from 'react'
import { api } from '../../api'
import { DivisionTag, Sheet, StatusChip, TechnicalDetails, Timeline, type TimelineItem } from '../../ui'
import type { AttendanceEvent } from '../../types'
import { AdminSendFacts } from '../ManualForceRelease'
import { CheckinNotifyRow } from '../CheckinNotification'
import {
  arrivalWords, checkinKind, clockWords, explainReleaseLock, GROUP_CHIP, journeyTime, longTime, neverSentSentence,
  NOTES, personName, placeDivision, placeLine, rowGroup, rowTime, statusNote,
} from './attendanceWords'
import { canSelect } from './AttendanceList'
import type { AttendanceJourney, AttendanceRow } from './types'

type Toast = { notice: (text: string) => void; error: (text: string) => void }

const SENT_SENTENCE = 'Sent to the HR system (Oracle)'

function normalizeJourney(raw: unknown): AttendanceJourney | null {
  const value = (raw && typeof raw === 'object' ? raw : null) as Record<string, unknown> | null
  if (!value || typeof value.id !== 'number' || !Array.isArray(value.steps)) return null
  return value as unknown as AttendanceJourney
}

function tries(journey: AttendanceJourney | null, row: AttendanceRow, now: number): string {
  const group = rowGroup(row)
  if (group === 'not_matched' || group === 'set_aside' || group === 'problem') return `Not sent: ${statusNote(row, null, now)}`
  const outbox = journey?.outbox
  if (!outbox || !outbox.attempt_count) return group === 'sent' ? '' : 'Waiting to be sent'
  const count = `${outbox.attempt_count} ${outbox.attempt_count === 1 ? 'try' : 'tries'}`
  return outbox.next_attempt_at && group !== 'sent' ? `${count} · next try ${rowTime(outbox.next_attempt_at, now)}` : count
}

export function journeyItems(row: AttendanceRow, journey: AttendanceJourney | null, now = Date.now()): TimelineItem[] {
  const group = rowGroup(row)
  const sentAt = journey?.outbox?.last_attempt_at || journey?.outbox?.created_at || null
  const confirmedAt = journey?.oracle_confirmed_at ?? row.oracle_confirmed_at
  const reachedSending = Boolean(journey?.outbox) || group === 'sent'
  return [
    {
      id: 'checked-in', title: 'Checked in at the machine', time: journeyTime(row.device_event_time, now), dateTime: row.device_event_time,
      description: clockWords(row.clock_quality, row.clock_drift_seconds), icon: 'check', tone: 'working',
    },
    {
      id: 'received', title: 'Received', time: journeyTime(journey?.received_at || row.received_at, now), dateTime: row.received_at,
      description: arrivalWords(row.source), icon: 'check', tone: 'working',
    },
    {
      id: 'sent', title: 'Sent to HR system', time: reachedSending ? journeyTime(sentAt || confirmedAt, now) || 'Not yet' : 'Not yet',
      description: tries(journey, row, now) || undefined, icon: reachedSending ? 'check' : 'clock', tone: reachedSending ? 'working' : 'neutral',
    },
    {
      id: 'confirmed', title: 'Confirmed by HR system', time: confirmedAt ? journeyTime(confirmedAt, now) : 'Not yet',
      description: confirmedAt && /CHECK/i.test(row.oracle_confirmation_path || '') ? 'Checked against HR records' : undefined,
      icon: confirmedAt ? 'check' : 'clock', tone: confirmedAt ? 'working' : 'neutral',
    },
  ]
}

export function AttendanceSheet({ row, missing, onClose, onSend, onChanged, toast, autoSending, returnFocusRef }: {
  row: AttendanceRow | null
  missing: boolean
  onClose: () => void
  onSend: (row: AttendanceRow) => void
  onChanged: () => void
  toast: Toast
  autoSending: boolean | null
  returnFocusRef?: RefObject<HTMLElement | null>
}) {
  const [journey, setJourney] = useState<AttendanceJourney | null>(null)
  const [journeyState, setJourneyState] = useState<'loading' | 'ready' | 'failed'>('loading')
  const id = row?.id
  const load = useCallback(async (signal?: AbortSignal) => {
    if (!id) return
    setJourneyState('loading')
    try {
      const value = normalizeJourney(await api<unknown>(`/api/v1/attendance/${id}/journey`, { signal }))
      if (signal?.aborted) return
      setJourney(value)
      setJourneyState(value ? 'ready' : 'failed')
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setJourneyState('failed')
    }
  }, [id])
  useEffect(() => {
    setJourney(null)
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  if (!row) {
    return <Sheet title="Check-in" onClose={onClose} returnFocusRef={returnFocusRef} className="attendance-sheet">
      <p>{missing ? 'This check-in could not be found. It may be outside the dates you chose.' : 'Loading…'}</p>
      <button type="button" className="button secondary" onClick={onClose}>Close</button>
    </Sheet>
  }

  const now = Date.now()
  const group = rowGroup(row)
  const chip = GROUP_CHIP[group]
  const division = placeDivision(row)
  const note = statusNote(row, autoSending, now)
  const exclusion = row.direct_ords_identity?.exclusion
  const peopleLink = `/people/${encodeURIComponent(row.source_connector?.connector_id || '')}?q=${encodeURIComponent(row.user_id)}`
  const never = neverSentSentence(row)
  const action = canSelect(row)
    ? <div className="attendance-sheet-action"><button type="button" className="button primary" onClick={() => onSend(row)}>Send to HR system</button><p>You'll confirm with your password.</p></div>
    : exclusion === 'IDENTITY_CONFLICT_UNRESOLVED' && row.source_connector
      ? <div className="attendance-sheet-action"><a className="button secondary" href={peopleLink}>Open on People page</a><p>Two people share this CNIC. Fix it on the People page first.</p></div>
      : note === NOTES.noCnic && row.source_connector
        ? <div className="attendance-sheet-action"><a className="button secondary" href={peopleLink}>Open on People page</a><p>Add this person's CNIC on the People page, then send it.</p></div>
        : note === NOTES.wrongTime
          ? <div className="attendance-sheet-action"><a className="button secondary" href="/recover?tab=held">Fix the time in Recover attendance</a><p>{never || 'The machine had the wrong time, so this check-in is not sent until the time is fixed.'}</p></div>
          : never ? <div className="attendance-sheet-action"><p>{never}</p></div> : null

  return (
    <Sheet
      title={personName(row)}
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      className="attendance-sheet"
      headerExtra={<div className="attendance-sheet-facts">
        <p>{checkinKind(row.punch)} · {longTime(row.device_event_time, now)}</p>
        <p className="attendance-sheet-place"><span>{placeLine(row)}</span>{division && <DivisionTag division={division} />}</p>
        <p className="attendance-sheet-status"><StatusChip tone={chip.tone} label={chip.label} icon={chip.icon} code={row.ords_status} /><span>{group === 'sent' ? SENT_SENTENCE : note}</span></p>
      </div>}
    >
      <section className="attendance-sheet-block" aria-labelledby={`journey-${row.id}`}>
        <h3 id={`journey-${row.id}`}>Journey</h3>
        {journeyState === 'loading' && !journey
          ? <div className="attendance-journey-skeleton" role="status" aria-label="Loading the journey">{Array.from({ length: 4 }, (_, index) => <span key={index} className="ui-skeleton ui-skeleton-line" />)}</div>
          : <Timeline items={journeyItems(row, journey, now)} label="Journey" />}
        {journeyState === 'failed' && <p className="attendance-sheet-error" role="alert">Couldn't load the journey. <button type="button" className="text-button" onClick={() => void load()}>Try again</button></p>}
      </section>

      {action && <section className="attendance-sheet-block" aria-label="What you can do"><h3>What you can do</h3>{action}</section>}
      {row.force_release && <AdminSendFacts evidence={row.force_release} />}
      <CheckinNotifyRow row={row as unknown as AttendanceEvent} toast={toast} onChanged={onChanged} />

      <TechnicalDetails>
        <dl className="attendance-tech-list">
          <div><dt>Record reference</dt><dd>{row.event_uid}</dd></div>
          <div><dt>Machine slot</dt><dd>{row.uid || '—'}</dd></div>
          <div><dt>Employee ID</dt><dd>{row.user_id}</dd></div>
          <div><dt>Machine serial</dt><dd>{row.device_serial || 'not saved'}</dd></div>
          <div><dt>Link box</dt><dd>{row.source_connector?.display_name || 'not known'}</dd></div>
          <div><dt>How it arrived (code)</dt><dd>{row.source}</dd></div>
          <div><dt>Machine clock</dt><dd>{row.clock_quality}{row.clock_drift_seconds == null ? '' : ` · ${Math.round(row.clock_drift_seconds)} s`}</dd></div>
          <div><dt>HR system status (code)</dt><dd>{row.ords_status}</dd></div>
          {row.oracle_confirmation_path && <div><dt>Confirmation path</dt><dd>{row.oracle_confirmation_path}</dd></div>}
          {row.terminal_provenance && <div><dt>Machine proof</dt><dd>{row.terminal_provenance.state} · {row.terminal_provenance.explanation}</dd></div>}
          {row.release_state && row.release_state !== 'NOT_APPLICABLE' && <div><dt>Old release state</dt><dd>{row.release_state_label || row.release_state}{row.latest_release_job_id ? ` · job ${row.latest_release_job_id}` : ''}{row.release_lock_reason ? ` · ${explainReleaseLock(row.release_lock_reason)} (${row.release_lock_reason})` : ''}</dd></div>}
          {row.effective_identity_downstream_confirmed_at && <div><dt>Old release checked</dt><dd>{rowTime(row.effective_identity_downstream_confirmed_at, now)}</dd></div>}
          {journey?.outbox && <div><dt>Sending record</dt><dd>{journey.outbox.status} · {journey.outbox.delivery_type || 'normal'} · {journey.outbox.attempt_count} tries</dd></div>}
        </dl>
      </TechnicalDetails>
    </Sheet>
  )
}

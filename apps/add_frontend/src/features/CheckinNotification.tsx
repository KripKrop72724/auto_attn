import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api'
import { Dialog, dateTime, relativeTime } from '../App'
import { Icon } from '../Icon'
import type {
  AttendanceEvent,
  CheckinNotification,
  CheckinNotificationOverview,
  CheckinNotificationSettings,
  CheckinNotificationStatus,
} from '../types'
import { explainOperatorError, OperatorFailureMessage, type OperatorFailure } from './AlertResolveDialog'
import './CheckinNotification.css'

type Toast = { notice: (message: string) => void; error: (message: string) => void }

// The employee phone notification ("attendance punched") for each punch.
export const notifyStatusLabels: Record<CheckinNotificationStatus, string> = {
  NOTIFIED: 'Notified',
  PENDING: 'Pending',
  RETRYING: 'Retrying',
  FAILED: 'Failed',
  NOT_SENT: 'Not sent',
}

const statusPatterns: Record<CheckinNotificationStatus, string> = {
  NOTIFIED: 'confirmed',
  PENDING: 'waiting',
  RETRYING: 'waiting',
  FAILED: 'blocked',
  NOT_SENT: 'notice',
}

const statusIcons: Record<CheckinNotificationStatus, Parameters<typeof Icon>[0]['name']> = {
  NOTIFIED: 'check',
  PENDING: 'clock',
  RETRYING: 'refresh',
  FAILED: 'alert',
  NOT_SENT: 'info',
}

const reasonLabels: Record<string, string> = {
  BEFORE_FEATURE: 'Punch is from before phone notifications started',
  OLD_PUNCH: 'Old punch: Oracle confirmed it too long after the punch time',
  NO_CNIC: 'No CNIC on the punch',
  CNIC_UNCERTAIN: 'The CNIC Oracle received could not be confirmed',
  DISABLED: 'Phone notifications were switched off',
  NOT_CONFIGURED: 'The notification service key is not configured',
  GROUPED: 'Grouped with an earlier punch by the same employee',
  NOT_FIRST_OF_DAY: 'Only the first punch of the day is notified',
  NOT_IN_PILOT: 'Outside the pilot zones and employees',
  INVALID_CLOCK: 'The punch time is invalid or in the future',
  AWAITING_ORACLE: 'Waiting for Oracle to confirm the punch',
  CONFIRMATION_WITHDRAWN: 'Oracle withdrew its confirmation of this punch',
  EVENT_MISSING: 'The punch is no longer available',
  NOT_RECORDED: 'No notification was recorded for this punch',
  RETRY_WINDOW_EXHAUSTED: 'Gave up after retrying for the whole retry period',
  DELIVERY_UNCERTAIN: 'The HRM service gave no clear answer, so the employee may already have the message; it is not repeated automatically',
  API_REPORTED_FAILURE: 'The HRM service reported that it could not send it',
  TRANSPORT_ALL_HOSTS_UNREACHABLE: 'Neither notification host could be reached',
  TRANSPORT_TIMEOUT: 'The HRM service did not answer in time',
  TRANSPORT_CONNECT_ERROR: 'The HRM service could not be reached',
  TRANSPORT_CONNECT_TIMEOUT: 'Connecting to the HRM service timed out',
}

const shortReasons: Record<string, string> = {
  BEFORE_FEATURE: 'before feature',
  OLD_PUNCH: 'old punch',
  NO_CNIC: 'no CNIC',
  CNIC_UNCERTAIN: 'CNIC uncertain',
  DISABLED: 'disabled',
  NOT_CONFIGURED: 'not configured',
  GROUPED: 'grouped',
  NOT_FIRST_OF_DAY: 'not first of day',
  NOT_IN_PILOT: 'outside pilot',
  INVALID_CLOCK: 'invalid time',
  AWAITING_ORACLE: 'awaiting Oracle',
  CONFIRMATION_WITHDRAWN: 'confirmation withdrawn',
}

export const explainNotifyReason = (reason: string | null) => {
  if (!reason) return ''
  if (reasonLabels[reason]) return reasonLabels[reason]
  const http = /^HTTP_(\d{3})$/.exec(reason)
  if (http) return `The HRM service answered HTTP ${http[1]}`
  return reason.replaceAll('_', ' ').toLowerCase().replace(/^./, (value) => value.toUpperCase())
}

const pktTime = (value: string) =>
  new Date(value).toLocaleTimeString('en-PK', { timeZone: 'Asia/Karachi', hour: 'numeric', minute: '2-digit' })

export const notifyPillText = (value: CheckinNotification) => {
  if (value.status === 'NOTIFIED' && value.sent_at) return `Notified ${pktTime(value.sent_at)}`
  if (value.status === 'NOT_SENT' && value.reason) {
    return `Not sent · ${shortReasons[value.reason] || explainNotifyReason(value.reason).toLowerCase()}`
  }
  return notifyStatusLabels[value.status]
}

export function CheckinNotifyPill({
  row,
  toast,
  onChanged,
}: {
  row: AttendanceEvent
  toast: Toast
  onChanged: () => void
}) {
  const [open, setOpen] = useState(false)
  const value = row.checkin_notification
  if (!value) return null
  const text = notifyPillText(value)
  return (
    <>
      <button
        type="button"
        className={`status-badge notify-pill pattern-${statusPatterns[value.status]}`}
        data-notify-status={value.status}
        aria-label={`Phone notification: ${text}. View details`}
        title={explainNotifyReason(value.reason) || notifyStatusLabels[value.status]}
        onClick={() => setOpen(true)}
      >
        <Icon name={statusIcons[value.status]} />
        <span>{text}</span>
      </button>
      {open && (
        <CheckinNotifyDetails
          row={row}
          value={value}
          toast={toast}
          onClose={() => setOpen(false)}
          onChanged={() => { setOpen(false); onChanged() }}
        />
      )}
    </>
  )
}

function CheckinNotifyDetails({
  row,
  value,
  toast,
  onClose,
  onChanged,
}: {
  row: AttendanceEvent
  value: CheckinNotification
  toast: Toast
  onClose: () => void
  onChanged: () => void
}) {
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<OperatorFailure | null>(null)
  const trimmed = reason.trim()
  const canRetry = value.can_retry && trimmed.length >= 10 && Boolean(password) && !busy
  const retry = async (event: FormEvent) => {
    event.preventDefault()
    if (!canRetry) return
    const secret = password
    setPassword('')
    setBusy(true)
    setFailure(null)
    try {
      await api(`/api/v1/attendance/${row.id}/checkin-notification/retry`, {
        method: 'POST',
        body: JSON.stringify({ reason: trimmed, password: secret }),
      })
      toast.notice('The phone notification was queued again with an audit entry.')
      onChanged()
    } catch (error) {
      setFailure(explainOperatorError(error, 'The notification could not be queued again.'))
    } finally {
      setBusy(false)
    }
  }
  const facts: Array<[string, string]> = [
    ['Status', notifyStatusLabels[value.status]],
  ]
  if (value.reason) facts.push(['Reason', explainNotifyReason(value.reason)])
  if (value.detail) facts.push(['Service detail', value.detail])
  if (value.sent_at) facts.push(['Sent', dateTime(value.sent_at)])
  if (value.next_attempt_at) facts.push(['Next attempt', `${dateTime(value.next_attempt_at)} (${relativeTime(value.next_attempt_at)})`])
  if (value.attempts) facts.push(['Attempts', String(value.attempts)])
  if (value.last_http_status) facts.push(['Last HTTP status', String(value.last_http_status)])
  if (value.host) facts.push(['Host', value.host === 'FALLBACK' ? 'Public fallback host' : 'Internal ORDS host'])
  if (value.manual_retry) facts.push(['Administrator retry', 'Yes'])
  return (
    <Dialog
      titleId={`notify-${row.id}-title`}
      title="Phone notification"
      description={`${row.display_name || `User ${row.user_id}`} · punch ${dateTime(row.device_event_time)}`}
      onClose={() => { if (!busy) onClose() }}
      className="operator-action-dialog"
    >
      <form className="dialog-body notify-details" onSubmit={(event) => void retry(event)}>
        <dl className="notify-facts">
          {facts.map(([label, text]) => <div key={label}><dt>{label}</dt><dd>{text}</dd></div>)}
        </dl>
        <p className="notify-copy">ADD asks the HRM service to send the employee an “attendance punched” message only after Oracle confirms the punch. Punch delivery to Oracle is never affected by this notification.</p>
        {value.can_retry && value.reason === 'DELIVERY_UNCERTAIN' && (
          <p className="notify-copy notify-warning" role="note">The employee may already have received this message. Retrying can send it a second time.</p>
        )}
        {value.can_retry && (
          <>
            <label>Audited reason<textarea value={reason} maxLength={300} placeholder="At least 10 characters" onChange={(event) => setReason(event.target.value)} /></label>
            <label>Administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          </>
        )}
        {failure && <OperatorFailureMessage failure={failure} />}
        <div className="dialog-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onClose}>Close</button>
          {value.can_retry && <button type="submit" className="button primary" disabled={!canRetry}><Icon name="refresh" /> {busy ? 'Queuing…' : 'Retry notification'}</button>}
        </div>
      </form>
    </Dialog>
  )
}

const stateLabels: Record<CheckinNotificationOverview['state'], string> = {
  OK: 'Working',
  DEGRADED: 'Delayed',
  PAUSED: 'Paused after failures',
  OFF: 'Switched off',
  NOT_CONFIGURED: 'Not configured',
}

const statePatterns: Record<CheckinNotificationOverview['state'], string> = {
  OK: 'confirmed', DEGRADED: 'waiting', PAUSED: 'blocked', OFF: 'notice', NOT_CONFIGURED: 'notice',
}

const modeLabels: Record<CheckinNotificationOverview['mode'], string> = {
  OFF: 'Off', PILOT: 'Pilot', ALL: 'All zones',
}

export function CheckinNotifySummary({
  summary,
  toast,
  onChanged,
}: {
  summary: CheckinNotificationOverview
  toast: Toast
  onChanged: () => void
}) {
  const [settingsOpen, setSettingsOpen] = useState(false)
  const counts = summary.last_24h
  return (
    <section className="notify-summary" aria-label="Employee phone notifications">
      <div className="notify-summary-title">
        <Icon name="pulse" />
        <span><strong>Phone notifications</strong><small>Mode: {modeLabels[summary.mode]}{summary.mode === 'PILOT' ? ` (${summary.pilot_zone_count} zones, ${summary.pilot_cnic_count} employees)` : ''}{summary.punch_policy === 'FIRST_PER_DAY' ? ' · first punch per day' : ''}</small></span>
        <span className={`status-badge pattern-${statePatterns[summary.state]}`}>{stateLabels[summary.state]}</span>
      </div>
      <ul className="notify-summary-counts" aria-label="Last 24 hours">
        <li><strong>{counts.notified.toLocaleString()}</strong> notified</li>
        <li><strong>{(counts.pending + counts.retrying).toLocaleString()}</strong> waiting</li>
        <li><strong>{counts.failed.toLocaleString()}</strong> failed</li>
        <li><strong>{counts.not_sent.toLocaleString()}</strong> not sent</li>
        <li><small>Last 24 h · last sent {summary.last_success_at ? relativeTime(summary.last_success_at) : 'never'}{summary.oldest_pending_age_seconds > 60 ? ` · oldest waiting ${Math.round(summary.oldest_pending_age_seconds / 60)} min` : ''}{summary.preferred_host === 'FALLBACK' ? ' · using public fallback host' : ''}</small></li>
      </ul>
      <button className="button secondary" type="button" onClick={() => setSettingsOpen(true)}><Icon name="edit" /> Settings</button>
      {settingsOpen && <CheckinNotifySettingsDialog toast={toast} onClose={() => setSettingsOpen(false)} onSaved={() => { setSettingsOpen(false); onChanged() }} />}
    </section>
  )
}

export function CheckinNotifySettingsDialog({
  toast,
  onClose,
  onSaved,
}: {
  toast: Toast
  onClose: () => void
  onSaved: () => void
}) {
  const [current, setCurrent] = useState<CheckinNotificationSettings | null>(null)
  const [loadError, setLoadError] = useState('')
  const [mode, setMode] = useState<CheckinNotificationSettings['mode']>('ALL')
  const [policy, setPolicy] = useState<CheckinNotificationSettings['punch_policy']>('ALL_PUNCHES')
  const [zones, setZones] = useState('')
  const [addCnic, setAddCnic] = useState('')
  const [removeIds, setRemoveIds] = useState<string[]>([])
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<OperatorFailure | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    api<CheckinNotificationSettings>('/api/v1/checkin-notifications', { signal: controller.signal })
      .then((value) => {
        setCurrent(value)
        setMode(value.mode)
        setPolicy(value.punch_policy)
        setZones(value.pilot_zone_ids.join(', '))
      })
      .catch((error) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setLoadError(error instanceof Error ? error.message : 'Notification settings could not be loaded.')
      })
    return () => controller.abort()
  }, [])

  const trimmed = reason.trim()
  const cnicValid = !addCnic || /^\d{13}$/.test(addCnic)
  const canSave = Boolean(current) && trimmed.length >= 10 && Boolean(password) && cnicValid && !busy
  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSave || !current) return
    const secret = password
    setPassword('')
    setBusy(true)
    setFailure(null)
    try {
      await api<CheckinNotificationSettings>('/api/v1/checkin-notifications/settings', {
        method: 'PUT',
        body: JSON.stringify({
          mode,
          punch_policy: policy,
          pilot_zone_ids: zones.split(',').map((item) => item.trim()).filter(Boolean),
          add_pilot_cnics: addCnic ? [addCnic] : [],
          remove_pilot_cnic_ids: removeIds,
          expected_revision: current.revision,
          reason: trimmed,
          password: secret,
        }),
      })
      toast.notice(`Phone notifications are now ${modeLabels[mode].toLowerCase()}. The change was audited.`)
      onSaved()
    } catch (error) {
      setFailure(explainOperatorError(error, 'The notification settings could not be saved.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      titleId="notify-settings-title"
      title="Phone notification settings"
      description="Choose who receives the “attendance punched” phone message. Changes apply within seconds and are audited."
      onClose={() => { if (!busy) onClose() }}
      className="operator-action-dialog"
    >
      <form className="dialog-body notify-settings" onSubmit={(event) => void save(event)}>
        {loadError && <div className="message pattern-blocked" role="alert"><Icon name="alert" /><span>{loadError}</span></div>}
        {!current && !loadError && <p role="status">Loading notification settings…</p>}
        {current && (
          <>
            {!current.configured && <div className="message pattern-waiting" role="status"><Icon name="alert" /><span>The notification service key is not configured, so nothing is sent whatever the mode.</span></div>}
            <fieldset className="notify-mode">
              <legend>Mode</legend>
              {current.modes.map((value) => (
                <label key={value}><input type="radio" name="notify-mode" value={value} checked={mode === value} onChange={() => setMode(value)} />{modeLabels[value]}<small>{value === 'OFF' ? 'Send nothing' : value === 'PILOT' ? 'Only the zones and employees listed below' : 'Every zone'}</small></label>
              ))}
            </fieldset>
            <label>Which punches<select value={policy} onChange={(event) => setPolicy(event.target.value as CheckinNotificationSettings['punch_policy'])}><option value="ALL_PUNCHES">Every punch</option><option value="FIRST_PER_DAY">First punch of the day only</option></select></label>
            <label>Pilot zone IDs<input value={zones} placeholder="Comma separated, for example ZONE-ISB-01" onChange={(event) => setZones(event.target.value)} /></label>
            <div className="notify-pilot-cnics">
              <span>Pilot employees</span>
              {current.pilot_cnics.length === 0 && <small>No employees listed.</small>}
              {current.pilot_cnics.map((item) => (
                <label key={item.id}><input type="checkbox" checked={removeIds.includes(item.id)} onChange={(event) => setRemoveIds(event.target.checked ? [...removeIds, item.id] : removeIds.filter((id) => id !== item.id))} />Remove {item.masked || 'employee'}</label>
              ))}
              <label>Add employee CNIC<input inputMode="numeric" autoComplete="off" value={addCnic} placeholder="13 digits" onChange={(event) => setAddCnic(event.target.value.replace(/\D/g, '').slice(0, 13))} /></label>
              {!cnicValid && <small className="attention-copy">Enter all 13 digits of the CNIC.</small>}
            </div>
            <p className="notify-copy">Old punches are never notified: a punch confirmed by Oracle more than {current.stale_window_hours} hours after it happened is marked “Not sent · old punch”. Repeat punches within {Math.round(current.coalesce_seconds / 60)} minutes are grouped into one message. Host: {current.endpoint_host || 'not set'}{current.fallback_host ? `, fallback ${current.fallback_host}` : ''}.</p>
            <label>Audited reason<textarea value={reason} maxLength={300} placeholder="At least 10 characters" onChange={(event) => setReason(event.target.value)} /></label>
            <label>Administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          </>
        )}
        {failure && <OperatorFailureMessage failure={failure} />}
        <div className="dialog-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="submit" className="button primary" disabled={!canSave}><Icon name="shield" /> {busy ? 'Saving…' : 'Save settings'}</button>
        </div>
      </form>
    </Dialog>
  )
}

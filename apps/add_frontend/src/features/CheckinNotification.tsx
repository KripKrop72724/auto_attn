import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { api } from '../api'
import { Dialog } from '../App'
import { Icon } from '../Icon'
import { Disclosure, Skeleton, StatusChip, TechnicalDetails } from '../ui'
import { ago, clock } from '../lib/time'
import { deviceDivision } from '../lib/division'
import { deviceName } from '../lib/deviceNames'
import { rowTime } from './attendance/attendanceWords'
import type {
  AttendanceEvent,
  Device,
  CheckinNotification,
  CheckinNotificationOverview,
  CheckinNotificationSettings,
  CheckinNotificationStatus,
} from '../types'
import { explainOperatorError, OperatorFailureMessage, type OperatorFailure } from './AlertResolveDialog'
import './CheckinNotification.css'

type Toast = { notice: (message: string) => void; error: (message: string) => void }

// The employee phone message ("attendance punched") for each check-in.
export const notifyStatusLabels: Record<CheckinNotificationStatus, string> = {
  NOTIFIED: 'Delivered',
  PENDING: 'Waiting',
  RETRYING: 'Trying again',
  FAILED: 'Failed',
  NOT_SENT: 'Not sent',
}

const reasonLabels: Record<string, string> = {
  BEFORE_FEATURE: 'This check-in is from before phone messages started',
  OLD_PUNCH: 'The HR system confirmed this check-in too long after it happened',
  NO_CNIC: 'No CNIC on this check-in',
  CNIC_UNCERTAIN: "The CNIC the HR system received couldn't be confirmed",
  DISABLED: 'Phone messages were switched off',
  NOT_CONFIGURED: "Phone messages aren't set up",
  GROUPED: 'Combined with an earlier check-in by the same person',
  NOT_FIRST_OF_DAY: 'Only the first check-in of the day gets a message',
  NOT_IN_PILOT: 'This person or area is not in the trial',
  INVALID_CLOCK: 'The check-in time is wrong or in the future',
  AWAITING_ORACLE: 'Waiting for the HR system to confirm the check-in',
  CONFIRMATION_WITHDRAWN: 'The HR system took back its confirmation of this check-in',
  EVENT_MISSING: 'The check-in is no longer available',
  NOT_RECORDED: 'No phone message was recorded for this check-in',
  RETRY_WINDOW_EXHAUSTED: 'Stopped trying after the whole retry period',
  DELIVERY_UNCERTAIN: "The phone message service gave no clear answer, so the person may already have the message; it is not repeated automatically",
  API_REPORTED_FAILURE: "The phone message service said it couldn't send it",
  TRANSPORT_ALL_HOSTS_UNREACHABLE: "The phone message service couldn't be reached",
  TRANSPORT_TIMEOUT: "The phone message service didn't answer in time",
  TRANSPORT_CONNECT_ERROR: "The phone message service couldn't be reached",
  TRANSPORT_CONNECT_TIMEOUT: 'Connecting to the phone message service took too long',
}

const shortReasons: Record<string, string> = {
  BEFORE_FEATURE: 'older check-in',
  OLD_PUNCH: 'confirmed too late',
  NO_CNIC: 'no CNIC',
  CNIC_UNCERTAIN: 'CNIC not confirmed',
  DISABLED: 'switched off',
  NOT_CONFIGURED: 'not set up',
  GROUPED: 'combined with an earlier one',
  NOT_FIRST_OF_DAY: 'not the first of the day',
  NOT_IN_PILOT: 'not in the trial',
  INVALID_CLOCK: 'wrong time',
  AWAITING_ORACLE: 'waiting for the HR system',
  CONFIRMATION_WITHDRAWN: 'the HR system took it back',
}

export const explainNotifyReason = (reason: string | null) => {
  if (!reason) return ''
  if (reasonLabels[reason]) return reasonLabels[reason]
  if (/^HTTP_(\d{3})$/.test(reason)) return 'The phone message service refused it'
  return "The phone message couldn't be sent"
}

const pktTime = (value: string) => clock(value)

/** "Delivered 8:03 am", "Not sent: older check-in", "Failed: …", "Trying again". */
export const notifyPillText = (value: CheckinNotification) => {
  if (value.status === 'NOTIFIED' && value.sent_at) return `Delivered ${pktTime(value.sent_at)}`
  if (value.status === 'NOT_SENT' && value.reason) {
    return `Not sent: ${shortReasons[value.reason] || explainNotifyReason(value.reason).toLowerCase()}`
  }
  if (value.status === 'FAILED' && value.reason) return `Failed: ${explainNotifyReason(value.reason).toLowerCase()}`
  return notifyStatusLabels[value.status]
}

/** The only phone-message mark on a list row: shown when the message failed. */
export function CheckinNotifyFailedMark({ row }: { row: AttendanceEvent }) {
  if (row.checkin_notification?.status !== 'FAILED') return null
  return <span className="notify-failed-mark" data-notify-status="FAILED"><Icon name="alert" /> Phone message failed</span>
}

/** The "Phone message" block in the check-in sheet, with "Send the message again" when allowed. */
export function CheckinNotifyRow({
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
    <section className="attendance-sheet-block notify-row" aria-labelledby={`notify-${row.id}-heading`} data-notify-status={value.status}>
      <h3 id={`notify-${row.id}-heading`}>Phone message</h3>
      <p aria-label={`Phone message: ${text}`}>{text}</p>
      {value.can_retry && <button className="button secondary" type="button" onClick={() => setOpen(true)}><Icon name="refresh" /> Send the message again</button>}
      {(value.reason || value.attempts || value.last_http_status || value.host || value.detail) && (
        <TechnicalDetails summary="Phone message service detail">
          <dl className="attendance-tech-list">
            {value.reason && <div><dt>Reason code</dt><dd>{value.reason}</dd></div>}
            {value.detail && <div><dt>Service detail</dt><dd>{value.detail}</dd></div>}
            {value.attempts > 0 && <div><dt>Attempts</dt><dd>{value.attempts}</dd></div>}
            {value.next_attempt_at && <div><dt>Next try</dt><dd>{rowTime(value.next_attempt_at)}</dd></div>}
            {value.last_http_status && <div><dt>Last answer</dt><dd>{value.last_http_status}</dd></div>}
            {value.host && <div><dt>Host</dt><dd>{value.host}</dd></div>}
            {value.manual_retry && <div><dt>Sent again by an admin</dt><dd>Yes</dd></div>}
          </dl>
        </TechnicalDetails>
      )}
      {open && (
        <CheckinNotifyDetails
          row={row}
          value={value}
          toast={toast}
          onClose={() => setOpen(false)}
          onChanged={() => { setOpen(false); onChanged() }}
        />
      )}
    </section>
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
      toast.notice('The phone message will be sent again.')
      onChanged()
    } catch (error) {
      setFailure(explainOperatorError(error, "Couldn't send the phone message again."))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog
      titleId={`notify-${row.id}-title`}
      title="Send the message again"
      description={`${row.display_name || 'Name not known'} · ${rowTime(row.device_event_time)}`}
      onClose={() => { if (!busy) onClose() }}
      className="operator-action-dialog"
    >
      <form className="dialog-body notify-details" onSubmit={(event) => void retry(event)}>
        <p className="notify-copy">{notifyPillText(value)}{value.reason && value.status !== 'NOT_SENT' && value.status !== 'FAILED' ? ` · ${explainNotifyReason(value.reason)}` : ''}.</p>
        {value.reason && <p className="notify-copy">{explainNotifyReason(value.reason)}.</p>}
        <p className="notify-copy">The person gets a phone message only after the HR system confirms the check-in. Sending to the HR system is never affected by this message.</p>
        {value.can_retry && value.reason === 'DELIVERY_UNCERTAIN' && (
          <p className="notify-copy notify-warning" role="note">The person may already have this message. Sending again can send it a second time.</p>
        )}
        {value.can_retry && (
          <>
            <label>Why? (saved in the records)<textarea value={reason} maxLength={300} placeholder="At least 10 characters" onChange={(event) => setReason(event.target.value)} /></label>
            <label>Your password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          </>
        )}
        {value.detail && <TechnicalDetails><p>{value.detail}</p></TechnicalDetails>}
        {failure && <OperatorFailureMessage failure={failure} />}
        <div className="dialog-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onClose}>Close</button>
          {value.can_retry && <button type="submit" className="button primary" disabled={!canRetry}><Icon name="refresh" /> {busy ? 'Sending…' : 'Send the message again'}</button>}
        </div>
      </form>
    </Dialog>
  )
}

export const notifyStateLabels: Record<CheckinNotificationOverview['state'], string> = {
  OK: 'Working',
  DEGRADED: 'Slow',
  PAUSED: 'Paused after failures',
  OFF: 'Switched off',
  NOT_CONFIGURED: 'Not set up',
}

const stateTones: Record<CheckinNotificationOverview['state'], 'working' | 'attention' | 'offline' | 'neutral'> = {
  OK: 'working', DEGRADED: 'attention', PAUSED: 'offline', OFF: 'neutral', NOT_CONFIGURED: 'neutral',
}

const modeLabels: Record<CheckinNotificationOverview['mode'], string> = {
  OFF: 'Off', PILOT: 'Trial', ALL: 'Everyone',
}

const sendingTo = (summary: CheckinNotificationOverview) => {
  const who = summary.mode === 'OFF' ? 'nobody'
    : summary.mode === 'PILOT' ? `trial (${summary.pilot_zone_count} ${summary.pilot_zone_count === 1 ? 'area' : 'areas'}, ${summary.pilot_cnic_count} ${summary.pilot_cnic_count === 1 ? 'person' : 'people'})`
      : 'everyone'
  if (summary.mode === 'OFF') return `Sending to: ${who}`
  return `Sending to: ${who} · ${summary.punch_policy === 'FIRST_PER_DAY' ? 'first check-in of the day only' : 'every check-in'}`
}

/**
 * Settings › "Check-in phone messages" (ATT-13). Reads its own figures from
 * `GET /api/v1/checkin-notifications`, so it no longer depends on the
 * attendance list. The counts are a 2×2 grid, never a stretched wrapping list.
 */
export function CheckinNotifySettingsCard({ toast, devices = [] }: { toast: Toast; devices?: Device[] }) {
  const [summary, setSummary] = useState<CheckinNotificationSettings | null>(null)
  const [failed, setFailed] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const value = await api<CheckinNotificationSettings>('/api/v1/checkin-notifications', { signal })
      if (signal?.aborted) return
      setSummary(value)
      setFailed(false)
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setFailed(true)
    }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  return (
    <section className="panel notify-card" aria-labelledby="notify-card-title">
      <header className="notify-card-head">
        <h2 id="notify-card-title">Check-in phone messages</h2>
        {summary && <StatusChip tone={stateTones[summary.state]} label={notifyStateLabels[summary.state]} code={summary.state} />}
      </header>
      <p className="notify-copy">People get a phone message after the HR system confirms their check-in.</p>
      {!summary && !failed && <Skeleton variant="line" count={3} label="Loading phone message settings" />}
      {failed && !summary && <p className="message pattern-blocked" role="alert">Couldn't load phone message settings. <button className="text-button" type="button" onClick={() => void load()}>Try again</button></p>}
      {summary && (
        <>
          {!summary.configured && <p className="notify-copy">Phone messages aren't set up yet, so nothing is sent.</p>}
          <p className="notify-card-mode">{sendingTo(summary)}</p>
          <p className="notify-card-caption">Last {summary.last_24h.window_hours === 24 ? '24 hours' : `${summary.last_24h.window_hours} hours`}</p>
          <dl className="notify-card-counts" aria-label="Last 24 hours">
            <div><dt>Delivered</dt><dd>{summary.last_24h.notified.toLocaleString()}</dd></div>
            <div><dt>Waiting</dt><dd>{(summary.last_24h.pending + summary.last_24h.retrying).toLocaleString()}</dd></div>
            <div><dt>Failed</dt><dd>{summary.last_24h.failed.toLocaleString()}</dd></div>
            <div><dt>Not needed</dt><dd>{summary.last_24h.not_sent.toLocaleString()}</dd></div>
          </dl>
          {summary.last_24h.not_sent > 0 && Object.keys(summary.last_24h.not_sent_reasons || {}).length > 0 && (
            <Disclosure summary="See why some were not needed">
              <ul className="notify-card-reasons">{Object.entries(summary.last_24h.not_sent_reasons).map(([code, count]) => <li key={code}><strong>{count.toLocaleString()}</strong> {shortReasons[code] || explainNotifyReason(code).toLowerCase()}</li>)}</ul>
            </Disclosure>
          )}
          <p className="notify-card-caption">Last message {summary.last_success_at ? ago(summary.last_success_at) : 'not sent yet'}{summary.oldest_pending_age_seconds > 60 ? ` · Oldest waiting ${Math.round(summary.oldest_pending_age_seconds / 60)} min` : ''}</p>
          <TechnicalDetails>
            <dl className="attendance-tech-list">
              <div><dt>State</dt><dd>{summary.state}</dd></div>
              <div><dt>Host in use</dt><dd>{summary.preferred_host}{summary.endpoint_host ? ` · ${summary.endpoint_host}` : ''}{summary.fallback_host ? ` · fallback ${summary.fallback_host}` : ''}</dd></div>
              {summary.last_failure_category && <div><dt>Last failure</dt><dd>{summary.last_failure_category}</dd></div>}
            </dl>
          </TechnicalDetails>
          <button className="button secondary" type="button" onClick={() => setSettingsOpen(true)}><Icon name="edit" /> Change settings</button>
        </>
      )}
      {settingsOpen && <CheckinNotifySettingsDialog toast={toast} devices={devices} onClose={() => setSettingsOpen(false)} onSaved={() => { setSettingsOpen(false); void load() }} />}
    </section>
  )
}

/** One line for the Check-ins tab when phone messages are paused after failures. */
export function checkinNotifyBanner(summary: Pick<CheckinNotificationOverview, 'state'> | null | undefined): string | null {
  if (!summary) return null
  if (summary.state === 'PAUSED') return 'Phone messages are paused after failures.'
  return null
}

export function CheckinNotifySettingsDialog({
  toast,
  devices = [],
  onClose,
  onSaved,
}: {
  toast: Toast
  devices?: Device[]
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
        setLoadError("Couldn't load phone message settings.")
      })
    return () => controller.abort()
  }, [])

  const zoneList = zones.split(',').map((item) => item.trim()).filter(Boolean)
  const areas = [...new Map(devices.filter((device) => !device.is_spare).map((device) => [device.zone_id, {
    id: device.zone_id,
    name: deviceName(device.zone_name || device.zone_id).location || device.zone_name || device.zone_id,
    division: deviceDivision(device),
  }] as const)).values()].sort((a, b) => a.name.localeCompare(b.name, 'en', { numeric: true }))
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
      toast.notice(`Phone messages: now ${modeLabels[mode]}.`)
      onSaved()
    } catch (error) {
      setFailure(explainOperatorError(error, "Couldn't save the settings."))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      titleId="notify-settings-title"
      title="Phone message settings"
      description="Choose who gets the phone message. Changes apply in seconds."
      onClose={() => { if (!busy) onClose() }}
      className="operator-action-dialog"
    >
      <form className="dialog-body notify-settings" onSubmit={(event) => void save(event)}>
        {loadError && <div className="message pattern-blocked" role="alert"><Icon name="alert" /><span>{loadError}</span></div>}
        {!current && !loadError && <p role="status">Loading…</p>}
        {current && (
          <>
            {!current.configured && <div className="message pattern-waiting" role="status"><Icon name="alert" /><span>Phone messages aren't set up yet, so nothing is sent whatever you choose.</span></div>}
            <fieldset className="notify-mode">
              <legend>Send to</legend>
              {current.modes.map((value) => (
                <label key={value}><input type="radio" name="notify-mode" value={value} checked={mode === value} onChange={() => setMode(value)} />{modeLabels[value]}<small>{value === 'OFF' ? 'Send nothing' : value === 'PILOT' ? 'Only the areas and people chosen below' : 'Every area'}</small></label>
              ))}
            </fieldset>
            <label>Which check-ins<select value={policy} onChange={(event) => setPolicy(event.target.value as CheckinNotificationSettings['punch_policy'])}><option value="ALL_PUNCHES">Every check-in</option><option value="FIRST_PER_DAY">First check-in of the day only</option></select></label>
            {areas.length > 0
              ? <fieldset className="notify-areas"><legend>Trial areas</legend>{(['life', 'health'] as const).map((division) => {
                const items = areas.filter((area) => area.division === division)
                return items.length ? <div key={division} className="notify-area-group"><strong>{division === 'life' ? 'Life' : 'Health'}</strong>{items.map((area) => <label key={area.id}><input type="checkbox" checked={zoneList.includes(area.id)} onChange={(event) => setZones((event.target.checked ? [...zoneList, area.id] : zoneList.filter((id) => id !== area.id)).join(', '))} />{area.name}</label>)}</div> : null
              })}{zoneList.filter((id) => !areas.some((area) => area.id === id)).length > 0 && <small>Also kept: {zoneList.filter((id) => !areas.some((area) => area.id === id)).length} more areas.</small>}</fieldset>
              : <label>Trial areas<input value={zones} placeholder="Area codes, separated by commas" onChange={(event) => setZones(event.target.value)} /></label>}
            <div className="notify-pilot-cnics">
              <span>Trial people</span>
              {current.pilot_cnics.length === 0 && <small>Nobody listed.</small>}
              {current.pilot_cnics.map((item) => (
                <label key={item.id}><input type="checkbox" checked={removeIds.includes(item.id)} onChange={(event) => setRemoveIds(event.target.checked ? [...removeIds, item.id] : removeIds.filter((id) => id !== item.id))} />Remove {item.masked || 'person'}</label>
              ))}
              <label>Add a person's CNIC<input inputMode="numeric" autoComplete="off" value={addCnic} placeholder="13 digits" onChange={(event) => setAddCnic(event.target.value.replace(/\D/g, '').slice(0, 13))} /></label>
              {!cnicValid && <small className="attention-copy">Enter all 13 digits of the CNIC.</small>}
            </div>
            <p className="notify-copy">Old check-ins never get a message: one the HR system confirms more than {current.stale_window_hours} hours after it happened shows "Not sent: confirmed too late". Check-ins within {Math.round(current.coalesce_seconds / 60)} minutes of each other get one message.</p>
            <label>Why? (saved in the records)<textarea value={reason} maxLength={300} placeholder="At least 10 characters" onChange={(event) => setReason(event.target.value)} /></label>
            <label>Your password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
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

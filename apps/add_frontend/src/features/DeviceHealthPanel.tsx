// Health evidence of one device (04-device §2 Technical details › Health
// evidence). The plain headline and the Mark as seen / Mark as fixed buttons
// moved out: the "What's happening" card says what is wrong, the Problems tab
// owns the buttons. What stays here is for IT: how current each reason is,
// what the link box software can report, the machine connection and the saved
// error. It is shown only inside a Technical details box.
import { dayTime, ago } from '../lib/time'
import { normalizedStatus } from '../status'
import type { Coverage, Device, DeviceHealth, HealthReason } from '../types'
import { healthDetailWords, plainTier } from '../words/health'
import { plainMachineLink } from '../words/machine'
import { problemWords, TEST_UPDATES_WAIT } from '../words/problems'
import { plainStatus } from '../words/states'
import './DeviceHealth.css'

export interface HealthAlertTarget {
  id: number
  code: string
  message: string
}

const firmwareVersion = (value?: string | null) => value ? value.replace(/^zone-lite-/i, '') : null
const shortRestart = (value?: string | null) => value ? `${value.slice(0, 8)}…` : null

/** How current a reason is, in the words of 02-words A3 (raw facts kept for IT). */
export function currencyLabel(reason: Pick<HealthReason, 'currency' | 'firmware_version' | 'boot_id' | 'latch' | 'code'>) {
  const latch = normalizedStatus(reason.latch?.kind).toUpperCase()
  if (latch === 'LED_LATCH_NO_IO_ERRORS') return 'Kept until the next restart: no memory errors since this restart'
  if (latch === 'LED_LATCH_STORAGE_VERIFIED') return 'Kept until the next restart: memory checked since this restart'
  switch (normalizedStatus(reason.currency).toUpperCase()) {
    case 'CURRENT': return 'Reported now'
    case 'HELD': return 'Waiting for the link box to confirm it'
    case 'PREVIOUS_BOOT': {
      const facts = [
        firmwareVersion(reason.firmware_version) && `software ${firmwareVersion(reason.firmware_version)}`,
        shortRestart(reason.boot_id) && `restart ${shortRestart(reason.boot_id)}`,
      ].filter(Boolean)
      return `From before the last restart${facts.length ? ` (${facts.join(', ')})` : ''}`
    }
    case 'LATCHED': return 'Kept until it is checked again'
    default: return plainStatus('currency', reason.currency).label
  }
}

function coverageText(item: Coverage, firmware?: string | null) {
  if (item.detail) return item.detail
  const status = normalizedStatus(item.status).toUpperCase()
  if (status === 'NOT_REPORTED_BY_FIRMWARE') return `Not reported by link box software ${firmwareVersion(firmware) || 'in use'}.`
  if (status === 'MISSING') return 'The last status report carried no details.'
  if (status === 'REPORTED' || status === 'ACTIVE') return 'Reported.'
  return 'Not known.'
}

function ReasonRow({ reason }: { reason: HealthReason }) {
  const key = reason.alert_id == null ? `derived-${reason.code}` : `alert-${reason.alert_id}`
  const titleId = `device-health-${key}-title`
  const words = problemWords(reason)
  const tier = reason.tier ? plainTier(reason.tier).label : 'Does not change the status'
  const extra = healthDetailWords(reason)
  return (
    <li className="device-health-reason" aria-labelledby={titleId}>
      <h5 id={titleId}>{words.title}</h5>
      <dl className="device-health-facts">
        <div><dt>Code</dt><dd>{reason.code}</dd></div>
        <div><dt>Effect</dt><dd>{tier}</dd></div>
        <div><dt>How current</dt><dd>{currencyLabel(reason)}</dd></div>
        <div><dt>Since</dt><dd>{reason.since ? <time dateTime={reason.since}>{dayTime(reason.since)} · {ago(reason.since)}</time> : 'Not recorded'}</dd></div>
        <div><dt>Last reported</dt><dd>{reason.last_seen_at ? <time dateTime={reason.last_seen_at}>{ago(reason.last_seen_at)}</time> : 'Happening now'}</dd></div>
        {reason.alert_id != null && <div><dt>Problem number</dt><dd>#{reason.alert_id}</dd></div>}
        <div className="wide"><dt>Message</dt><dd>{reason.message}</dd></div>
        {reason.evidence_summary && <div className="wide"><dt>Details</dt><dd>{reason.evidence_summary}</dd></div>}
        {reason.clear_condition && <div className="wide"><dt>Clears when</dt><dd>{reason.clear_condition}</dd></div>}
        {reason.operator && !reason.operator.resolvable && reason.operator.refusal && <div className="wide"><dt>Why it can't be marked as fixed</dt><dd>{reason.operator.refusal}</dd></div>}
      </dl>
      {extra.map((line) => <p key={line}>{line}</p>)}
      {reason.gating && reason.tier && <p>{TEST_UPDATES_WAIT}</p>}
    </li>
  )
}

export function HealthEvidence({ device, health }: { device: Device; health?: DeviceHealth | null }) {
  if (!health) {
    if (!device.last_error_code) return <p>No health details reported.</p>
    return (
      <div className="device-health-evidence">
        <p>Saved error: {problemWords({ code: device.last_error_code }).title} ({device.last_error_code}).</p>
        {device.last_error_message && <p>{device.last_error_message}</p>}
      </div>
    )
  }
  const shadow = normalizedStatus(health.mode).toUpperCase() === 'SHADOW'
  const reasons = health.reasons || []
  const others = health.other_active_alerts || []
  const coverage = health.coverage || []
  const link = health.terminal_link ?? device.terminal_link
  const deviceError = health.device_error
  const machine = link ? plainMachineLink(link) : null
  return (
    <div className="device-health-evidence">
      {shadow && <section aria-labelledby="device-health-preview-title">
        <h5 id="device-health-preview-title">Preview of the new status rules: {plainStatus('device', health.tier).label}</h5>
        <p>Preview only. The status at the top still decides the totals and problems.</p>
      </section>}
      {normalizedStatus(health.tier).toUpperCase() === 'OFFLINE' && <p>We show a device as offline when we haven't heard from it for 45 seconds. It comes back by itself when it reconnects.</p>}
      {health.evaluated_at && <p className="device-muted">Checked {ago(health.evaluated_at)}</p>}
      {reasons.length > 0 && <ul className="device-health-reasons" aria-label="Health reasons">
        {reasons.map((reason) => <ReasonRow key={reason.alert_id ?? reason.code} reason={reason} />)}
      </ul>}
      {deviceError?.code && <section aria-labelledby="device-health-error-title">
        <h5 id="device-health-error-title">Saved error: {problemWords({ code: deviceError.code }).title}</h5>
        <p>{deviceError.code}{deviceError.message ? ` · ${deviceError.message}` : ''}</p>
        <p>{deviceError.backed
          ? `Kept by open problem ${deviceError.backing_alert_ids.map((id) => `#${id}`).join(', ')}. It clears with ${deviceError.backing_alert_ids.length === 1 ? 'that problem' : 'those problems'}.`
          : 'No open problem keeps this saved error. It is out of date.'}</p>
      </section>}
      {coverage.length > 0 && <section aria-labelledby="device-health-coverage-title">
        <h5 id="device-health-coverage-title">What this link box software can report</h5>
        <ul className="device-health-coverage">
          {coverage.map((item) => <li key={item.key}><strong>{item.label || item.key}:</strong> <span>{coverageText(item, device.firmware_version)}</span></li>)}
        </ul>
      </section>}
      {link && machine && <section aria-labelledby="device-health-terminal-title">
        <h5 id="device-health-terminal-title">Machine connection</h5>
        <p aria-label={`Machine connection: ${machine.label.toLowerCase()}`}>{machine.label}. {machine.sentence}</p>
        <dl className="device-health-facts">
          <div><dt>Reported by the link box</dt><dd>{link.raw_state || 'Not reported'}</dd></div>
          <div><dt>Since</dt><dd>{link.since ? `${dayTime(link.since)} · ${ago(link.since)}` : 'Not recorded'}</dd></div>
          {link.message && <div className="wide"><dt>Message</dt><dd>{link.message}</dd></div>}
        </dl>
      </section>}
      {others.length > 0 && <section aria-labelledby="device-health-other-title">
        <h5 id="device-health-other-title">Also open (does not change the status)</h5>
        <ul className="device-health-reasons">
          {others.map((reason) => <ReasonRow key={reason.alert_id ?? reason.code} reason={reason} />)}
        </ul>
      </section>}
    </div>
  )
}

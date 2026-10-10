import type { ReactNode } from 'react'
import {
  StatusBadge, TerminalLinkBadge, dateTime, healthReasonLine, healthTierPattern, primaryHealthReason,
  relativeTime, statusIcon,
} from '../App'
import { Icon } from '../Icon'
import { humanizeStatus, normalizedStatus, statusPattern, type StatusPattern } from '../status'
import type { Coverage, Device, DeviceHealth, HealthReason } from '../types'
import './DeviceHealth.css'

export interface HealthAlertTarget {
  id: number
  code: string
  message: string
}

const headlinePhrases: Record<string, string> = {
  ONLINE: 'online',
  ONLINE_WITH_WARNINGS: 'online with warnings',
  DEGRADED: 'degraded',
  OFFLINE: 'offline',
  QUARANTINED_DUPLICATE_SERIAL: 'quarantined',
  ONBOARDING: 'onboarding',
}

const phrase = (state: string) => headlinePhrases[state] || humanizeStatus(state).toLowerCase()
const sentenceCase = (value: string) => value.charAt(0).toUpperCase() + value.slice(1)

// Quarantine and onboarding override the reason-derived tier.
export function healthHeadline(health: Pick<DeviceHealth, 'tier' | 'derived_lifecycle'>) {
  const derived = normalizedStatus(health.derived_lifecycle).toUpperCase()
  return ['QUARANTINED_DUPLICATE_SERIAL', 'ONBOARDING'].includes(derived) ? derived : normalizedStatus(health.tier).toUpperCase()
}

const shortBoot = (value?: string | null) => value ? `${value.slice(0, 8)}…` : null
const firmwareVersion = (value?: string | null) => value ? value.replace(/^zone-lite-/i, '') : null

export function currencyLabel(reason: Pick<HealthReason, 'currency' | 'firmware_version' | 'boot_id' | 'latch' | 'code'>) {
  const latch = normalizedStatus(reason.latch?.kind).toUpperCase()
  if (latch === 'LED_LATCH_NO_IO_ERRORS') return 'Latched until reboot — no I/O errors this boot'
  if (latch === 'LED_LATCH_STORAGE_VERIFIED') return 'Latched until reboot — storage verified this boot'
  switch (normalizedStatus(reason.currency).toUpperCase()) {
    case 'CURRENT': return 'Current'
    case 'HELD': return 'Held: waiting for verified evidence'
    case 'PREVIOUS_BOOT': {
      const facts = [
        firmwareVersion(reason.firmware_version) && `firmware ${firmwareVersion(reason.firmware_version)}`,
        shortBoot(reason.boot_id) && `boot ${shortBoot(reason.boot_id)}`,
      ].filter(Boolean)
      return `From an earlier boot${facts.length ? ` (${facts.join(', ')})` : ''}`
    }
    case 'LATCHED': return 'Latched: heartbeats do not re-check it'
    default: return humanizeStatus(reason.currency)
  }
}

const currencyIcon = (reason: Pick<HealthReason, 'currency' | 'latch'>) => {
  if (reason.latch?.kind) return 'shield' as const
  const currency = normalizedStatus(reason.currency).toUpperCase()
  return currency === 'CURRENT' ? 'pulse' as const : currency === 'HELD' ? 'pause' as const : currency === 'PREVIOUS_BOOT' ? 'clock' as const : 'shield' as const
}

const tierLabels: Record<string, string> = { DEGRADED: 'Degraded', WARNING: 'Warning' }

function Pill({ pattern, icon, children }: { pattern: StatusPattern; icon?: Parameters<typeof Icon>[0]['name']; children: ReactNode }) {
  return (
    <span className={`status-badge device-health-pill pattern-${pattern}`} data-pattern={pattern}>
      <Icon name={icon || statusIcon[pattern]} />
      <span>{children}</span>
    </span>
  )
}

function coverageText(item: Coverage, firmware?: string | null) {
  if (item.detail) return item.detail
  const status = normalizedStatus(item.status).toUpperCase()
  if (status === 'NOT_REPORTED_BY_FIRMWARE') return `Not reported by firmware ${firmwareVersion(firmware) || 'in use'}.`
  if (status === 'MISSING') return 'The latest heartbeat carried no diagnostics.'
  return humanizeStatus(status)
}

function ReasonRow({
  reason,
  busy,
  onAcknowledge,
  onResolve,
}: {
  reason: HealthReason
  busy: boolean
  onAcknowledge?: (alertId: number) => void
  onResolve?: (target: HealthAlertTarget) => void
}) {
  const key = reason.alert_id == null ? `derived-${reason.code}` : `alert-${reason.alert_id}`
  const titleId = `device-health-${key}-title`
  const refusalId = `device-health-${key}-refusal`
  const tier = normalizedStatus(reason.tier).toUpperCase()
  const resolvable = reason.operator?.resolvable === true
  const acknowledged = Boolean(reason.acknowledged_at || reason.acknowledged_by)
  const canAcknowledge = reason.alert_id != null && normalizedStatus(reason.alert_state).toUpperCase() === 'OPEN' && !acknowledged
  const alertId = reason.alert_id
  return (
    <li className={`device-health-reason pattern-${reason.tier ? healthTierPattern(reason.tier) : 'notice'}`} aria-labelledby={titleId}>
      <div className="device-health-reason-head">
        <div>
          <h4 id={titleId}>{humanizeStatus(reason.code)}</h4>
          <p>{reason.message}</p>
        </div>
        <div className="device-health-pills">
          <Pill pattern={reason.tier ? healthTierPattern(reason.tier) : 'notice'}>{tierLabels[tier] || (reason.tier ? humanizeStatus(reason.tier) : 'No health effect')}</Pill>
          <Pill pattern="notice" icon={currencyIcon(reason)}>{currencyLabel(reason)}</Pill>
        </div>
      </div>
      <dl className="device-health-facts">
        <div><dt>Since</dt><dd>{reason.since ? <time dateTime={reason.since}>{dateTime(reason.since)} · {relativeTime(reason.since)}</time> : 'Not recorded'}</dd></div>
        <div><dt>Last seen</dt><dd>{reason.last_seen_at ? <time dateTime={reason.last_seen_at}>{relativeTime(reason.last_seen_at)}</time> : 'Live condition'}</dd></div>
        {reason.evidence_summary && <div className="wide"><dt>Evidence</dt><dd>{reason.evidence_summary}</dd></div>}
      </dl>
      {reason.clear_condition && <p className="device-health-clears">{reason.clear_condition}</p>}
      {acknowledged && <p>Acknowledged{reason.acknowledged_by ? ` by ${reason.acknowledged_by}` : ''}{reason.acknowledged_at ? ` · ${dateTime(reason.acknowledged_at)}` : ''}. It stays active until it clears.</p>}
      {reason.gating && reason.tier && <p>Holds HIL and factory work while active.</p>}
      {alertId != null && (onAcknowledge || onResolve) && <div className="device-health-actions">
        {canAcknowledge && onAcknowledge && <button type="button" className="button secondary small" disabled={busy} onClick={() => onAcknowledge(alertId)}><Icon name="check" /> Acknowledge</button>}
        {onResolve && <button
          type="button"
          className="button secondary small"
          disabled={busy || !resolvable}
          aria-describedby={resolvable ? undefined : refusalId}
          onClick={() => onResolve({ id: alertId, code: reason.code, message: reason.message })}
        ><Icon name="shield" /> Resolve with reason</button>}
        {!resolvable && <p className="device-health-refusal" id={refusalId}>{reason.operator?.refusal || 'ADD does not allow resolving this alert by hand.'}</p>}
      </div>}
    </li>
  )
}

export function DeviceHealthPanel({
  device,
  health,
  busy = false,
  onAcknowledge,
  onResolve,
  onClearError,
}: {
  device: Device
  // The full object from GET /api/v1/devices/{id}; a list summary renders its primary reason.
  health?: DeviceHealth | null
  busy?: boolean
  onAcknowledge?: (alertId: number) => void
  onResolve?: (target: HealthAlertTarget) => void
  onClearError?: () => void
}) {
  if (!health) {
    // Older backends report only the stored error.
    if (!device.last_error_code) return null
    return (
      <article className="detail-card wide device-health-panel pattern-blocked" aria-labelledby="device-health-title">
        <div>
          <p className="eyebrow">DEVICE HEALTH</p>
          <h3 id="device-health-title">Why this device is {phrase(normalizedStatus(device.state).toUpperCase())}</h3>
        </div>
        <p>Stored device error: {humanizeStatus(device.last_error_code)}. {device.last_error_message || device.zkt?.writes_disabled_reason || 'Review live logs and connectivity history.'}</p>
      </article>
    )
  }
  const headline = healthHeadline(health)
  const shadow = normalizedStatus(health.mode).toUpperCase() === 'SHADOW'
  const title = shadow ? `New health model (preview): ${sentenceCase(phrase(headline))}` : `Why this device is ${phrase(headline)}`
  const reasons = health.reasons
  const primary = primaryHealthReason(health)
  const others = health.other_active_alerts || []
  const coverage = health.coverage || []
  const link = health.terminal_link ?? device.terminal_link
  const deviceError = health.device_error
  return (
    <article className={`detail-card wide device-health-panel pattern-${statusPattern(headline)}`} aria-labelledby="device-health-title">
      <div className="detail-title">
        <div>
          <p className="eyebrow">{shadow ? 'DEVICE HEALTH · PREVIEW' : 'DEVICE HEALTH'}</p>
          <h3 id="device-health-title">{title}</h3>
        </div>
        <div className="device-health-headline">
          {shadow && <span className="device-health-legacy">Current status <StatusBadge state={device.state} /></span>}
          <StatusBadge state={headline} />
        </div>
      </div>
      {shadow && <p>Preview only. The current status above still drives fleet counts, holds and alerts until the derived model is enforced.</p>}
      {headline === 'OFFLINE' && <p>ADD marks a connector offline when it accepts no heartbeat for 45 seconds; the next accepted heartbeat brings it back.</p>}
      {health.evaluated_at && <p>Evaluated {relativeTime(health.evaluated_at)}.</p>}
      {reasons ? (
        reasons.length
          ? <ul className="device-health-reasons" aria-label="Health reasons">
            {reasons.map((reason) => <ReasonRow key={reason.alert_id ?? reason.code} reason={reason} busy={busy} onAcknowledge={onAcknowledge} onResolve={onResolve} />)}
          </ul>
          : <p>No active health reasons.</p>
      ) : primary ? <p>{healthReasonLine(primary)}{health.degraded_count + health.warning_count > 1 ? ` · ${health.degraded_count + health.warning_count} reasons in total` : ''}</p> : <p>No active health reasons.</p>}
      {deviceError?.code && <section className="device-health-section" aria-labelledby="device-health-error-title">
        <h4 id="device-health-error-title">Stored device error: {humanizeStatus(deviceError.code)}</h4>
        {deviceError.message && <p>{deviceError.message}</p>}
        <p>{deviceError.backed
          ? `Backed by active alert${deviceError.backing_alert_ids.length === 1 ? '' : 's'} ${deviceError.backing_alert_ids.map((id) => `#${id}`).join(', ')}; it clears with ${deviceError.backing_alert_ids.length === 1 ? 'that alert' : 'those alerts'}.`
          : `No active alert backs this error. ADD derives ${deviceError.derived_code ? humanizeStatus(deviceError.derived_code) : 'no device error'} from the active alerts.`}</p>
        {!deviceError.backed && onClearError && <div className="device-health-actions"><button type="button" className="button secondary small" disabled={busy} onClick={onClearError}><Icon name="refresh" /> Re-evaluate device error</button></div>}
      </section>}
      {coverage.length > 0 && <section className="device-health-section" aria-labelledby="device-health-coverage-title">
        <h4 id="device-health-coverage-title">What ADD can verify on this firmware</h4>
        <ul className="device-health-coverage">
          {coverage.map((item) => <li key={item.key}><strong>{item.label || humanizeStatus(item.key)}:</strong> <span>{coverageText(item, device.firmware_version)}</span></li>)}
        </ul>
      </section>}
      {link && <section className="device-health-section" aria-labelledby="device-health-terminal-title">
        <div className="detail-title">
          <h4 id="device-health-terminal-title">Terminal link</h4>
          <TerminalLinkBadge link={link} />
        </div>
        {link.message && <p>{link.message}</p>}
        <dl>
          <div><dt>Reported state</dt><dd>{link.raw_state ? humanizeStatus(link.raw_state) : 'Not reported'}</dd></div>
          <div><dt>Since</dt><dd>{link.since ? `${dateTime(link.since)} · ${relativeTime(link.since)}` : 'Not recorded'}</dd></div>
        </dl>
      </section>}
      {others.length > 0 && <section className="device-health-section" aria-labelledby="device-health-other-title">
        <h4 id="device-health-other-title">Other open items (no health effect)</h4>
        <ul className="device-health-reasons">
          {others.map((reason) => <ReasonRow key={reason.alert_id ?? reason.code} reason={reason} busy={busy} onAcknowledge={onAcknowledge} onResolve={onResolve} />)}
        </ul>
      </section>}
    </article>
  )
}

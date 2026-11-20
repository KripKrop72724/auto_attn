import type { ReactNode } from 'react'
import { StatusBadge, dateTime, formatAlertDiagnostics, relativeTime, statusIcon } from '../App'
import { Icon } from '../Icon'
import { routePath } from '../routing'
import { alertStatePattern, normalizedStatus } from '../status'
import type { Alert, HealthOperatorPolicy } from '../types'
import './DeviceHealth.css'

const text = (value: unknown) => typeof value === 'string' && value.trim() ? value.trim() : null

// OPEN rows and legacy ACKNOWLEDGED rows are both active.
export const alertIsActive = (alert: Pick<Alert, 'state'>) =>
  ['OPEN', 'ACKNOWLEDGED'].includes(normalizedStatus(alert.state).toUpperCase())

export const alertIsAcknowledged = (alert: Pick<Alert, 'state' | 'acknowledged_at'>) =>
  normalizedStatus(alert.state).toUpperCase() === 'ACKNOWLEDGED' || Boolean(alert.acknowledged_at)

export const alertNeedsAction = (alert: Pick<Alert, 'state' | 'acknowledged_at'>) =>
  normalizedStatus(alert.state).toUpperCase() === 'OPEN' && !alert.acknowledged_at

// 'Acknowledged by X · time · note'
export function acknowledgementLine(alert: Alert) {
  const by = text(alert.acknowledged_by) || text(alert.details?.acknowledged_by)
  const note = text(alert.details?.acknowledgement_note)
  return [`Acknowledged${by ? ` by ${by}` : ''}`, alert.acknowledged_at ? dateTime(alert.acknowledged_at) : null, note]
    .filter(Boolean)
    .join(' · ')
}

// The resolution kind and actor are part of the diagnostics line; this line
// carries when it happened and the recorded reason.
export function resolutionLine(alert: Alert) {
  const details = alert.details?.resolution
  const resolution = alert.resolution || (details && typeof details === 'object' ? details as Record<string, unknown> : null)
  const at = text(resolution?.at) || alert.resolved_at
  const reason = text(resolution?.reason)
  return [at ? `Resolved ${dateTime(at)}` : 'Resolved', reason ? `Reason: ${reason}` : null].filter(Boolean).join(' · ')
}

export function AlertCard({
  alert,
  device,
  headingLevel = 2,
  operator,
  busy = false,
  onAcknowledge,
  onResolve,
  children,
}: {
  alert: Alert
  device?: { connector_id: string; display_name: string; zone_id: string } | null
  headingLevel?: 2 | 3 | 4
  operator?: HealthOperatorPolicy | null
  busy?: boolean
  onAcknowledge?: (alert: Alert) => void
  onResolve?: (alert: Alert) => void
  children?: ReactNode
}) {
  const pattern = alertStatePattern(alert)
  const active = alertIsActive(alert)
  const acknowledged = active && alertIsAcknowledged(alert)
  const resolved = normalizedStatus(alert.state).toUpperCase() === 'RESOLVED'
  const refused = operator?.resolvable === false
  const diagnostics = formatAlertDiagnostics(alert.details || {})
  const Heading = `h${headingLevel}` as 'h2' | 'h3' | 'h4'
  const titleId = `alert-${alert.id}-title`
  return (
    <article className={`alert-card pattern-${pattern}`} aria-labelledby={titleId}>
      <span className="alert-icon"><Icon name="alert" /></span>
      <div>
        <div className="alert-meta">
          <StatusBadge state={alert.severity} />
          {acknowledged && <span className="status-badge pattern-notice" data-pattern="notice"><Icon name={statusIcon.notice} /><span>Acknowledged</span></span>}
          {device && <a href={routePath('fleet', device.connector_id)}>{device.display_name} · {device.zone_id}</a>}
        </div>
        <Heading id={titleId}>{alert.message}</Heading>
        <p>{alert.code} · First {dateTime(alert.first_seen_at)} · Last {relativeTime(alert.last_seen_at)}</p>
        {diagnostics && <p className="alert-diagnostics" aria-label="Safe alert diagnostics">{diagnostics}</p>}
        {acknowledged && <p className="alert-annotation">{acknowledgementLine(alert)}</p>}
        {resolved && <p className="alert-annotation">{resolutionLine(alert)}</p>}
        {active && refused && operator?.refusal && <p className="alert-refusal" id={`alert-${alert.id}-refusal`}>{operator.refusal}</p>}
      </div>
      <div className="alert-actions">
        {children}
        {alertNeedsAction(alert) && onAcknowledge && <button type="button" className="button secondary" disabled={busy} onClick={() => onAcknowledge(alert)}><Icon name="check" /> Acknowledge</button>}
        {active && onResolve && <button
          type="button"
          className="button secondary"
          disabled={busy || refused}
          aria-describedby={refused && operator?.refusal ? `alert-${alert.id}-refusal` : undefined}
          onClick={() => onResolve(alert)}
        ><Icon name="shield" /> Resolve…</button>}
        {!active && <StatusBadge state={alert.state} />}
      </div>
    </article>
  )
}

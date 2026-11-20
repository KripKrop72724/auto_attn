// Problems on one device (04-device §4): what is happening now (health
// reasons with no problem record), then the problems not fixed yet (New
// first, then Seen), then "Fixed recently" closed. Mark as seen is one click
// on that card only; Mark as fixed opens the existing dialog unchanged
// (reason and password stay until the owner decides, D-2).
import { useState } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import { Icon } from '../../Icon'
import { api } from '../../api'
import { ago, dayTime } from '../../lib/time'
import type { Alert, HealthOperatorPolicy, HealthReason } from '../../types'
import { Badge, Button, Disclosure, EmptyState, ErrorState, Skeleton, TechnicalDetails, useToast } from '../../ui'
import type { Toast } from '../../ui/Toast'
import { PROBLEMS_WORDS, type DeviceDetail } from '../../words/device'
import { problemWords, refusalWords, resolutionWords } from '../../words/problems'
import { alertNeedsAction } from '../AlertCard'
import { AlertResolveDialog } from '../AlertResolveDialog'
import type { HealthAlertTarget } from '../DeviceHealthPanel'
import { friendlyName } from './deviceNaming'
import { useResolvedAlerts } from './useDeviceData'

const text = (value: unknown) => (typeof value === 'string' && value.trim() ? value.trim() : null)
const newestFirst = (left: Alert, right: Alert) => Date.parse(right.last_seen_at || '') - Date.parse(left.last_seen_at || '')

function ProblemCard({ alert, operator, reason, busy, onSeen, onFix }: {
  alert: Alert
  operator?: HealthOperatorPolicy
  reason?: HealthReason
  busy: boolean
  onSeen?: () => void
  onFix?: () => void
}) {
  const words = problemWords({ ...(reason as object | undefined), ...(alert.details || {}), ...(alert as object), code: alert.code } as Parameters<typeof problemWords>[0])
  const resolved = Boolean(alert.resolved_at)
  const isNew = !resolved && alertNeedsAction(alert)
  const seenBy = text(alert.acknowledged_by) || text(alert.details?.acknowledged_by)
  const note = text(alert.details?.acknowledgement_note)
  const resolution = alert.resolution || (alert.details?.resolution && typeof alert.details.resolution === 'object' ? alert.details.resolution as Alert['resolution'] : null)
  const resolvable = operator ? operator.resolvable : true
  const stillHappening = !resolved && Date.now() - Date.parse(alert.last_seen_at) < 5 * 60_000
  return (
    <article className={`dv-problem-card${isNew ? ' is-new' : ''}${resolved ? ' is-fixed' : ''}`} aria-label={words.title} data-status-code={alert.code}>
      <div className="dv-problem-head">
        <Icon name={resolved ? 'check' : 'alert'} size="sm" />
        <h4>{words.title}</h4>
        {!resolved && <Badge tone={isNew ? 'danger' : 'neutral'}>{isNew ? PROBLEMS_WORDS.newPill : PROBLEMS_WORDS.seenPill}</Badge>}
      </div>
      <p className="dv-muted">
        {PROBLEMS_WORDS.started(dayTime(alert.first_seen_at))}
        {!resolved && ` · ${stillHappening ? PROBLEMS_WORDS.stillHappening : PROBLEMS_WORDS.lastReported(ago(alert.last_seen_at))}`}
      </p>
      {!resolved && <p>{words.detail}</p>}
      {!resolved && words.next && <p><strong>What to do: </strong>{words.next}</p>}
      {!resolved && alert.acknowledged_at && <p>{PROBLEMS_WORDS.seenBy(seenBy || 'an admin', dayTime(alert.acknowledged_at))}{note && <span data-user-text>{PROBLEMS_WORDS.seenNote(note)}</span>}</p>}
      {resolved && <p>{resolutionWords(resolution?.kind, resolution?.actor)}. {PROBLEMS_WORDS.fixedAt(dayTime(resolution?.at || alert.resolved_at))}{text(resolution?.reason) && <span data-user-text>{PROBLEMS_WORDS.fixedNote(text(resolution?.reason) as string)}</span>}</p>}
      {!resolved && (onSeen || onFix) && (
        <div className="dv-tool-actions">
          {isNew && onSeen && <Button size="sm" busy={busy} busyLabel={PROBLEMS_WORDS.marking} onClick={onSeen}>{PROBLEMS_WORDS.markSeen}</Button>}
          {onFix && resolvable && <Button size="sm" onClick={onFix}>{PROBLEMS_WORDS.markFixed}</Button>}
        </div>
      )}
      {!resolved && !resolvable && <p className="dv-muted">{refusalWords(operator?.refusal_code, reason?.code ?? alert.code, reason?.currency)}</p>}
      <TechnicalDetails>
        <dl className="dv-tech-rows">
          <div><dt>Code</dt><dd>{alert.code}</dd></div>
          <div><dt>Problem number</dt><dd>#{alert.id}</dd></div>
          <div><dt>Message</dt><dd>{alert.message}</dd></div>
          <div><dt>Level</dt><dd>{alert.severity}</dd></div>
          {operator?.refusal && <div><dt>Refusal</dt><dd>{operator.refusal}</dd></div>}
        </dl>
      </TechnicalDetails>
    </article>
  )
}

function HappeningNow({ reason }: { reason: HealthReason }) {
  const words = problemWords(reason)
  return (
    <article className="dv-problem-card is-derived" aria-label={words.title} data-status-code={reason.code}>
      <div className="dv-problem-head"><Icon name="alert" size="sm" /><h4>{words.title}</h4></div>
      {reason.since && <p className="dv-muted">Since {dayTime(reason.since)} · {ago(reason.since).replace(/ ago$/, '')}.</p>}
      <p>{words.detail}</p>
      <p>{PROBLEMS_WORDS.clearsBySelf(words.closesWhen)}</p>
    </article>
  )
}

export function ProblemsTab({ device, activeAlerts, toast, onChanged }: {
  device: DeviceDetail
  activeAlerts: UseQueryResult<Alert[]>
  toast: Toast
  onChanged: () => Promise<void>
}) {
  const kitToast = useToast()
  const resolved = useResolvedAlerts(device.connector_id, true)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [fixTarget, setFixTarget] = useState<HealthAlertTarget | null>(null)
  const name = friendlyName(device)

  if (device.is_spare) return <EmptyState icon="info" title={PROBLEMS_WORDS.spare} />

  const reasons = [...(device.health?.reasons || []), ...(device.health?.other_active_alerts || [])]
  const operatorByAlert = new Map<number, HealthOperatorPolicy>()
  const reasonByAlert = new Map<number, HealthReason>()
  reasons.forEach((reason) => {
    if (reason.alert_id != null) {
      if (reason.operator) operatorByAlert.set(reason.alert_id, reason.operator)
      reasonByAlert.set(reason.alert_id, reason)
    }
  })
  const derived = (device.health?.reasons || []).filter((reason) => reason.alert_id == null)
  const open = [...(activeAlerts.data || [])].sort(newestFirst)
  const newOnes = open.filter(alertNeedsAction)
  const seen = open.filter((row) => !alertNeedsAction(row))
  const fixed = resolved.data || []

  const markSeen = async (alert: Alert) => {
    setBusyId(alert.id)
    try {
      await api<Alert>(`/api/v1/alerts/${alert.id}/acknowledge`, { method: 'POST', body: '{}' })
      kitToast.show({ tone: 'success', text: PROBLEMS_WORDS.markedSeen })
      await onChanged()
    } catch {
      kitToast.show({ tone: 'error', text: PROBLEMS_WORDS.markSeenFailed })
    } finally {
      setBusyId(null)
    }
  }

  if (activeAlerts.isPending) return <Skeleton variant="block" label="Loading problems" />
  if (activeAlerts.isError) return <ErrorState title={PROBLEMS_WORDS.loadError} onRetry={() => activeAlerts.refetch()} />

  const card = (alert: Alert) => (
    <ProblemCard
      key={alert.id}
      alert={alert}
      operator={operatorByAlert.get(alert.id)}
      reason={reasonByAlert.get(alert.id)}
      busy={busyId === alert.id}
      onSeen={() => void markSeen(alert)}
      onFix={() => setFixTarget({ id: alert.id, code: alert.code, message: alert.message })}
    />
  )
  const fixedList = fixed.length > 0 && (
    <Disclosure summary={PROBLEMS_WORDS.fixedRecently(fixed.length)} className="dv-fixed-recently">
      <div className="dv-problem-list">{fixed.map((alert) => <ProblemCard key={alert.id} alert={alert} busy={false} />)}</div>
    </Disclosure>
  )
  return (
    <div className="dv-problems">
      {derived.length > 0 && (
        <section aria-labelledby="dv-problems-now">
          <h3 id="dv-problems-now">{PROBLEMS_WORDS.happeningNow}</h3>
          <div className="dv-problem-list">{derived.map((reason) => <HappeningNow key={reason.code} reason={reason} />)}</div>
        </section>
      )}
      {open.length > 0 ? (
        <section aria-labelledby="dv-problems-open">
          <h3 id="dv-problems-open">{PROBLEMS_WORDS.notFixed(open.length)}</h3>
          <div className="dv-problem-list">{[...newOnes, ...seen].map(card)}</div>
        </section>
      ) : derived.length === 0 && <EmptyState icon="check" title={PROBLEMS_WORDS.none} />}
      {resolved.isError
        ? <ErrorState compact title={PROBLEMS_WORDS.loadError} onRetry={() => resolved.refetch()} />
        : fixedList}
      {fixTarget && <AlertResolveDialog
        alert={fixTarget}
        deviceName={name}
        toast={toast}
        onClose={() => setFixTarget(null)}
        onResolved={onChanged}
      />}
    </div>
  )
}

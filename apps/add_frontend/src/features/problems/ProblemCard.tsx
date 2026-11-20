// One problem (PRB-4). Shared with the device page's Problems tab (04-device
// DEV-7). Plain title, sentence and "What to do" come from the row's plain_*
// fields or fe/words; the code, the system message and the details sit only in
// Technical details. Mark as fixed shows only when the backend allows it.
import { useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Icon } from '../../Icon'
import { ActionMenu, Button, DivisionTag, StatusChip, TechnicalDetails, useIsPhone, type ActionMenuItem, type Tone } from '../../ui'
import { ago, clock, day, dayTime, duration } from '../../lib/time'
import { deviceDivision } from '../../lib/division'
import { deviceName } from '../../lib/deviceNames'
import { formatAlertDiagnostics } from '../../words/health'
import { plainStatus } from '../../words/states'
import { PROBLEM_CARD, problemWords, refusalWords, resolutionWords } from '../../words/problems'
import { devicePath, problemLink, type ProblemLink } from './problemLinks'
import type { RowBusy, RowPhase } from './useProblems'
import type { ProblemDevice, ProblemRow } from './types'
import './problems.css'

// The 10 workflow-owned codes and the storage reminder cannot be closed by hand
// (be/device_health.py); used only when an older backend sends no `operator`.
const NEVER_BY_HAND = new Set([
  'ADMIN_REVOKE_OVERDUE', 'ATTENDANCE_EVENT_QUARANTINED', 'ATTENDANCE_REPAIR_NEEDS_ATTENTION',
  'ATTENDANCE_REPAIR_STALLED', 'ORDS_DELIVERY_FAILED', 'DUPLICATE_USER_CNIC',
  'ORACLE_RECEIPT_CONNECTOR_MISMATCH', 'HISTORY_BACKFILL_BLOCKED', 'USER_CNIC_FORMAT_SUSPECT',
  'CHECKIN_NOTIFY_DEGRADED', 'ESP_PRESERVATION_UNVERIFIED',
])

const upper = (value: string | null | undefined) => (value || '').toUpperCase()

export const problemIsFixed = (row: Pick<ProblemRow, 'state'>) => upper(row.state) === 'RESOLVED'
export const problemIsSeen = (row: Pick<ProblemRow, 'state' | 'acknowledged_at'>) =>
  !problemIsFixed(row) && (upper(row.state) === 'ACKNOWLEDGED' || Boolean(row.acknowledged_at))
export const problemIsNew = (row: Pick<ProblemRow, 'state' | 'acknowledged_at'>) =>
  upper(row.state) === 'OPEN' && !row.acknowledged_at

/** Can this problem be marked as fixed by hand? From `operator`, else from the code alone. */
export function canMarkFixed(row: ProblemRow) {
  if (problemIsFixed(row)) return false
  if (row.operator) return row.operator.resolvable === true
  return !NEVER_BY_HAND.has(row.code)
}

/** Severity chip tone: Urgent red, Important amber, Low grey (one meaning each). */
export function severityTone(severity: string | null | undefined): Tone {
  const value = upper(severity)
  if (value === 'CRITICAL') return 'offline'
  if (value === 'HIGH') return 'attention'
  return 'neutral'
}

export const severityLabel = (severity: string | null | undefined) => {
  const word = plainStatus('severity', upper(severity) || 'INFO')
  return word.unknown ? 'Low' : word.label
}

/** Friendly name and division for a row's device (backend fields first, the name rule as fallback). */
export function deviceLabel(device: ProblemDevice | null | undefined) {
  if (!device) return { name: '', division: 'health' as const, code: '' }
  const code = device.zone_id || device.display_name || ''
  const named = deviceName(device.display_name || device.zone_id)
  const fallback = named.unit ? `${named.location} · ${named.unit}` : named.location
  return {
    name: device.friendly_name?.trim() || fallback || device.display_name,
    division: deviceDivision(device),
    code,
  }
}

/** Full Pakistan date and time for Technical details ("Thu 6 Nov, 9:24 am"). */
const fullTime = (value: string | null | undefined) => (value ? `${day(value)}, ${clock(value)}` : '')

const text = (value: unknown) => (typeof value === 'string' && value.trim() ? value.trim() : null)

/** "Seen by Sara · 10:02 am · “Power back at 2 pm”", or "Seen by you · …". */
export function seenLine(row: ProblemRow, username?: string, now: number = Date.now()) {
  const by = text(row.acknowledged_by) || text(row.details?.acknowledged_by)
  const when = row.acknowledged_at ? dayTime(row.acknowledged_at, now) : ''
  const note = text(row.details?.acknowledgement_note)
  const head = by && username && by === username
    ? PROBLEM_CARD.seenByYou(when || 'now')
    : by ? PROBLEM_CARD.seenBy(by, when || 'earlier') : `${PROBLEM_CARD.seen}${when ? ` · ${when}` : ''}`
  return { head, note }
}

/** "Fixed by itself at 11:05 am · lasted 3 h 20 min", said once. */
export function fixedLine(row: ProblemRow) {
  const resolution = row.resolution || (row.details?.resolution && typeof row.details.resolution === 'object' ? row.details.resolution as Record<string, unknown> : null)
  const kind = upper(text(resolution?.kind))
  const at = text(resolution?.at) || row.resolved_at
  const time = at ? clock(at) : ''
  const actor = text(resolution?.actor)
  const reason = text(resolution?.reason)
  if (kind === 'OPERATOR') return { line: PROBLEM_CARD.fixedByPerson(actor || 'an admin', time), reason }
  if (kind === 'CLEANUP') return { line: PROBLEM_CARD.cleanup(actor || 'an admin', time), reason }
  const lasted = at ? duration(row.first_seen_at, at) : ''
  return { line: PROBLEM_CARD.fixedAt(resolutionWords(kind || null), time, lasted || duration(row.first_seen_at)), reason: null }
}

/** Plain text IT can paste into a message: place, problem, since, code and ids. */
export function copyText(row: ProblemRow) {
  const words = problemWords(row)
  const device = deviceLabel(row.device)
  return [
    `${device.name}${device.code ? ` (${device.code})` : ''}`,
    words.title,
    `Started ${dayTime(row.first_seen_at)}`,
    `Code ${row.code} · Problem ${row.id}`,
    row.device?.hardware_id ? `Link box ${row.device.hardware_id}` : null,
  ].filter(Boolean).join('\n')
}

export interface ProblemCardProps {
  row: ProblemRow
  headingLevel?: 2 | 3 | 4
  /** Show the device (flat lists and the Fixed tab); groups show it in their header. */
  showDevice?: boolean
  /** Show [Open device] on the card (not inside a device group or on the device page). */
  showOpenDevice?: boolean
  phase?: RowPhase
  busy?: RowBusy
  /** 'failed' or 'gone' after a Mark as seen. */
  rowError?: string
  username?: string
  onMarkSeen?: (row: ProblemRow) => void
  onMarkSeenWithNote?: (row: ProblemRow) => void
  onMarkFixed?: (row: ProblemRow) => void
  onSetAside?: (row: ProblemRow) => void
  /** Query string kept on links into other pages (for example `?division=life`). */
  linkSearch?: string
  now?: number
  children?: ReactNode
}

function LinkButton({ link, row, onSetAside, linkSearch }: { link: ProblemLink; row: ProblemRow; onSetAside?: (row: ProblemRow) => void; linkSearch?: string }) {
  const location = useLocation()
  if (link.kind === 'set-aside') {
    if (!onSetAside) return null
    return <button type="button" className="text-button problem-link" onClick={() => onSetAside(row)}>{link.label}<Icon name="chevron" size="sm" /></button>
  }
  const division = new URLSearchParams(linkSearch ?? location.search).get('division')
  const to = division && !/[?&]division=/.test(link.to) ? `${link.to}${link.to.includes('?') ? '&' : '?'}division=${division}` : link.to
  return <Link className="text-button problem-link" to={to} state={{ from: location }}>{link.label}<Icon name="chevron" size="sm" /></Link>
}

export function ProblemCard({
  row,
  headingLevel = 3,
  showDevice = false,
  showOpenDevice = false,
  phase,
  busy,
  rowError,
  username,
  onMarkSeen,
  onMarkSeenWithNote,
  onMarkFixed,
  onSetAside,
  linkSearch,
  now = Date.now(),
  children,
}: ProblemCardProps) {
  const phone = useIsPhone()
  const location = useLocation()
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)
  const words = problemWords(row)
  const fixed = problemIsFixed(row)
  const seen = problemIsSeen(row)
  const fresh = problemIsNew(row)
  const fixable = canMarkFixed(row) && Boolean(onMarkFixed)
  const link = problemLink(row)
  const tone = severityTone(row.severity)
  const device = deviceLabel(row.device)
  const Heading = `h${headingLevel}` as 'h2' | 'h3' | 'h4'
  const titleId = `problem-${row.id}-title`
  const since = text(row.details?.since) || row.first_seen_at
  const refused = !fixed && row.operator && row.operator.resolvable === false
  const closing = fixed
    ? null
    : refused
      ? `${refusalWords(row.operator?.refusal_code, row.code)} ${words.closesWhen}`
      : words.closesWhen
  const seenText = seen ? seenLine(row, username, now) : null
  const fixedText = fixed ? fixedLine(row) : null
  const compact = phone && !fixed && !expanded
  const marking = busy === 'seen'
  const canSee = fresh && Boolean(onMarkSeen)
  const deviceTo = row.device?.connector_id ? devicePath(row.device.connector_id) : null
  const diagnostics = formatAlertDiagnostics(row.details || {})

  const copyForIt = async () => {
    try {
      await navigator.clipboard?.writeText(copyText(row))
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }

  const menu: ActionMenuItem[] = [
    ...(phone && fixable && canSee ? [{ id: 'fixed', label: PROBLEM_CARD.markFixed, icon: 'check' as const, onSelect: () => onMarkFixed?.(row) }] : []),
    ...(canSee && onMarkSeenWithNote ? [{ id: 'note', label: PROBLEM_CARD.markSeenWithNote, icon: 'edit' as const, onSelect: () => onMarkSeenWithNote(row) }] : []),
    ...(typeof navigator !== 'undefined' && navigator.clipboard ? [{ id: 'copy', label: copied ? PROBLEM_CARD.copied : PROBLEM_CARD.copyForIt, icon: 'copy' as const, onSelect: () => void copyForIt() }] : []),
  ]

  const technical = (
    <TechnicalDetails>
      <dl className="problem-facts">
        <div><dt>{PROBLEM_CARD.facts.code}</dt><dd>{row.code}</dd></div>
        <div><dt>{PROBLEM_CARD.facts.message}</dt><dd>{row.message}</dd></div>
        {diagnostics && <div><dt>{PROBLEM_CARD.facts.details}</dt><dd>{diagnostics}</dd></div>}
        <div><dt>{PROBLEM_CARD.facts.number}</dt><dd>#{row.id}</dd></div>
        <div><dt>{PROBLEM_CARD.facts.started}</dt><dd>{fullTime(row.first_seen_at)}</dd></div>
        <div><dt>{PROBLEM_CARD.facts.lastReport}</dt><dd>{fullTime(row.last_seen_at)}</dd></div>
        {row.device?.display_name && <div><dt>{PROBLEM_CARD.facts.deviceCode}</dt><dd>{row.device.display_name}</dd></div>}
        {row.device?.hardware_id && <div><dt>{PROBLEM_CARD.facts.linkBox}</dt><dd>{row.device.hardware_id}</dd></div>}
        {row.operator?.refusal && <div><dt>{PROBLEM_CARD.facts.refusal}</dt><dd>{row.operator.refusal}</dd></div>}
        {fixedText?.reason && fixedText.line.startsWith('Closed') && <div><dt>{PROBLEM_CARD.facts.details}</dt><dd>{fixedText.reason}</dd></div>}
      </dl>
    </TechnicalDetails>
  )

  return (
    <div className="problem-card-shell" data-phase={phase || undefined}>
      <article
        className={`problem-card${fixed ? ' is-fixed' : ''}${compact ? ' is-compact' : ''}`}
        aria-labelledby={titleId}
        data-tone={tone}
        data-problem-id={row.id}
        data-phase={phase || undefined}
        tabIndex={-1}
      >
        <div className="problem-card-marks">
          {fixed
            ? <StatusChip tone="working" label={PROBLEM_CARD.fixed} labelPrefix={null} size="sm" />
            : <StatusChip tone={tone} label={severityLabel(row.severity)} labelPrefix={PROBLEM_CARD.severityPrefix} icon="alert" size="sm" code={row.severity} />}
          {fresh && <span className="problem-new"><span className="problem-new-dot" aria-hidden="true" />{PROBLEM_CARD.new}</span>}
          {seen && <StatusChip tone="info" label={PROBLEM_CARD.seen} labelPrefix={null} icon="eye" size="sm" />}
          {phase === 'fixed-hold' && <span className="problem-done" role="status"><Icon name="check" size="sm" />{PROBLEM_CARD.markedFixed}</span>}
          {phase === 'fixed-now' && <span className="problem-done" role="status"><Icon name="check" size="sm" />{PROBLEM_CARD.fixedJustNow}</span>}
        </div>
        <Heading id={titleId} className="problem-title">{words.title}</Heading>
        {showDevice && row.device && (
          <p className="problem-device">
            <DivisionTag division={device.division} />
            <span data-device-code={device.code}>{device.name}</span>
          </p>
        )}
        {fixedText && <p className="problem-fixed-line">{fixedText.line}{fixedText.reason && !fixedText.line.startsWith('Closed') && <> · <span data-user-text="">“{fixedText.reason}”</span></>}</p>}
        {!fixed && !compact && <p className="problem-detail">{words.detail}</p>}
        {!fixed && (
          <p className={`problem-next${compact ? ' is-clamped' : ''}`}>
            <strong>{PROBLEM_CARD.whatToDo}</strong> {words.next}
            {link && !compact && <> <LinkButton link={link} row={row} onSetAside={onSetAside} linkSearch={linkSearch} /></>}
          </p>
        )}
        {!fixed && (compact
          ? <p className="problem-times">{PROBLEM_CARD.phoneSince(dayTime(since, now), duration(since, now))}</p>
          : <p className="problem-times">{PROBLEM_CARD.started(dayTime(since, now), duration(since, now), ago(row.last_seen_at, now))}</p>)}
        {seenText && !compact && (
          <p className="problem-seen">{seenText.head}{seenText.note && <> · <span data-user-text="">“{seenText.note}”</span></>}</p>
        )}
        {closing && !compact && <p className="problem-closing">{closing}</p>}
        {rowError && (
          <p className="problem-row-error" role="alert">{rowError === 'gone' ? PROBLEM_CARD.alreadyFixed : PROBLEM_CARD.seenError}</p>
        )}
        {(canSee || fixable || menu.length > 0 || (showOpenDevice && deviceTo) || (compact && link) || children) && !fixed && phase !== 'fixed-hold' && phase !== 'fixed-now' && (
          <div className="problem-actions">
            {canSee && (
              <Button variant={phone ? 'primary' : 'secondary'} busy={marking} busyLabel={PROBLEM_CARD.marking} icon="eye" onClick={() => onMarkSeen?.(row)}>
                {PROBLEM_CARD.markSeen}
              </Button>
            )}
            {fixable && !phone && (
              <Button variant="secondary" icon="check" onClick={() => onMarkFixed?.(row)}>{PROBLEM_CARD.markFixed}</Button>
            )}
            {fixable && phone && !canSee && (
              <Button variant="secondary" icon="check" onClick={() => onMarkFixed?.(row)}>{PROBLEM_CARD.markFixed}</Button>
            )}
            {compact && link && <LinkButton link={link} row={row} onSetAside={onSetAside} linkSearch={linkSearch} />}
            {showOpenDevice && deviceTo && (
              <Link className="button secondary problem-open-device" to={deviceTo} state={{ from: location }}>{PROBLEM_CARD.openDeviceLabel}<Icon name="chevron" size="sm" /></Link>
            )}
            {children}
            {menu.length > 0 && <ActionMenu items={menu} label={PROBLEM_CARD.more(words.title)} />}
          </div>
        )}
        {phone && !fixed && (
          <button type="button" className="text-button problem-expand" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
            <Icon name={expanded ? 'chevronDown' : 'chevron'} size="sm" />{expanded ? PROBLEM_CARD.hideDetails : PROBLEM_CARD.showDetails}
          </button>
        )}
        {(!compact || fixed) && technical}
      </article>
    </div>
  )
}

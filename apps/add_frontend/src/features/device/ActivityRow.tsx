// One Activity row (04-device §3 Row anatomy) and the event sheet with its
// technical line. A folded run of routine events expands in place.
import { useEffect, useState } from 'react'
import { api } from '../../api'
import { Icon, type IconName } from '../../Icon'
import { clock, dayTime } from '../../lib/time'
import type { DeviceLog } from '../../types'
import { Button, Sheet, Spinner, TechnicalDetails, useShowTechnical, useToast } from '../../ui'
import {
  ACTIVITY_KIND_LABELS, ACTIVITY_TAB, ACTIVITY_WORDS_TAB, PROBLEM_EVENT_LABELS, SERIOUS_PROBLEM, describeLog, fallbackKind, topicLabel,
  type ActivityDisplayKind,
} from '../../words/activity'
import { itemTime, type ActivityItem } from './activityData'

const KIND_ICON: Record<ActivityDisplayKind, IconName> = {
  problem: 'x', review: 'list', retried: 'refresh', good: 'check', routine: 'info', action: 'users',
}

export const displayKind = (item: ActivityItem): ActivityDisplayKind => {
  if (item.type === 'action') return 'action'
  return (item.kind in KIND_ICON ? item.kind : 'routine') as ActivityDisplayKind
}

export function kindLabel(item: ActivityItem): string {
  if (item.type === 'problem_event') return PROBLEM_EVENT_LABELS[item.event] ?? ACTIVITY_KIND_LABELS.problem
  if (item.type === 'event' && item.serious) return SERIOUS_PROBLEM
  return ACTIVITY_KIND_LABELS[displayKind(item)]
}

const rawOf = (item: ActivityItem): DeviceLog | null => (item.type === 'event' ? item.raw : null)

export function technicalLine(item: ActivityItem): string {
  const raw = rawOf(item)
  if (raw) return [raw.code || 'no code', raw.level, raw.subsystem].filter(Boolean).join(' · ')
  if (item.type === 'action') return `${item.command_type} · ${item.status}`
  if (item.type === 'problem_event') return `#${item.alert_id} · ${item.event}`
  return [item.code, 'count' in item ? `× ${item.count}` : ''].filter(Boolean).join(' · ')
}

/** Rows of a folded run, read on demand from /logs by code and time. */
function RunRows({ connectorId, item }: { connectorId: string; item: Extract<ActivityItem, { type: 'group' }> }) {
  const [rows, setRows] = useState<DeviceLog[] | null>(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    const params = new URLSearchParams()
    if (item.code) params.set('code', item.code)
    params.set('since', item.first_at)
    params.set('until', new Date(Date.parse(item.last_at) + 1).toISOString())
    params.set('cursor', String(item.last_id + 1))
    params.set('limit', String(Math.min(100, Math.max(1, item.count))))
    api<{ rows: DeviceLog[] }>(`/api/v1/devices/${encodeURIComponent(connectorId)}/logs?${params.toString()}`, { signal: controller.signal })
      .then((answer) => setRows((answer.rows || []).filter((row) => row.id >= item.first_id && row.id <= item.last_id)))
      .catch(() => { if (!controller.signal.aborted) setError(true) })
    return () => controller.abort()
  }, [connectorId, item.code, item.count, item.first_at, item.first_id, item.last_at, item.last_id])
  if (error) return <p className="dv-muted">{ACTIVITY_TAB.loadError}</p>
  if (!rows) return <p className="dv-muted"><Spinner size="sm" /> {ACTIVITY_WORDS_TAB.loading}</p>
  return (
    <ul className="dv-run-rows">
      {rows.map((row) => <li key={row.id}><time dateTime={row.received_at}>{clock(row.received_at)}</time> <span>{describeLog(row).sentence}</span></li>)}
    </ul>
  )
}

export function ActivityRow({ item, connectorId, onOpen, isNew = false }: {
  item: ActivityItem
  connectorId: string
  onOpen: (item: ActivityItem) => void
  isNew?: boolean
}) {
  const technical = useShowTechnical()
  const [expanded, setExpanded] = useState(false)
  const kind = displayKind(item)
  const fixed = Boolean(item.fixed_at)
  const time = item.type === 'group' || item.type === 'escalation'
    ? (clock(item.first_at) === clock(item.last_at) ? clock(item.last_at) : `${clock(item.first_at)} – ${clock(item.last_at)}`)
    : clock(itemTime(item))
  return (
    <li className={`dv-activity-row kind-${kind}${fixed ? ' is-fixed' : ''}${isNew ? ' is-new' : ''}`} data-kind={kind}>
      <button type="button" className="dv-activity-open" onClick={() => onOpen(item)}>
        <time dateTime={itemTime(item)}>{time}</time>
        <span className="dv-activity-icon" aria-hidden="true"><Icon name={fixed ? 'check' : KIND_ICON[kind]} size="sm" /></span>
        <span className="dv-activity-text">
          <span className="sr-only">{kindLabel(item)}: </span>
          <span className="dv-activity-title">{item.title}</span>
          {item.action && <span className="dv-activity-next"> {item.action}</span>}
          {fixed && item.fixed_at && <span className="dv-activity-fixed">{ACTIVITY_WORDS_TAB.fixedAt(clock(item.fixed_at))}</span>}
          <span className="dv-activity-topic">{topicLabel(item.topic)}</span>
          {technical && <code className="dv-activity-code" data-technical>{technicalLine(item)}</code>}
        </span>
      </button>
      {item.type === 'group' && (
        <button type="button" className="text-button dv-activity-show" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
          {expanded ? ACTIVITY_WORDS_TAB.hide : ACTIVITY_WORDS_TAB.show}
          <Icon name={expanded ? 'chevronDown' : 'chevron'} size="sm" />
        </button>
      )}
      {item.type === 'group' && expanded && <RunRows connectorId={connectorId} item={item} />}
    </li>
  )
}

function copyText(item: ActivityItem): string {
  const raw = rawOf(item)
  const lines = [
    `${kindLabel(item)}: ${item.title}`,
    item.action ? `${ACTIVITY_WORDS_TAB.whatToDo} ${item.action}` : '',
    `${ACTIVITY_WORDS_TAB.when} ${dayTime(itemTime(item))}`,
    raw?.code ? `${ACTIVITY_WORDS_TAB.sheetCode}: ${raw.code}` : item.code ? `${ACTIVITY_WORDS_TAB.sheetCode}: ${item.code}` : '',
    raw ? `${ACTIVITY_WORDS_TAB.sheetLevel}: ${raw.level} · ${ACTIVITY_WORDS_TAB.sheetArea}: ${raw.subsystem}` : '',
    raw?.device_time ? `${ACTIVITY_WORDS_TAB.sheetLinkBoxTime}: ${raw.device_time}` : '',
    raw ? `${ACTIVITY_WORDS_TAB.sheetReceived}: ${raw.received_at}` : '',
    raw ? `${ACTIVITY_WORDS_TAB.sheetRestart}: ${raw.boot_id} · ${ACTIVITY_WORDS_TAB.sheetNumber}: #${raw.id}` : '',
    raw?.message ? `${ACTIVITY_WORDS_TAB.sheetMessage}: ${raw.message}` : '',
  ]
  return lines.filter(Boolean).join('\n')
}

export function ActivityEventSheet({ item, onClose }: { item: ActivityItem; onClose: () => void }) {
  const toast = useToast()
  const raw = rawOf(item)
  const copy = async () => {
    try {
      await navigator.clipboard?.writeText(copyText(item))
      toast.show({ tone: 'success', text: ACTIVITY_WORDS_TAB.copied })
    } catch {
      // Copy is a convenience; the text stays on screen.
    }
  }
  const described = raw ? describeLog(raw) : null
  return (
    <Sheet title={kindLabel(item)} onClose={onClose} className="dv-event-sheet">
      <p className="dv-event-title">{item.title}</p>
      <p><strong>{ACTIVITY_WORDS_TAB.when}</strong> {item.type === 'group' || item.type === 'escalation'
        ? `${dayTime(item.first_at)} – ${clock(item.last_at)}`
        : `${dayTime(itemTime(item))} (${ACTIVITY_WORDS_TAB.received})`}</p>
      {(item.action || described?.next) && <p><strong>{ACTIVITY_WORDS_TAB.whatToDo}</strong> {item.action || described?.next}</p>}
      {item.fixed_at && <p>{ACTIVITY_WORDS_TAB.fixedAt(dayTime(item.fixed_at))}</p>}
      <TechnicalDetails defaultOpen copyText={copyText(item)}>
        <dl className="dv-tech-rows">
          {(raw?.code || item.code) && <div><dt>{ACTIVITY_WORDS_TAB.sheetCode}</dt><dd>{raw?.code || item.code}</dd></div>}
          {raw && <div><dt>{ACTIVITY_WORDS_TAB.sheetLevel}</dt><dd>{raw.level}</dd></div>}
          {raw && <div><dt>{ACTIVITY_WORDS_TAB.sheetArea}</dt><dd>{raw.subsystem}</dd></div>}
          {raw?.device_time && <div><dt>{ACTIVITY_WORDS_TAB.sheetLinkBoxTime}</dt><dd>{dayTime(raw.device_time)} ({raw.device_time})</dd></div>}
          {raw && <div><dt>{ACTIVITY_WORDS_TAB.sheetReceived}</dt><dd>{dayTime(raw.received_at)} ({raw.received_at})</dd></div>}
          {raw && <div><dt>{ACTIVITY_WORDS_TAB.sheetRestart}</dt><dd>{raw.boot_id}</dd></div>}
          {raw && <div><dt>{ACTIVITY_WORDS_TAB.sheetNumber}</dt><dd>#{raw.id}</dd></div>}
          {'count' in item && <div><dt>{ACTIVITY_WORDS_TAB.sheetCount}</dt><dd>{item.count}</dd></div>}
          {(item.type === 'group' || item.type === 'escalation') && <div><dt>{ACTIVITY_WORDS_TAB.sheetFirst}</dt><dd>{item.first_at}</dd></div>}
          {(item.type === 'group' || item.type === 'escalation') && <div><dt>{ACTIVITY_WORDS_TAB.sheetLast}</dt><dd>{item.last_at}</dd></div>}
          {(item.type === 'action' || item.type === 'problem_event') && item.actor && <div><dt>{ACTIVITY_WORDS_TAB.sheetActor}</dt><dd>{item.actor}</dd></div>}
          {item.type === 'action' && <div><dt>{ACTIVITY_WORDS_TAB.sheetStatus}</dt><dd>{item.command_type} · {item.status} · {item.command_id}</dd></div>}
        </dl>
        {raw?.message && <><p>{ACTIVITY_WORDS_TAB.sheetMessage}</p><pre className="dv-pre">{raw.message}</pre></>}
        {raw?.context && Object.keys(raw.context).length > 0 && <><p>{ACTIVITY_WORDS_TAB.sheetContext}</p><pre className="dv-pre">{JSON.stringify(raw.context, null, 2)}</pre></>}
      </TechnicalDetails>
      <div className="dv-tool-actions">
        <Button icon="copy" onClick={() => void copy()}>{ACTIVITY_WORDS_TAB.copyDetails}</Button>
      </div>
    </Sheet>
  )
}

/** A raw /logs row as a plain Activity item (the simpler view on older backends). */
export function itemFromLog(row: DeviceLog): ActivityItem {
  const described = describeLog(row)
  return {
    type: 'event',
    id: row.id,
    at: row.received_at,
    kind: fallbackKind(described, row.level),
    topic: described.group === 'memory' ? 'storage' : described.group,
    title: described.sentence,
    action: described.kind === 'problem' || described.act === 'yes' ? described.next ?? null : null,
    code: row.code,
    fixed_at: null,
    serious: (row.level || '').toUpperCase() === 'CRITICAL',
    raw: row,
  }
}

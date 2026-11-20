// Summary (04-device §2): "What's happening" in one card, the facts people
// ask for, the last few problems and changes, and everything for IT in a
// closed Technical details box.
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { Icon } from '../../Icon'
import { formatNumber } from '../../lib/format'
import { ago, dayTime, pakistanDay, whenWithAgo } from '../../lib/time'
import { Button, DivisionTag, ErrorState, Skeleton } from '../../ui'
import { ACTIVITY_KIND_LABELS, topicLabel } from '../../words/activity'
import {
  SUMMARY_WORDS, machineBrand, machineClock, softwareVersion, whatsHappening,
  type DeviceDetail, type DeviceTab,
} from '../../words/device'
import { problemWords } from '../../words/problems'
import { deviceDivision, devicePlace, isHikvision } from './deviceNaming'
import { DeviceTechnicalDetails } from './DeviceTechnicalDetails'
import { fetchActivity, itemKey, itemTime, type ActivityItem } from './activityData'
import { useSpareToggle } from './SpareTool'

const KIND_ICON = { problem: 'x', review: 'list', retried: 'refresh', good: 'check', routine: 'info', action: 'users' } as const

export function serialMismatch(device: DeviceDetail) {
  const bound = device.zkt?.confirmed_serial || device.zkt?.expected_serial || ''
  const observed = device.zkt?.serial || ''
  return Boolean(bound && observed && bound !== observed)
}

function WhatsHappeningCard({ device, onOpenTab, onInventoryChanged }: {
  device: DeviceDetail
  onOpenTab: (tab: DeviceTab, extra?: { advanced?: boolean }) => void
  onInventoryChanged: () => Promise<void>
}) {
  const words = whatsHappening(device)
  const spare = useSpareToggle(device, onInventoryChanged)
  const reasons = device.health?.reasons ?? []
  const other = reasons.find((reason) => reason.code !== words.code)
  const total = Math.max(reasons.length, (device.health?.degraded_count ?? 0) + (device.health?.warning_count ?? 0))
  const lastCheckin = device.attendance_today?.last_checkin_at
  const arriving = words.tone === 'offline'
    ? SUMMARY_WORDS.notArriving
    : words.tone === 'attention' && lastCheckin && Date.now() - Date.parse(lastCheckin) <= 2 * 3_600_000 ? SUMMARY_WORDS.arriving : null
  const icon = words.tone === 'working' ? 'check' : words.tone === 'offline' ? 'power' : words.tone === 'attention' ? 'alert' : 'info'
  return (
    <section className={`dv-card dv-whats-happening tone-${words.tone}`} aria-labelledby="dv-whats-title" data-status-code={words.code}>
      <p className="dv-card-eyebrow"><Icon name={icon} size="sm" />{SUMMARY_WORDS.whatsHappening}</p>
      <h3 id="dv-whats-title">{words.title}</h3>
      <p>{words.meaning}{arriving ? ` ${arriving}` : ''}</p>
      {words.action && <p><strong>{SUMMARY_WORDS.whatToDo(words.action)}</strong></p>}
      {words.clearsWhen && <p>{SUMMARY_WORDS.clearsWhen(words.clearsWhen)}</p>}
      {other && total > 1 && (
        <p className="dv-also-open">
          {SUMMARY_WORDS.alsoOpen(problemWords({ code: other.code }).title)} ·{' '}
          <button type="button" className="text-button" onClick={() => onOpenTab('problems')}>{SUMMARY_WORDS.seeAll(total)}</button>
        </p>
      )}
      {serialMismatch(device) && (
        <p className="dv-mismatch">
          <Icon name="alert" size="sm" /> {SUMMARY_WORDS.serialMismatch}{' '}
          <button type="button" className="text-button" onClick={() => onOpenTab('tools', { advanced: true })}>{SUMMARY_WORDS.openAdvanced}</button>
        </p>
      )}
      {device.is_spare && (
        <div className="dv-tool-actions">
          <Button busy={spare.busy} busyLabel="Saving…" onClick={() => void spare.toggle()}>{SUMMARY_WORDS.putBack}</Button>
          {spare.error && <p className="field-error" role="alert">{spare.error}</p>}
        </div>
      )}
    </section>
  )
}

function Fact({ label, children, wide = false }: { label: string; children: React.ReactNode; wide?: boolean }) {
  return <div className={`dv-fact${wide ? ' is-wide' : ''}`}><dt>{label}</dt><dd>{children}</dd></div>
}

function KeyFacts({ device, detailQuery }: { device: DeviceDetail; detailQuery: UseQueryResult<DeviceDetail> }) {
  const today = device.attendance_today
  const loading = !today && detailQuery.isPending
  const failed = !today && detailQuery.isError
  const brand = machineBrand(device)
  const machine = device.zkt?.model
    ? <><span data-brand>{brand}</span> {device.zkt.model}</>
    : isHikvision(device) ? <span data-brand>{brand}</span> : SUMMARY_WORDS.noMachine
  const skeleton = <Skeleton width="7rem" />
  const todayValue = (render: (value: NonNullable<DeviceDetail['attendance_today']>) => React.ReactNode) =>
    loading ? skeleton : today ? render(today) : failed ? '–' : SUMMARY_WORDS.notReported
  const lastCheckin = (value: NonNullable<DeviceDetail['attendance_today']>) => {
    if (!value.last_checkin_at) return SUMMARY_WORDS.noneYet
    if (pakistanDay(value.last_checkin_at) === value.day || pakistanDay(value.last_checkin_at) === pakistanDay(new Date())) return whenWithAgo(value.last_checkin_at)
    return SUMMARY_WORDS.noneToday(dayTime(value.last_checkin_at))
  }
  const filtered = `/attendance?device=${encodeURIComponent(device.connector_id)}&when=today`
  return (
    <section className="dv-card" aria-labelledby="dv-facts-title">
      <h3 id="dv-facts-title" className="sr-only">{SUMMARY_WORDS.place}</h3>
      <dl className="dv-facts">
        <Fact label={SUMMARY_WORDS.place}>{devicePlace(device)}</Fact>
        <Fact label={SUMMARY_WORDS.division}><DivisionTag division={deviceDivision(device)} /></Fact>
        <Fact label={SUMMARY_WORDS.machine}>{machine}</Fact>
        <Fact label={SUMMARY_WORDS.software}>{softwareVersion(device.firmware_version) || SUMMARY_WORDS.notReported}</Fact>
        <Fact label={SUMMARY_WORDS.lastCheckin}>{todayValue(lastCheckin)}</Fact>
        <Fact label={SUMMARY_WORDS.checkinsToday}>{todayValue((value) => formatNumber(value.checkins_today))}</Fact>
        <Fact label={SUMMARY_WORDS.sentToHr} wide>{todayValue((value) => (
          <>
            <span>{SUMMARY_WORDS.sentOf(formatNumber(value.sent_to_hr_today), formatNumber(value.checkins_today))}</span>
            {(value.waiting_to_send > 0 || value.not_matched_today > 0) && (
              <span className="dv-fact-sub">
                {value.waiting_to_send > 0 && <a href={filtered}>{SUMMARY_WORDS.waiting(formatNumber(value.waiting_to_send))}</a>}
                {value.waiting_to_send > 0 && value.not_matched_today > 0 && ' · '}
                {value.not_matched_today > 0 && <a href={`${filtered}&status=not_matched`}>{SUMMARY_WORDS.notMatched(formatNumber(value.not_matched_today))}</a>}
              </span>
            )}
          </>
        ))}</Fact>
        <Fact label={SUMMARY_WORDS.machineClock}>{machineClock(device.zkt?.drift_seconds, device.zkt?.device_time_sampled_at)}</Fact>
      </dl>
      {failed && <ErrorState compact title={SUMMARY_WORDS.todayError} onRetry={() => detailQuery.refetch()} />}
      {today?.computed_at && <p className="dv-card-footer">{SUMMARY_WORDS.todayFooter(ago(today.computed_at))}</p>}
    </section>
  )
}

/** One short activity line (Summary's recent list). */
export function ActivityLine({ item }: { item: ActivityItem }) {
  const kind = item.kind in KIND_ICON ? item.kind : 'routine'
  return (
    <li className={`dv-activity-line kind-${kind}`}>
      <time dateTime={itemTime(item)}>{dayTime(itemTime(item))}</time>
      <span className="dv-activity-icon" aria-hidden="true"><Icon name={KIND_ICON[kind as keyof typeof KIND_ICON]} size="sm" /></span>
      <span className="sr-only">{ACTIVITY_KIND_LABELS[kind as keyof typeof ACTIVITY_KIND_LABELS]}: </span>
      <span className="dv-activity-title">{item.title}</span>
      <span className="dv-activity-topic">{topicLabel(item.topic)}</span>
    </li>
  )
}

function RecentActivity({ connectorId, onOpenTab }: { connectorId: string; onOpenTab: (tab: DeviceTab) => void }) {
  const query = useQuery({
    queryKey: ['device', connectorId, 'recent'],
    queryFn: ({ signal }) => fetchActivity(connectorId, { show: 'problems', limit: 3, since: new Date(Date.now() - 14 * 86_400_000).toISOString() }, signal),
    retry: false,
  })
  const items = (query.data?.items || []).filter((item) => item.kind !== 'routine').slice(0, 3)
  return (
    <section className="dv-card" aria-labelledby="dv-recent-title">
      <h3 id="dv-recent-title">{SUMMARY_WORDS.recent}</h3>
      {query.isPending ? <Skeleton variant="row" count={3} label={SUMMARY_WORDS.recent} />
        : query.isError ? <ErrorState compact title={SUMMARY_WORDS.recentError} onRetry={() => query.refetch()} />
          : items.length ? <ul className="dv-activity-lines">{items.map((item) => <ActivityLine key={itemKey(item)} item={item} />)}</ul>
            : <p className="dv-muted">{SUMMARY_WORDS.nothingUnusual}</p>}
      <button type="button" className="text-button dv-see-all" onClick={() => onOpenTab('activity')}>
        {SUMMARY_WORDS.seeAllActivity} <Icon name="chevron" size="sm" />
      </button>
    </section>
  )
}

export function SummaryTab({ device, detailQuery, onOpenTab, onInventoryChanged }: {
  device: DeviceDetail
  detailQuery: UseQueryResult<DeviceDetail>
  onOpenTab: (tab: DeviceTab, extra?: { advanced?: boolean }) => void
  onInventoryChanged: () => Promise<void>
}) {
  return (
    <div className="dv-summary">
      <WhatsHappeningCard device={device} onOpenTab={onOpenTab} onInventoryChanged={onInventoryChanged} />
      <KeyFacts device={device} detailQuery={detailQuery} />
      <RecentActivity connectorId={device.connector_id} onOpenTab={onOpenTab} />
      {detailQuery.isError && !detailQuery.data
        ? <ErrorState compact title={SUMMARY_WORDS.detailError} onRetry={() => detailQuery.refetch()} />
        : <DeviceTechnicalDetails device={device} />}
    </div>
  )
}

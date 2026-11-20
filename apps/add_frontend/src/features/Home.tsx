// Home (`/home`, 03-home-devices §1): four numbers, the Life and Health panels
// with only the devices that are not working, today's attendance, the new
// problems, spares and temporary admin access. The map is gone. The numbers
// count the same device rows the Devices list shows, so tiles = chips.
import { useMemo, type MouseEvent, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../api'
import { useAppData } from '../AppData'
import { queryClient } from '../data'
import { Icon } from '../Icon'
import type { DivisionChoice, DivisionKey } from '../lib/division'
import { ago, clock, dayTime, pakistanDay } from '../lib/time'
import type { Alert, Device } from '../types'
import { DivisionTag } from '../ui/DivisionTag'
import { EmptyState } from '../ui/EmptyState'
import { ErrorState } from '../ui/ErrorState'
import { PageHeader } from '../ui/PageHeader'
import { Skeleton } from '../ui/Skeleton'
import { StatTile } from '../ui/StatTile'
import { useIsPhone } from '../ui/useMediaQuery'
import { formatCount } from '../words/glossary'
import {
  ATTENDANCE_TODAY_WORDS as AT, HOME_WORDS, PROBLEMS_BLOCK_WORDS as PW, REFRESH_WORDS, homeHeadline, otherDivisionLine,
} from '../words/home'
import { problemWords } from '../words/problems'
import { DeviceStatusChip, UpdatedAgo, sinceText, useNow } from './devices/DeviceBits'
import { deviceViews, fleetCounts, notWorking, type DeviceView } from './devices/deviceList'
import { usePageDivision } from './devices/usePageDivision'
import { useStatusChange } from './devices/useStatusChange'
import { autoSendingLine } from './home/autoSendingLine'
import { MenuChangeNote } from './home/MenuChangeNote'
import { useUnknownAutoReleaseStatus } from './unknownAutoRelease'
import './Home.css'

const DIVISION_WORD: Record<DivisionKey, string> = { life: 'Life', health: 'Health' }

const withQuery = (path: string, params: Record<string, string | undefined>) => {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) if (value) query.set(key, value)
  const text = query.toString()
  return text ? `${path}?${text}` : path
}

/** In-app link: real href (new tab, copy link) and router navigation on a plain click. */
function useLink() {
  const app = useAppData()
  return (to: string) => (event: MouseEvent<HTMLElement>) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return
    event.preventDefault()
    app.navigate(to)
  }
}

export function HomeView() {
  const app = useAppData()
  const phone = useIsPhone()
  const now = useNow()
  const division = usePageDivision()
  const link = useLink()
  const views = useMemo(() => deviceViews(app.devices), [app.devices])
  const { changed, announcement } = useStatusChange(views, app.devicesLoaded)
  const scoped = useMemo(() => views.filter((view) => division === 'all' || view.division === division), [division, views])
  const counts = useMemo(() => fleetCounts(scoped), [scoped])
  const byDivision = useMemo(() => ({
    life: fleetCounts(views.filter((view) => view.division === 'life')),
    health: fleetCounts(views.filter((view) => view.division === 'health')),
  }), [views])
  const loaded = app.devicesLoaded
  const firstLoadFailed = !loaded && Boolean(app.devicesError)
  const divisionParam = division === 'all' ? undefined : division

  const onlyDivision = (key: 'attention' | 'offline') => {
    if (division !== 'all') return null
    const life = byDivision.life[key]
    const health = byDivision.health[key]
    return life && !health ? 'Life' : health && !life ? 'Health' : null
  }
  const headline = loaded
    ? homeHeadline({
        total: counts.total,
        attention: counts.attention,
        offline: counts.offline,
        setup: counts.setup,
        division: division === 'all' ? null : DIVISION_WORD[division],
        attentionDivision: onlyDivision('attention'),
        offlineDivision: onlyDivision('offline'),
      })
    : ''
  const other: DivisionKey | null = division === 'life' ? 'health' : division === 'health' ? 'life' : null
  const otherLine = loaded && other ? otherDivisionLine(DIVISION_WORD[other], byDivision[other].attention, byDivision[other].offline) : ''

  const split = (key: 'total' | 'attention' | 'offline') =>
    division === 'all' ? HOME_WORDS.divisionSplit(byDivision.life[key], byDivision.health[key]) : undefined
  const devicesHref = (status?: string, extra: Record<string, string | undefined> = {}) =>
    withQuery('/devices', { status, ...extra, division: divisionParam })
  const tiles = (
    <section className="home-tiles" aria-label={HOME_WORDS.summary} aria-busy={!loaded || undefined}>
      {!loaded && <span className="sr-only" role="status">{HOME_WORDS.loadingDevices}</span>}
      <StatTile
        label={HOME_WORDS.tiles.devices}
        value={loaded ? counts.total : null}
        loading={!loaded}
        subtitle={loaded ? <>{split('total')}{counts.setup > 0 && <>{split('total') ? ' · ' : ''}{HOME_WORDS.settingUp(counts.setup)}</>}</> : undefined}
        href={devicesHref()}
        onClick={link(devicesHref())}
      />
      <StatTile
        label={HOME_WORDS.tiles.working}
        value={loaded ? counts.working : null}
        loading={!loaded}
        tone="working"
        subtitle={loaded && counts.withNote > 0 ? HOME_WORDS.withNote(counts.withNote) : undefined}
        href={devicesHref('working')}
        onClick={link(devicesHref('working'))}
      />
      <StatTile
        label={HOME_WORDS.tiles.attention}
        value={loaded ? counts.attention : null}
        loading={!loaded}
        tone={counts.attention ? 'attention' : 'neutral'}
        subtitle={loaded ? split('attention') : undefined}
        href={devicesHref('attention')}
        onClick={link(devicesHref('attention'))}
      />
      <StatTile
        label={HOME_WORDS.tiles.offline}
        value={loaded ? counts.offline : null}
        loading={!loaded}
        tone={counts.offline ? 'offline' : 'neutral'}
        subtitle={loaded ? split('offline') : undefined}
        href={devicesHref('offline')}
        onClick={link(devicesHref('offline'))}
      />
    </section>
  )

  const panelDivisions: DivisionKey[] = division === 'all' ? ['life', 'health'] : [division]
  const panels = firstLoadFailed
    ? <ErrorState title={REFRESH_WORDS.loadFailedTitle} message={REFRESH_WORDS.loadFailedBody} onRetry={app.refreshFleet} headingLevel={2} />
    : (
      <div className={`home-panels${panelDivisions.length === 1 ? ' is-single' : ''}`}>
        {panelDivisions.map((key) => (
          <DivisionPanel
            key={key}
            division={key}
            views={views.filter((view) => view.division === key)}
            loaded={loaded}
            limit={phone ? 3 : 5}
            now={now}
            changed={changed}
          />
        ))}
      </div>
    )

  const empty = loaded && !app.devices.length
  const spares = scoped.filter((view) => view.spare)
  const leases = app.overview?.active_leases ?? 0
  const footer = (
    <div className="home-footer-lines">
      {loaded && spares.length > 0 && (
        <a className="home-line-link" href={withQuery('/devices', { spare: '1', division: divisionParam })} onClick={link(withQuery('/devices', { spare: '1', division: divisionParam }))}>
          {division === 'all'
            ? HOME_WORDS.spares(spares.length, spares.filter((view) => view.division === 'life').length, spares.filter((view) => view.division === 'health').length)
            : HOME_WORDS.sparesOne(spares.length)}
          <Icon name="chevron" size="sm" />
        </a>
      )}
      {leases > 0 && (
        <a className="home-line-link" href={withQuery('/people', { division: divisionParam })} onClick={link(withQuery('/people', { division: divisionParam }))}>
          {HOME_WORDS.leases(leases)}
          <Icon name="chevron" size="sm" />
        </a>
      )}
    </div>
  )

  return (
    <div className="home-page">
      <PageHeader title={HOME_WORDS.title} description={HOME_WORDS.description} primaryAction={<UpdatedAgo at={app.devicesUpdatedAt} />} />
      {headline && <p className="home-headline">{headline}</p>}
      {otherLine && <p className="home-other-line">{otherLine}</p>}
      <p className="sr-only" aria-live="polite">{announcement}</p>
      <MenuChangeNote />
      {empty
        ? <section className="panel"><EmptyState title={HOME_WORDS.emptyTitle} headingLevel={2}>{HOME_WORDS.emptyBody}</EmptyState></section>
        : (
          <>
            {!firstLoadFailed && tiles}
            {phone
              ? <><AttendanceToday division={division} />{panels}</>
              : <>{panels}<AttendanceToday division={division} /></>}
            <NewProblems division={division} />
            {footer}
          </>
        )}
    </div>
  )
}

function DivisionPanel({ division, views, loaded, limit, now, changed }: {
  division: DivisionKey
  views: DeviceView[]
  loaded: boolean
  limit: number
  now: number
  changed: ReadonlySet<string>
}) {
  const app = useAppData()
  const link = useLink()
  const counts = fleetCounts(views)
  const rows = notWorking(views)
  const shown = rows.slice(0, limit)
  const word = DIVISION_WORD[division]
  const id = `home-panel-${division}`
  const working = counts.working
  const total = counts.total
  const percent = total ? Math.round((working / total) * 100) : 0
  const allHref = `/devices?division=${division}`
  const notesHref = `/devices?division=${division}&status=working&note=1`
  return (
    <section className={`panel home-panel is-${division}`} aria-labelledby={id}>
      <header className="home-panel-header">
        <h2 id={id}><DivisionTag division={division} /></h2>
        {loaded
          ? <p className="home-panel-count">{total === 0 ? HOME_WORDS.panelNone : rows.length ? HOME_WORDS.panelWorking(working, total) : HOME_WORDS.panelAllWorking(total)}</p>
          : <Skeleton variant="line" width="120px" />}
      </header>
      {loaded && total > 0 && (
        <div className="home-meter" role="meter" aria-label={HOME_WORDS.panelWorking(working, total)} aria-valuemin={0} aria-valuemax={total} aria-valuenow={working}>
          <span style={{ width: `${percent}%` }} />
        </div>
      )}
      {!loaded
        ? <div className="home-panel-rows"><Skeleton variant="row" count={3} /></div>
        : rows.length === 0
          ? <p className="home-panel-fine"><Icon name="check" size="sm" /> {total ? HOME_WORDS.panelAllWorking(total) : HOME_WORDS.panelNone}</p>
          : (
            <ul className="home-panel-rows">
              {shown.map((view) => {
                const href = `/devices/${encodeURIComponent(view.id)}`
                return (
                  <li key={view.id} className="home-device" data-changed={changed.has(view.id) ? '' : undefined} data-group={view.status.group}>
                    <a
                      className="home-device-link"
                      href={href}
                      aria-label={HOME_WORDS.openDevice(view.name)}
                      onClick={(event) => {
                        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return
                        event.preventDefault()
                        app.inspectDevice(view.device)
                      }}
                    >
                      <span className="home-device-name">{view.name}</span>
                      <span className="home-device-chip"><DeviceStatusChip view={view} size="sm" /></span>
                      {view.status.heading && <span className="home-device-reason">{view.status.heading}</span>}
                      <span className="home-device-since">
                        {view.status.since ? sinceText(view.status.since, now) : ''}
                        <span className="home-device-open">{HOME_WORDS.open} <span aria-hidden="true">›</span></span>
                      </span>
                    </a>
                  </li>
                )
              })}
            </ul>
          )}
      {loaded && (
        <footer className="home-panel-footer">
          {rows.length > shown.length && (
            <a className="home-line-link" href={allHref} onClick={link(allHref)}>{HOME_WORDS.panelMore(rows.length - shown.length)}</a>
          )}
          {counts.withNote > 0 && (
            <a className="home-line-link" href={notesHref} onClick={link(notesHref)}>{HOME_WORDS.panelNotes(counts.withNote)} <Icon name="chevron" size="sm" /></a>
          )}
          <a className="home-line-link" href={allHref} onClick={link(allHref)}>{HOME_WORDS.panelShowAll(word)} <Icon name="chevron" size="sm" /></a>
        </footer>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Attendance today (H3): the shared totals endpoint (ATT-4), from Pakistan
// midnight with no end, so new check-ins keep counting.

interface SummaryBucket {
  received: number
  sent: number
  waiting: number
  trying_again?: number
  not_matched: number
  set_aside?: number
  problem?: number
  oldest_waiting_at?: string | null
}

interface AttendanceSummary {
  totals: SummaryBucket
  by_division: Record<DivisionKey, SummaryBucket>
  last_sent_at: string | null
  computed_at?: string
}

export const pakistanMidnightUtc = (now: number = Date.now()) => new Date(`${pakistanDay(now)}T00:00:00+05:00`).toISOString()

function AttendanceToday({ division }: { division: DivisionChoice }) {
  const link = useLink()
  const now = useNow(30_000)
  const fromTime = pakistanMidnightUtc(now)
  const summary = useQuery({
    queryKey: ['attendance-summary', 'home', fromTime],
    queryFn: ({ signal }) => api<AttendanceSummary>(`/api/v1/attendance/summary?from_time=${encodeURIComponent(fromTime)}`, { signal }),
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: (count, error) => (error instanceof ApiError && error.status === 503 ? count < 12 : count < 2),
    retryDelay: (_count, error) => (error instanceof ApiError && error.status === 503 ? 5_000 : 2_000),
  }, queryClient)
  const autoRelease = useUnknownAutoReleaseStatus()
  // An answer without the totals (an older backend) reads as "could not load".
  const data = summary.data && summary.data.totals && summary.data.by_division?.life && summary.data.by_division?.health ? summary.data : undefined
  const counting = summary.isPending && summary.failureReason instanceof ApiError && summary.failureReason.status === 503
  const rows: Array<{ id: DivisionChoice; label: string; bucket: SummaryBucket }> = data
    ? division === 'all'
      ? [
          { id: 'life', label: AT.life, bucket: data.by_division.life },
          { id: 'health', label: AT.health, bucket: data.by_division.health },
          { id: 'all', label: AT.all, bucket: data.totals },
        ]
      : [{ id: division, label: DIVISION_WORD[division], bucket: data.by_division[division] }]
    : []
  const cellHref = (row: DivisionChoice, status?: string) =>
    withQuery('/attendance', { when: 'today', division: row === 'all' ? undefined : row, status })
  const number = (row: DivisionChoice, value: number, status: 'sent' | 'waiting' | 'not_matched' | undefined): ReactNode => (
    <a className="home-number-link" href={cellHref(row, status)} onClick={link(cellHref(row, status))} aria-label={AT.numberLabel(row === 'all' ? AT.all : DIVISION_WORD[row as DivisionKey], value, status || 'received')}>
      {formatCount(value)}
    </a>
  )
  const auto = autoRelease.data ? autoSendingLine(autoRelease.data) : null
  const scopedBucket = data ? (division === 'all' ? data.totals : data.by_division[division]) : null

  let body: ReactNode
  if (summary.isPending && !counting) {
    body = <Skeleton variant="block" count={1} label="Loading today's attendance…" />
  } else if (counting) {
    body = <p className="home-quiet" role="status">{AT.counting}</p>
  } else if (summary.isError || !data) {
    body = <ErrorState compact title={AT.failed} onRetry={() => summary.refetch()} headingLevel={3} />
  } else if (!scopedBucket?.received) {
    body = <p className="home-quiet">{AT.noneToday}</p>
  } else {
    body = (
      <div className="home-attendance-table">
        <table>
          <caption className="sr-only">{AT.title}</caption>
          <thead>
            <tr>
              <td />
              <th scope="col">{AT.received}</th>
              <th scope="col"><span className="when-wide">{AT.sent}</span><span className="when-narrow">{AT.sentShort}</span></th>
              <th scope="col">{AT.waiting}</th>
              <th scope="col"><span className="when-wide">{AT.notMatched}</span><span className="when-narrow">{AT.notMatchedShort}</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className={row.id === 'all' ? 'is-total' : undefined}>
                <th scope="row">{row.label}</th>
                <td>{number(row.id, row.bucket.received, undefined)}</td>
                <td>{number(row.id, row.bucket.sent, 'sent')}</td>
                <td>
                  {number(row.id, row.bucket.waiting, 'waiting')}
                  {row.bucket.waiting > 0 && row.bucket.oldest_waiting_at && <small className="home-oldest">{AT.oldestWaiting(dayTime(row.bucket.oldest_waiting_at, now))}</small>}
                </td>
                <td>{number(row.id, row.bucket.not_matched, 'not_matched')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  const refused = scopedBucket && ((scopedBucket.problem || 0) > 0 || (scopedBucket.set_aside || 0) > 0)
  return (
    <section className="panel home-attendance" aria-labelledby="home-attendance-title">
      <header className="home-block-header">
        <h2 id="home-attendance-title">{AT.title}</h2>
        {data?.computed_at && <UpdatedAgo at={data.computed_at} />}
      </header>
      {body}
      {data && (
        <div className="home-attendance-lines">
          {data.last_sent_at && <p>{AT.lastSent(clock(data.last_sent_at), ago(data.last_sent_at, now))}</p>}
          {refused && (
            <p>
              <a className="home-line-link" href={withQuery('/attendance', { when: 'today', division: division === 'all' ? undefined : division, status: 'problem' })} onClick={link(withQuery('/attendance', { when: 'today', division: division === 'all' ? undefined : division, status: 'problem' }))}>
                {AT.refused(scopedBucket?.problem || 0, scopedBucket?.set_aside || 0)} <Icon name="chevron" size="sm" />
              </a>
            </p>
          )}
        </div>
      )}
      <footer className="home-block-footer">
        {auto && (
          <a className={`home-auto-line tone-${auto.tone}`} href="/attendance" onClick={link('/attendance')}>{auto.text}</a>
        )}
        <a className="home-line-link" href={withQuery('/attendance', { division: division === 'all' ? undefined : division })} onClick={link(withQuery('/attendance', { division: division === 'all' ? undefined : division }))}>
          {AT.seeAttendance} <Icon name="chevron" size="sm" />
        </a>
      </footer>
    </section>
  )
}

// ---------------------------------------------------------------------------
// New problems (H4): open problems nobody has marked as seen, worst first.

type ProblemRow = Alert & {
  plain_title?: string | null
  plain_detail?: string | null
  device?: Partial<Pick<Device, 'connector_id' | 'display_name' | 'zone_id' | 'division' | 'friendly_name'>>
}

interface ProblemsAnswer {
  rows: ProblemRow[]
  queue_totals?: { needs_action: number; acknowledged: number }
}

function NewProblems({ division }: { division: DivisionChoice }) {
  const app = useAppData()
  const link = useLink()
  const now = useNow(30_000)
  const views = useMemo(() => deviceViews(app.devices), [app.devices])
  const names = useMemo(() => new Map(views.map((view) => [view.id, view])), [views])
  const problems = useQuery({
    queryKey: ['alerts', 'home-new', division, app.revisions.alert],
    queryFn: ({ signal }) => api<ProblemsAnswer>(withQuery('/api/v1/alerts', { queue: 'NEEDS_ACTION', limit: '5', order: 'serious', division: division === 'all' ? undefined : division }), { signal }),
    staleTime: 10_000,
    placeholderData: (previous) => previous,
  }, queryClient)
  const rows = (problems.data?.rows || []).filter((row) => {
    const view = row.device?.connector_id ? names.get(row.device.connector_id) : undefined
    const rowDivision = row.device?.division || view?.division
    return division === 'all' || !rowDivision || rowDivision === division
  }).slice(0, 5)
  const fresh = problems.data?.queue_totals?.needs_action ?? rows.length
  const seen = problems.data?.queue_totals?.acknowledged ?? 0
  const problemsHref = withQuery('/problems', { division: division === 'all' ? undefined : division })
  const seenHref = withQuery('/problems', { tab: 'seen', division: division === 'all' ? undefined : division })

  let body: ReactNode
  if (problems.isPending) body = <Skeleton variant="row" count={3} label="Loading problems…" />
  else if (problems.isError && !problems.data) body = <ErrorState compact title={PW.failed} onRetry={() => problems.refetch()} headingLevel={3} />
  else if (!rows.length) {
    body = (
      <p className="home-panel-fine">
        <Icon name="check" size="sm" /> {PW.empty}
        {seen > 0 && <> <a className="home-line-link" href={seenHref} onClick={link(seenHref)}>{PW.seenStillOpen(seen)} <span aria-hidden="true">›</span></a></>}
      </p>
    )
  } else {
    body = (
      <ul className="home-problem-rows">
        {rows.map((row) => {
          const words = problemWords(row)
          const view = row.device?.connector_id ? names.get(row.device.connector_id) : undefined
          const name = row.device?.friendly_name || view?.name || ''
          const rowDivision = (row.device?.division || view?.division) as DivisionKey | undefined
          const severity = PW.severity[(row.severity || '').toUpperCase()] || 'Important'
          return (
            <li key={row.id} className="home-problem">
              <span className={`home-severity is-${severity.toLowerCase()}`}>{severity}</span>
              <span className="home-problem-title">{words.title}</span>
              <span className="home-problem-device">{name}{rowDivision && <> <DivisionTag division={rowDivision} /></>}</span>
              <small className="home-problem-since">{HOME_WORDS.since(dayTime(row.first_seen_at, now), '')}</small>
            </li>
          )
        })}
      </ul>
    )
  }
  return (
    <section className="panel home-problems" aria-labelledby="home-problems-title">
      <header className="home-block-header">
        <h2 id="home-problems-title">{PW.title}</h2>
        {problems.data && <p className="home-quiet">{PW.header(fresh, seen)}</p>}
      </header>
      {body}
      <footer className="home-block-footer">
        <a className="home-line-link" href={problemsHref} onClick={link(problemsHref)}>{PW.seeAll} <Icon name="chevron" size="sm" /></a>
      </footer>
    </section>
  )
}

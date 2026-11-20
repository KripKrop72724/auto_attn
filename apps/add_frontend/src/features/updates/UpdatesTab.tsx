// Software updates › Updates (08-advanced 2.3, today "Campaigns"): one card per
// update with the place first, a step strip, the pause reason in plain words and
// one-click actions with their own busy state. Clear history stays one click.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, queryString } from '../../api'
import { flag, oneOf, text, useUrlState } from '../../lib/urlState'
import { ago, dayTime, pakistanDay } from '../../lib/time'
import type { DivisionChoice } from '../../lib/prefs'
import type { Device, FirmwareCampaignTotals, FirmwareDeployment, FirmwareHistoryCleared } from '../../types'
import { ActionMenu, Button, Callout, DivisionTag, EmptyState, ErrorState, FilterBar, Skeleton, StatusChip, TechnicalDetails, useToast } from '../../ui'
import type { Toast } from '../../ui/Toast'
import {
  SOFTWARE_PAGE, UPDATE_SHOW, UPDATE_STRIP, downloadPercent, familyName, isDevicePause, pauseSentence, plainVersion, readPause,
  updateActionError, updateChip, updateNeedsYou, updateStepWord, updateStripIndex, updateToast, type UpdateShow,
} from '../../words/software'
import { CLOSED_STATES, LIVE_STATES, isAbort, releaseFamily, updatePlace, type CampaignResponse, type NameIndex, type UpdateRow } from './model'
import type { Catalog, RetryTarget } from './UpdatesPage'

const SHOW = ['all', 'attention', 'updating', 'waiting', 'paused', 'finished', 'stopped'] as const
const STARTED = ['any', 'today', '7d', '30d'] as const
const schema = {
  show: oneOf(SHOW, 'all'),
  device: text('', { typing: false }),
  version: text('', { typing: false }),
  started: oneOf(STARTED, 'any'),
  hidden: flag(),
  q: text(),
}
const STARTED_LABEL: Record<(typeof STARTED)[number], string> = { any: 'Any time', today: 'Today', '7d': 'Last 7 days', '30d': 'Last 30 days' }

type Action = 'pause' | 'resume' | 'cancel' | 'archive'
const BUSY_WORD: Record<Action, string> = { pause: 'Pausing…', resume: 'Continuing…', cancel: 'Stopping…', archive: 'Hiding…' }

/** Start of a Pakistan day `days` ago, in UTC, for created_from. */
function startedFrom(started: (typeof STARTED)[number]): string | undefined {
  if (started === 'any') return undefined
  const days = started === 'today' ? 0 : started === '7d' ? 6 : 29
  const day = pakistanDay(Date.now() - days * 86_400_000)
  return new Date(`${day}T00:00:00+05:00`).toISOString()
}

function serverFilter(show: (typeof SHOW)[number]) {
  switch (show) {
    case 'attention': return { attention: true }
    case 'updating': return { status: 'ACTIVE' }
    case 'waiting': return { waiting: true }
    case 'paused': return { status: 'PAUSED' }
    case 'finished': return { status: 'COMPLETED' }
    case 'stopped': return { status: 'CANCELLED' }
    default: return {}
  }
}

export function UpdatesTab({
  devices, division, names, catalog, live, refreshKey, highlight, toast, onOpenUpdate, onRetry, onChanged,
}: {
  devices: Device[]
  division: DivisionChoice
  names: NameIndex
  catalog: Catalog | null
  live: UpdateRow[] | null
  refreshKey: number
  highlight: string[]
  toast: Toast
  onOpenUpdate: (row: UpdateRow) => void
  onRetry: (target: RetryTarget) => void
  onChanged: () => Promise<void>
}) {
  const kit = useToast()
  const [filters, setFilters] = useUrlState(schema)
  const [rows, setRows] = useState<UpdateRow[] | null>(null)
  const [totals, setTotals] = useState<FirmwareCampaignTotals | null>(null)
  const [filteredTotal, setFilteredTotal] = useState(0)
  const [cursor, setCursor] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState(false)
  const [busy, setBusy] = useState<Record<string, Action>>({})
  const [clearing, setClearing] = useState(false)
  const [historyResult, setHistoryResult] = useState<FirmwareHistoryCleared | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const params = useMemo(() => ({
    view: 'summary',
    sort: 'attention',
    q: filters.q.trim() || undefined,
    zone_id: filters.device || undefined,
    release_id: filters.version || undefined,
    include_archived: filters.hidden || undefined,
    created_from: startedFrom(filters.started),
    division: division === 'all' ? undefined : division,
    limit: 24,
    ...serverFilter(filters.show),
  }), [filters, division])

  const load = useCallback(async ({ append = false, from }: { append?: boolean; from?: number } = {}) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    if (append) setLoadingMore(true)
    else setLoading(true)
    try {
      const response = await api<CampaignResponse>(`/api/v1/firmware/campaigns${queryString({ ...params, cursor: from })}`, { signal: controller.signal })
      setRows((current) => (append ? [...new Map([...(current ?? []), ...response.rows].map((row) => [row.campaign_id, row])).values()] : response.rows))
      setTotals(response.totals)
      setFilteredTotal(response.filtered_total)
      setCursor(response.next_cursor)
      setError(false)
    } catch (reason) {
      if (!isAbort(reason)) setError(true)
    } finally {
      if (abortRef.current === controller) {
        setLoading(false)
        setLoadingMore(false)
      }
    }
  }, [params])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 250)
    return () => window.clearTimeout(timer)
  }, [load, refreshKey])
  useEffect(() => () => abortRef.current?.abort(), [])

  const liveById = useMemo(() => new Map((live ?? []).map((row) => [row.campaign_id, row])), [live])
  const liveScoped = (live ?? []).filter((row) => division === 'all' || updatePlace(row, names).division === division)
  const campaigns = totals?.campaigns ?? {}
  const chipCount: Record<UpdateShow, number | null> = {
    attention: live ? liveScoped.filter(updateNeedsYou).length : null,
    updating: totals ? (campaigns.active ?? 0) : null,
    waiting: live ? liveScoped.filter((row) => row.status === 'ACTIVE' && row.waiting).length : null,
    paused: totals ? (campaigns.paused ?? 0) : null,
    finished: totals ? (campaigns.completed ?? 0) : null,
    stopped: totals ? (campaigns.cancelled ?? 0) : null,
  }

  const control = async (row: UpdateRow, action: Action, quiet = false): Promise<boolean> => {
    const place = updatePlace(row, names).name
    setBusy((current) => ({ ...current, [row.campaign_id]: action }))
    try {
      if (action === 'archive') await api(`/api/v1/firmware/campaigns/${row.campaign_id}/archive`, { method: 'POST' })
      else await api(`/api/v1/firmware/campaigns/${row.campaign_id}/${action}`, { method: 'POST', body: JSON.stringify({}) })
      if (!quiet) {
        if (action === 'pause' || action === 'resume') {
          const undo: Action = action === 'pause' ? 'resume' : 'pause'
          kit.show({
            tone: 'success',
            text: action === 'pause' ? updateToast.paused(place) : updateToast.continuing(place),
            action: { label: 'Undo', onClick: () => void control({ ...row, status: action === 'pause' ? 'PAUSED' : 'ACTIVE' }, undo, true) },
          })
        } else toast.notice(action === 'cancel' ? updateToast.stopped(place) : updateToast.hidden(place))
      }
      await Promise.all([load(), onChanged()])
      return true
    } catch (reason) {
      toast.error(updateActionError(reason instanceof Error ? reason.message : ''))
      return false
    } finally {
      setBusy((current) => {
        const next = { ...current }
        delete next[row.campaign_id]
        return next
      })
    }
  }

  const hideFinished = async () => {
    try {
      const result = await api<{ archived: number }>('/api/v1/firmware/campaigns/archive-cancelled', { method: 'POST' })
      toast.notice(updateToast.hiddenMany(result.archived))
      await load()
    } catch (reason) {
      toast.error(updateActionError(reason instanceof Error ? reason.message : ''))
    }
  }

  const clearHistory = async () => {
    setClearing(true)
    try {
      const result = await api<FirmwareHistoryCleared>('/api/v1/firmware/campaigns/clear-history', { method: 'POST' })
      setHistoryResult(result)
      await Promise.all([load(), onChanged()])
    } catch (reason) {
      toast.error(updateActionError(reason instanceof Error ? reason.message : ''))
    } finally {
      setClearing(false)
    }
  }

  const zones = useMemo(() => {
    const seen = new Map<string, { id: string; name: string; division: 'life' | 'health' }>()
    for (const device of devices) {
      if (device.is_spare || !device.zone_id || seen.has(device.zone_id)) continue
      const named = names.zone(device.zone_id)
      seen.set(device.zone_id, { id: device.zone_id, name: named.name, division: named.division })
    }
    return [...seen.values()].sort((a, b) => a.name.localeCompare(b.name, 'en', { numeric: true }))
  }, [devices, names])

  const active = [
    ...(filters.device ? [{ id: 'device', label: zones.find((zone) => zone.id === filters.device)?.name ?? 'One device', onRemove: () => setFilters({ device: '' }) }] : []),
    ...(filters.version ? [{ id: 'version', label: `Version ${plainVersion(catalog?.rows.find((row) => row.release_id === filters.version)?.version) ?? ''}`.trim(), onRemove: () => setFilters({ version: '' }) }] : []),
    ...(filters.started !== 'any' ? [{ id: 'started', label: `Started: ${STARTED_LABEL[filters.started]}`, onRemove: () => setFilters({ started: 'any' }) }] : []),
    ...(filters.hidden ? [{ id: 'hidden', label: 'Hidden shown', onRemove: () => setFilters({ hidden: false }) }] : []),
  ]
  const filtered = filters.show !== 'all' || active.length > 0 || Boolean(filters.q.trim())

  const moreFilters = (
    <div className="updates-more-filters">
      <label>
        Device
        <select value={filters.device} onChange={(event) => setFilters({ device: event.target.value })}>
          <option value="">All devices</option>
          {(['life', 'health'] as const).map((key) => (
            <optgroup key={key} label={key === 'life' ? 'Life' : 'Health'}>
              {zones.filter((zone) => zone.division === key).map((zone) => <option key={zone.id} value={zone.id}>{zone.name}</option>)}
            </optgroup>
          ))}
        </select>
      </label>
      <label>
        Version
        <select value={filters.version} onChange={(event) => setFilters({ version: event.target.value })}>
          <option value="">All versions</option>
          {(catalog?.rows ?? []).filter((row) => row.current !== false).map((row) => (
            <option key={row.release_id} value={row.release_id}>{row.version} ({familyName(releaseFamily(row))})</option>
          ))}
        </select>
      </label>
      <label>
        Started
        <select value={filters.started} onChange={(event) => setFilters({ started: event.target.value as (typeof STARTED)[number] })}>
          {STARTED.map((value) => <option key={value} value={value}>{STARTED_LABEL[value]}</option>)}
        </select>
      </label>
      <label className="updates-check">
        <input type="checkbox" checked={filters.hidden} onChange={(event) => setFilters({ hidden: event.target.checked })} />
        Show hidden{totals?.archived_campaigns ? ` (${totals.archived_campaigns})` : ''}
      </label>
    </div>
  )

  return (
    <section className="updates-list" aria-labelledby="updates-list-title">
      <header className="updates-list-header">
        <div>
          <h2 id="updates-list-title">{SOFTWARE_PAGE.updatesTitle}</h2>
          <p>{SOFTWARE_PAGE.updatesIntro}</p>
        </div>
        <div className="updates-list-actions">
          <div className="updates-clear">
            <Button variant="danger-outline" icon="trash" busy={clearing} busyLabel="Clearing…" onClick={() => clearHistory()}>{SOFTWARE_PAGE.clearHistory}</Button>
            <small>{SOFTWARE_PAGE.clearHistoryHelp}</small>
          </div>
          <ActionMenu label="More update actions" items={[{ id: 'hide-finished', label: SOFTWARE_PAGE.hideFinished, onSelect: () => void hideFinished() }]} />
        </div>
      </header>
      {historyResult && (
        <Callout
          tone="success"
          role="status"
          title="Old updates cleared"
          action={
            <span className="updates-banner-actions">
              {historyResult.more && <Button size="sm" busy={clearing} busyLabel="Clearing…" onClick={() => clearHistory()}>Clear the rest</Button>}
              <Button size="sm" variant="ghost" onClick={() => setHistoryResult(null)}>Dismiss</Button>
            </span>
          }
        >
          <p>{historyResult.cancelled} stopped · {historyResult.deleted + historyResult.archived} cleared from the list</p>
          {historyResult.kept_running.length > 0 && (
            <p>
              Left running (still installing):{' '}
              {historyResult.kept_running.map((row) => `${names.zone(row.zone_id).name} — ${updateStepWord(row.state).toLowerCase()}`).join('; ')}
            </p>
          )}
        </Callout>
      )}
      <FilterBar
        search={{ value: filters.q, onChange: (q) => setFilters({ q }), label: 'Search updates', placeholder: 'Search by place, version or person' }}
        chipsLabel="Show updates"
        chips={(Object.keys(UPDATE_SHOW) as UpdateShow[]).map((id) => ({ id, label: UPDATE_SHOW[id], count: chipCount[id] }))}
        chipValue={filters.show}
        onChipChange={(id) => setFilters({ show: filters.show === id ? 'all' : (id as UpdateShow) })}
        moreFilters={moreFilters}
        moreFiltersCount={active.length}
        active={active}
        onClearAll={filtered ? () => setFilters({ show: 'all', device: '', version: '', started: 'any', hidden: false, q: '' }) : undefined}
        result={rows ? { shown: rows.length, total: filteredTotal } : undefined}
      />
      {error && rows && rows.length > 0 && (
        <Callout tone="warning" role="status" action={<Button size="sm" onClick={() => load()}>Try again</Button>}>{SOFTWARE_PAGE.updatesFailed}</Callout>
      )}
      {error && !rows?.length ? (
        <ErrorState title="Couldn't load updates." onRetry={() => load()} />
      ) : rows === null ? (
        <Skeleton variant="block" count={3} label="Loading updates" />
      ) : (
        <div className={`updates-cards${loading ? ' is-refreshing' : ''}`} aria-busy={loading}>
          {rows.map((row) => (
            <UpdateCard
              key={row.campaign_id}
              row={row}
              live={liveById.get(row.campaign_id)}
              names={names}
              catalog={catalog}
              busy={busy[row.campaign_id]}
              highlighted={highlight.includes(row.campaign_id)}
              onControl={control}
              onOpen={() => onOpenUpdate(liveById.get(row.campaign_id) ?? row)}
              onRetry={onRetry}
            />
          ))}
          {!rows.length && (
            filtered
              ? <EmptyState title={SOFTWARE_PAGE.noUpdatesMatch} action={<Button onClick={() => setFilters({ show: 'all', device: '', version: '', started: 'any', hidden: false, q: '' })}>Clear all</Button>} />
              : <EmptyState title={SOFTWARE_PAGE.noUpdates}>{SOFTWARE_PAGE.noUpdatesHelp}</EmptyState>
          )}
        </div>
      )}
      {cursor !== null && rows && (
        <div className="load-more">
          <Button busy={loadingMore} busyLabel="Loading…" onClick={() => load({ append: true, from: cursor })}>{SOFTWARE_PAGE.showOlderUpdates}</Button>
          <small>Showing {rows.length} of {filteredTotal}</small>
        </div>
      )}
    </section>
  )
}

function stripFor(deployment: FirmwareDeployment | undefined, row: UpdateRow): { index: number; failed: boolean } {
  if (deployment) {
    const failed = ['FAILED', 'ROLLED_BACK'].includes(deployment.status)
    const index = failed ? Math.max(1, updateStripIndex(lastMoving(deployment))) : updateStripIndex(deployment.status)
    return { index, failed }
  }
  const done = (row.counts.SUCCEEDED ?? row.counts.succeeded ?? 0) > 0
  return { index: done ? 5 : 0, failed: false }
}

// The last step a failed device reached, from its events.
function lastMoving(deployment: FirmwareDeployment): string {
  const reached = [...(deployment.events ?? [])].reverse().find((event) => updateStripIndex(event.state) > 0)
  return reached?.state ?? 'DOWNLOADING'
}

export function UpdateSteps({ deployment, row, imageSize }: { deployment?: FirmwareDeployment; row: UpdateRow; imageSize?: number | null }) {
  const { index, failed } = stripFor(deployment, row)
  const percent = deployment ? downloadPercent({ ...deployment, image_size: imageSize ?? null }) : undefined
  return (
    <ol className="updates-steps" aria-label="Update steps">
      {UPDATE_STRIP.map((label, step) => {
        const state = step < index ? 'done' : step === index ? (failed ? 'failed' : index === 5 ? 'done' : 'current') : 'todo'
        return (
          <li key={label} data-state={state} aria-current={state === 'current' ? 'step' : undefined}>
            <span className="updates-step-mark" aria-hidden="true">{state === 'done' ? '✓' : state === 'failed' ? '✕' : ''}</span>
            <span>{label}{step === 1 && state === 'current' && typeof percent === 'number' ? ` ${percent}%` : ''}</span>
            <span className="sr-only">{state === 'done' ? ' (done)' : state === 'failed' ? ' (stopped here)' : state === 'current' ? ' (now)' : ''}</span>
          </li>
        )
      })}
    </ol>
  )
}

function UpdateCard({
  row, live, names, catalog, busy, highlighted, onControl, onOpen, onRetry,
}: {
  row: UpdateRow
  live?: UpdateRow
  names: NameIndex
  catalog: Catalog | null
  busy?: Action
  highlighted: boolean
  onControl: (row: UpdateRow, action: Action) => Promise<boolean>
  onOpen: () => void
  onRetry: (target: RetryTarget) => void
}) {
  const place = updatePlace(row, names)
  const chip = updateChip(row)
  const pause = readPause(row)
  const deployments = live?.deployments ?? row.deployments ?? []
  const release = catalog?.rows.find((item) => item.release_id === row.release_id)
  const family = release ? releaseFamily(release) : null
  const deployment = deployments.length === 1 ? deployments[0] : undefined
  const failedDeployment = deployments.find((item) => ['FAILED', 'ROLLED_BACK'].includes(item.status))
  const pausedName = pause.deviceName || names.device(pause.connectorId)?.name || (failedDeployment && names.device(failedDeployment.connector_id)?.name) || place.name
  const sentence = LIVE_STATES.includes(row.status) ? pauseSentence(pause, { device: pausedName, actor: row.actor }) : null
  const from = plainVersion(deployment?.previous_version)
  const to = plainVersion(row.version)
  const succeeded = row.counts.SUCCEEDED ?? row.counts.succeeded ?? 0
  const failed = (row.counts.FAILED ?? row.counts.failed ?? 0) + (row.counts.ROLLED_BACK ?? row.counts.rolled_back ?? 0)
  const moving = ['OFFERED', 'DOWNLOADING', 'VERIFYING', 'READY_TO_BOOT', 'BOOTED_PENDING', 'RECONCILING']
    .reduce((total, state) => total + (row.counts[state] ?? row.counts[state.toLowerCase()] ?? 0), 0)
  const canRetry = row.status === 'PAUSED' && isDevicePause(pause) && failedDeployment
  return (
    <article className="updates-card" data-tone={chip.chip} data-changed={highlighted ? '' : undefined} aria-labelledby={`update-${row.campaign_id}`}>
      <header className="updates-card-header">
        <h3 id={`update-${row.campaign_id}`}>
          <button type="button" className="updates-card-title" onClick={onOpen}>{place.name}</button>
          <DivisionTag division={place.division} />
        </h3>
        <StatusChip tone={chip.chip} label={busy ? BUSY_WORD[busy] : chip.label} code={row.status} />
      </header>
      <p className="updates-card-version">
        Version {from && to && from !== to ? `${from} → ${to}` : to ?? 'not known'}
        {family && <> · <span data-brand>{familyName(family)}</span></>}
      </p>
      {sentence && <p className="updates-card-reason">{sentence}</p>}
      {row.status === 'ACTIVE' && row.waiting && (
        <p className="updates-card-reason">
          {row.waiting.code === 'NATIONWIDE_TARGET_OFFLINE'
            ? `Waiting for ${names.device(row.waiting.connector_id)?.name ?? place.name} to come online.`
            : 'Waiting for its turn.'}
        </p>
      )}
      {row.eligible > 1 ? (
        <p className="updates-card-progress">{succeeded} of {row.eligible} devices updated · {moving} updating · {failed} failed</p>
      ) : (
        <UpdateSteps deployment={deployment} row={row} imageSize={release?.image_size} />
      )}
      <footer className="updates-card-actions">
        {canRetry && (
          <Button variant="primary" icon="refresh" disabled={Boolean(busy)} onClick={() => onRetry({ campaign: live ?? row, deployment: failedDeployment! })}>
            Try again on this device
          </Button>
        )}
        {row.status === 'ACTIVE' && (
          <Button icon="pause" busy={busy === 'pause'} busyLabel={BUSY_WORD.pause} disabled={Boolean(busy)} aria-label={`Pause update at ${place.name}`} onClick={() => onControl(row, 'pause')}>Pause</Button>
        )}
        {row.status === 'PAUSED' && (
          <Button
            variant={canRetry ? 'secondary' : 'primary'}
            icon="refresh"
            busy={busy === 'resume'}
            busyLabel={BUSY_WORD.resume}
            disabled={Boolean(busy) || row.release_state === 'REVOKED'}
            aria-label={`Continue update at ${place.name}`}
            onClick={() => onControl(row, 'resume')}
          >
            Continue
          </Button>
        )}
        {LIVE_STATES.includes(row.status) && (
          <Button variant="danger-outline" icon="x" className="updates-stop" busy={busy === 'cancel'} busyLabel={BUSY_WORD.cancel} disabled={Boolean(busy)} aria-label={`Stop update at ${place.name}`} onClick={() => onControl(row, 'cancel')}>
            Stop update
          </Button>
        )}
        {CLOSED_STATES.includes(row.status) && !row.archived_at && (
          <Button busy={busy === 'archive'} busyLabel={BUSY_WORD.archive} disabled={Boolean(busy)} aria-label={`Hide update at ${place.name}`} onClick={() => onControl(row, 'archive')}>Hide from list</Button>
        )}
        <Button variant="ghost" aria-label={`Details of the update at ${place.name}`} onClick={onOpen}>Details</Button>
      </footer>
      <p className="updates-card-meta">
        Started by <span data-user-text>{row.actor}</span> · {dayTime(row.created_at)} · Last change {ago(row.updated_at)}
      </p>
      {row.release_state === 'REVOKED' && row.status === 'PAUSED' && <p className="updates-card-reason">This version was withdrawn, so this update can't continue.</p>}
      <TechnicalDetails>
        <dl className="updates-facts">
          <div><dt>Update ID</dt><dd>{row.campaign_id}</dd></div>
          <div><dt>Version ID</dt><dd>{row.release_id ?? 'not known'}</dd></div>
          <div><dt>Zone</dt><dd>{row.zone_id}</dd></div>
          <div><dt>State</dt><dd>{row.status}{row.waiting ? ` · ${row.waiting.code}` : ''}</dd></div>
          {row.pause_reason && <div><dt>Pause reason</dt><dd>{row.pause_reason}</dd></div>}
          <div><dt>Note</dt><dd>{row.reason}</dd></div>
          <div><dt>Devices</dt><dd>{row.eligible} · skipped {row.legacy_skipped}</dd></div>
        </dl>
      </TechnicalDetails>
    </article>
  )
}

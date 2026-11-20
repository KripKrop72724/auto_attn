// Software updates (`/updates`, today's Firmware page): 08-advanced 2.1.
// One status line built with softwareStatus(), three tabs (Overview, Updates,
// Versions), the Update software sheet and the update details sheet. Every
// action stays one click with the same request body as before.
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { api, queryString } from '../../api'
import { ago, clock } from '../../lib/time'
import { parseDivision } from '../../lib/division'
import { useDivision } from '../../lib/useDivision'
import type { Device, FirmwareDeployment, FirmwareRelease, FirmwareSection, UpdatesSection } from '../../types'
import { Button, Callout, ErrorState, PageHeader, Skeleton, TabPanel, Tabs, TechnicalDetails } from '../../ui'
import type { Toast } from '../../ui/Toast'
import { SOFTWARE_PAGE, softwareCounts, updateNeedsYou, type SoftwareContext, type SoftwareShow } from '../../words/software'
import { plural } from '../../words/glossary'
import { RetryTargetDialog } from '../FirmwareRetry'
import { hilScopeLabel } from '../hilTargets'
import { inDivision } from '../../lib/division'
import { isAbort, liveDeployments, nameIndex, publishable, testVersions, updatePlace, visibleInterval, type CampaignResponse, type ReleaseResponse, type UpdateRow } from './model'
import { OverviewTab } from './OverviewTab'
import { UpdatesTab } from './UpdatesTab'
import { VersionsTab } from './VersionsTab'
import './updates.css'

const UpdateSheet = lazy(() => import('./UpdateSheet'))
const UpdateSoftwareSheet = lazy(() => import('./UpdateSoftwareSheet'))

export interface Catalog {
  rows: FirmwareRelease[]
  enabled: boolean
  hilEnabled: boolean
}

const SECTION_ALIASES: Record<string, UpdatesSection> = { campaigns: 'updates', releases: 'versions', prepare: 'overview' }
const normalizeSection = (section: string): UpdatesSection =>
  section === 'overview' || section === 'updates' || section === 'versions' ? section : SECTION_ALIASES[section] ?? 'overview'

// Keys each tab keeps in the URL; switching tabs drops the other tab's keys.
const PAGE_KEYS = ['show', 'device', 'version', 'started', 'hidden', 'q', 'brand', 'spares', 'cursor']

export type RetryTarget = { campaign: UpdateRow; deployment: FirmwareDeployment }

export function FirmwareView({
  devices,
  devicesLoaded = true,
  devicesError = null,
  revision,
  toast,
  section,
  onRefreshDevices,
}: {
  devices: Device[]
  /** False until the first device list answer: counts show a skeleton, never "0/0". */
  devicesLoaded?: boolean
  devicesError?: unknown
  revision: number
  toast: Toast
  section: UpdatesSection | FirmwareSection
  /** Old prop; tabs now navigate themselves. */
  onSection?: (section: FirmwareSection) => void
  onRefreshDevices?: () => Promise<void> | void
}) {
  const tab = normalizeSection(section)
  const navigate = useNavigate()
  const location = useLocation()
  const [division] = useDivision()
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [catalogError, setCatalogError] = useState(false)
  const [live, setLive] = useState<UpdateRow[] | null>(null)
  const [liveError, setLiveError] = useState(false)
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null)
  const [listRefresh, setListRefresh] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  const [updateOpen, setUpdateOpen] = useState(false)
  const [sheet, setSheet] = useState<UpdateRow | null>(null)
  const [retry, setRetry] = useState<RetryTarget | null>(null)
  const [highlight, setHighlight] = useState<string[]>([])
  const liveAbort = useRef<AbortController | null>(null)
  const updateButton = useRef<HTMLButtonElement>(null)

  const names = useMemo(() => nameIndex(devices), [devices])

  const loadCatalog = useCallback(async () => {
    try {
      const response = await api<ReleaseResponse>('/api/v1/firmware/releases?limit=200')
      setCatalog({ rows: response.rows, enabled: response.enabled, hilEnabled: response.hil_enabled })
      setCatalogError(false)
    } catch {
      setCatalogError(true)
    }
  }, [])

  // Running and paused updates with their devices: one query shared by the
  // status line, Overview, the Updates tab chips and the Update software sheet.
  const loadLive = useCallback(async () => {
    liveAbort.current?.abort()
    const controller = new AbortController()
    liveAbort.current = controller
    try {
      const [active, paused] = await Promise.all(['ACTIVE', 'PAUSED'].map((status) =>
        api<CampaignResponse>(`/api/v1/firmware/campaigns${queryString({ status, limit: 200 })}`, { signal: controller.signal })))
      setLive([...active.rows, ...paused.rows])
      setLiveError(false)
      setRefreshedAt(new Date())
    } catch (reason) {
      if (!isAbort(reason)) setLiveError(true)
    }
  }, [])

  useEffect(() => { void loadCatalog() }, [loadCatalog, revision])
  useEffect(() => {
    void loadLive()
    setListRefresh((value) => value + 1)
  }, [loadLive, revision])
  useEffect(() => () => liveAbort.current?.abort(), [])

  const anyActive = Boolean(live?.some((row) => row.status === 'ACTIVE'))
  // Live movement: poll every 15 s while an update runs and the tab is visible.
  useEffect(() => visibleInterval(anyActive, 15_000, () => {
    void loadLive()
    setListRefresh((value) => value + 1)
  }), [anyActive, loadLive])

  const refreshAll = async () => {
    setRefreshing(true)
    try {
      await Promise.all([loadCatalog(), loadLive(), onRefreshDevices?.()])
      setListRefresh((value) => value + 1)
    } finally {
      setRefreshing(false)
    }
  }
  const afterChange = useCallback(async () => {
    await Promise.all([loadLive(), loadCatalog()])
    setListRefresh((value) => value + 1)
  }, [loadCatalog, loadLive])

  const releases = catalog?.rows ?? null
  const deployments = useMemo(() => liveDeployments(live ?? [], releases ?? []), [live, releases])
  const tests = useMemo(() => testVersions(releases ?? [], devices), [releases, devices])
  const context: SoftwareContext = useMemo(() => ({
    devicesLoaded: devicesLoaded && live !== null,
    releases,
    liveDeployments: deployments,
    testVersions: tests,
  }), [devicesLoaded, live, releases, deployments, tests])
  const scoped = useMemo(() => devices.filter((device) => inDivision(device, division)), [devices, division])
  const counts = useMemo(() => softwareCounts(scoped, context), [scoped, context])
  const liveScoped = useMemo(() => (live ?? []).filter((row) => division === 'all' || updatePlace(row, names).division === division), [live, division, names])
  const attention = liveScoped.filter(updateNeedsYou).length
  const canCreate = Boolean(catalog?.rows.some((release) => publishable(release, catalog.enabled, catalog.hilEnabled)))

  const go = useCallback((next: UpdatesSection, extra: Record<string, string> = {}) => {
    const params = new URLSearchParams(location.search)
    for (const key of PAGE_KEYS) params.delete(key)
    params.set('tab', next)
    for (const [key, value] of Object.entries(extra)) params.set(key, value)
    const divisionChoice = parseDivision(location.search)
    if (divisionChoice === 'all') params.delete('division')
    navigate(`/updates?${params.toString()}`)
  }, [location.search, navigate])

  const showOverview = (show: SoftwareShow) => go('overview', { show })
  const devicesFailed = devicesLoaded && !devices.length && Boolean(devicesError)

  const statusLine = () => {
    if (devicesFailed) {
      return <ErrorState compact title={SOFTWARE_PAGE.devicesFailed} onRetry={onRefreshDevices ? () => onRefreshDevices() : undefined} />
    }
    if (!context.devicesLoaded) return <Skeleton variant="line" width="60%" label="Loading the software status" />
    if (!counts.total) return <p className="updates-status-text">No devices to show yet.</p>
    const parts: Array<{ text: string; show: SoftwareShow }> = []
    const updating = counts.updating + counts.waiting
    if (updating) parts.push({ text: `${updating} updating`, show: 'updating' })
    if (counts.failed) parts.push({ text: `${counts.failed} update failed`, show: 'attention' })
    if (counts.blocked) parts.push({ text: `${counts.blocked} can't update now`, show: 'attention' })
    if (counts.technician) parts.push({ text: `${counts.technician} ${counts.technician === 1 ? 'needs' : 'need'} a technician`, show: 'technician' })
    return (
      <p className="updates-status-text">
        {counts.current === counts.total ? (
          <span>{counts.total === 1 ? 'The device is up to date.' : `All ${counts.total} devices are up to date.`}</span>
        ) : (
          <button type="button" className="updates-status-link" onClick={() => showOverview('current')}>
            {counts.current} of {plural(counts.total, 'device')} {counts.total === 1 ? 'is' : 'are'} up to date
          </button>
        )}
        {parts.map((part) => (
          <span key={part.text}>
            <span aria-hidden="true"> · </span>
            <button type="button" className="updates-status-link" onClick={() => showOverview(part.show)}>{part.text}</button>
          </span>
        ))}
        {counts.spares > 0 && <span className="updates-status-note"> {counts.spares === 1 ? '1 spare device is not counted.' : `${counts.spares} spare devices are not counted.`}</span>}
      </p>
    )
  }

  const openUpdate = (row: UpdateRow | string) => {
    const seed = typeof row === 'string' ? (live ?? []).find((item) => item.campaign_id === row) ?? ({ campaign_id: row } as UpdateRow) : row
    setSheet(seed)
  }

  return (
    <div className="updates-page">
      <PageHeader
        title={SOFTWARE_PAGE.title}
        description={SOFTWARE_PAGE.intro}
        primaryAction={
          <Button
            ref={updateButton}
            variant="primary"
            icon="update"
            busy={catalog === null && !catalogError}
            busyLabel="Loading…"
            disabled={!canCreate}
            onClick={() => setUpdateOpen(true)}
          >
            {SOFTWARE_PAGE.updateSoftware}
          </Button>
        }
        moreActions={[{
          id: 'refresh',
          label: refreshing ? 'Refreshing…' : refreshedAt ? `Refresh · updated ${clock(refreshedAt)}` : 'Refresh',
          icon: 'refresh',
          onSelect: () => void refreshAll(),
        }]}
      />
      {catalog && !canCreate && <p className="updates-helper">{SOFTWARE_PAGE.noVersionReady}</p>}
      {catalog && !catalog.enabled && <Callout tone="warning" role="status">{SOFTWARE_PAGE.switchedOff}</Callout>}
      <section className="updates-status" aria-label="Software status">
        {statusLine()}
        <TechnicalDetails summary="Technical details">
          <ul className="updates-technical-list">
            <li>Updates for all devices: {catalog ? (catalog.enabled ? 'On' : 'Off') : 'not known yet'}</li>
            <li>Updates for test devices only: {catalog ? (catalog.hilEnabled ? 'On' : 'Off') : 'not known yet'}</li>
            {refreshedAt && <li>Running updates last read {ago(refreshedAt)}</li>}
          </ul>
        </TechnicalDetails>
      </section>
      {liveError && live !== null && <Callout tone="warning" role="status" action={<Button size="sm" onClick={() => loadLive()}>Try again</Button>}>{SOFTWARE_PAGE.updatesFailed}</Callout>}
      <Tabs
        idPrefix="updates"
        label={SOFTWARE_PAGE.tabsLabel}
        value={tab}
        onChange={(next) => go(next)}
        tabs={[
          { id: 'overview', label: 'Overview' },
          { id: 'updates', label: 'Updates', count: attention > 0 ? attention : undefined },
          { id: 'versions', label: 'Versions' },
        ]}
      />
      <TabPanel idPrefix="updates" id={tab}>
        {tab === 'overview' && (
          liveError && live === null ? (
            <ErrorState title={SOFTWARE_PAGE.updatesFailed} onRetry={() => loadLive()} />
          ) : (
            <OverviewTab
              devices={devices}
              division={division}
              context={context}
              names={names}
              catalog={catalog}
              catalogError={catalogError}
              live={live}
              onRetryCatalog={loadCatalog}
              onOpenUpdate={openUpdate}
              onRetry={setRetry}
              onOpenDevice={(device) => navigate(`/devices/${encodeURIComponent(device.connector_id)}`)}
              onGo={go}
              onSetup={() => navigate('/setup')}
              testPlan={(catalog?.rows ?? []).filter((release) => release.state === 'HIL_ONLY').map((release) => `${release.version}: ${hilScopeLabel(release)}`)}
            />
          )
        )}
        {tab === 'updates' && (
          <UpdatesTab
            devices={devices}
            division={division}
            names={names}
            catalog={catalog}
            live={live}
            refreshKey={listRefresh}
            highlight={highlight}
            toast={toast}
            onOpenUpdate={openUpdate}
            onRetry={setRetry}
            onChanged={afterChange}
          />
        )}
        {tab === 'versions' && (
          <VersionsTab devices={devices} catalog={catalog} refreshKey={listRefresh} toast={toast} onChanged={afterChange} />
        )}
      </TabPanel>
      <Suspense fallback={null}>
        {updateOpen && catalog && (
          <UpdateSoftwareSheet
            catalog={catalog}
            devices={devices}
            names={names}
            live={live ?? []}
            toast={toast}
            returnFocusRef={updateButton}
            onClose={() => setUpdateOpen(false)}
            onStarted={afterChange}
            onOpenUpdate={(id) => {
              setUpdateOpen(false)
              go('updates')
              openUpdate(id)
            }}
            onDone={(ids) => {
              setUpdateOpen(false)
              setHighlight(ids)
              window.setTimeout(() => setHighlight([]), 1200)
              go('updates')
            }}
          />
        )}
        {sheet && (
          <UpdateSheet
            seed={sheet}
            names={names}
            catalog={catalog}
            toast={toast}
            onClose={() => setSheet(null)}
            onChanged={afterChange}
            onRetry={setRetry}
            revision={listRefresh}
          />
        )}
      </Suspense>
      {retry && (
        <RetryTargetDialog
          campaign={retry.campaign}
          deployment={retry.deployment}
          deviceName={names.device(retry.deployment.connector_id)?.name ?? updatePlace(retry.campaign, names).name}
          onClose={() => setRetry(null)}
          onChanged={afterChange}
          toast={toast}
        />
      )}
    </div>
  )
}

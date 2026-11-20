// One device (04-device DEV-5): a wide side panel on tablet and desktop, a full
// page with Back on phones (route /devices/:id, ?tab=summary|activity|problems|
// people|tools). The frame shows the list values at once; each tab loads its
// own data and says so when it fails.
import { Suspense, lazy, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { useLocation, useNavigate } from 'react-router-dom'
import type { Device } from '../../types'
import { Skeleton, TabPanel, Tabs, useDismiss, useFocusTrap, useIsPhone, useToast, type TabItem } from '../../ui'
import { lockApplicationForModal, overlayRoot } from '../../ui/modalLock'
import type { Toast } from '../../ui/Toast'
import { DEVICE_FRAME, DEVICE_TAB_LABELS, DEVICE_TABS, PROBLEMS_WORDS, headerStatus, type DeviceTab } from '../../words/device'
import { problemIsNew as alertNeedsAction } from '../problems/state'
import { DeviceHeader } from './DeviceHeader'
import { deviceDivision, friendlyName } from './deviceNaming'
import { PeopleTab } from './PeopleTab'
import { ProblemsTab } from './ProblemsTab'
import { SummaryTab } from './SummaryTab'
import { ToolsTab } from './ToolsTab'
import { useActiveAlerts, useDeviceDetail, useDeviceUrl, useLiveRefresh } from './useDeviceData'
import './device.css'

const ActivityTab = lazy(() => import('./ActivityTab').then((module) => ({ default: module.ActivityTab })))

export interface DeviceViewProps {
  seed: Device
  revision: number
  custodyRevision?: number
  onClose: () => void
  onManageUsers: (device: Device) => void
  onInventoryChanged: () => Promise<void>
  /** The frame's toast (notice / error); dialogs shared with other pages use it. */
  toast: Toast
}

const ID_PREFIX = 'device'

/** Holds the device frame: a modal layer, Escape closes it, focus comes back. */
function DeviceFrame({ phone, titleId, descriptionId, onClose, initialFocus, scrollRef, children }: {
  phone: boolean
  titleId: string
  descriptionId: string
  onClose: () => void
  initialFocus: RefObject<HTMLElement | null>
  scrollRef: RefObject<HTMLDivElement | null>
  children: ReactNode
}) {
  const layerRef = useRef<HTMLDivElement>(null)
  const isTop = useDismiss(onClose)
  useFocusTrap(layerRef, { initialFocus, isTop })
  useEffect(() => lockApplicationForModal(), [])
  const dialog = (
    <div
      ref={(node) => {
        layerRef.current = node
        if (phone) scrollRef.current = node
      }}
      className={phone ? 'dv-page' : 'dv-panel'}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      data-device-frame={phone ? 'page' : 'panel'}
    >
      {children}
    </div>
  )
  return createPortal(
    phone ? dialog : (
      <div className="dv-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
        {dialog}
      </div>
    ),
    overlayRoot(),
  )
}

export function DeviceView({ seed, revision, custodyRevision = 0, onClose, onManageUsers, onInventoryChanged, toast }: DeviceViewProps) {
  const phone = useIsPhone()
  const kitToast = useToast()
  const location = useLocation()
  const navigate = useNavigate()
  const [url, setUrl] = useDeviceUrl()
  const { device, query: detailQuery } = useDeviceDetail(seed)
  const activeAlerts = useActiveAlerts(seed.connector_id)
  const refreshNow = useLiveRefresh(seed.connector_id, revision)
  const closeRef = useRef<HTMLButtonElement>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const scrollPositions = useRef(new Map<DeviceTab, number>())
  const [collapsed, setCollapsed] = useState(false)
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 10_000)
    return () => window.clearInterval(timer)
  }, [])

  const name = friendlyName(device)
  const division = deviceDivision(device)
  const status = headerStatus(device, now, { detailFailed: detailQuery.isError && !detailQuery.data })
  const tab = url.tab

  // Status change while open: say it once, politely.
  const [announcement, setAnnouncement] = useState('')
  const lastChip = useRef(status.status.chip)
  useEffect(() => {
    if (lastChip.current === status.status.chip) return
    lastChip.current = status.status.chip
    setAnnouncement(DEVICE_FRAME.nowStatus(name, status.status.chip))
  }, [name, status.status.chip])

  const needsAction = (activeAlerts.data || []).filter(alertNeedsAction)
  const tabs = useMemo<TabItem<DeviceTab>[]>(() => DEVICE_TABS.map((id) => ({
    id,
    label: DEVICE_TAB_LABELS[id],
    count: id === 'problems' && needsAction.length > 0 ? needsAction.length : undefined,
  })), [needsAction.length])

  // Each tab remembers its own scroll position.
  const changeTab = useCallback((next: DeviceTab) => {
    const scroller = scrollRef.current
    if (scroller) scrollPositions.current.set(tab, scroller.scrollTop)
    setUrl({ tab: next })
  }, [setUrl, tab])
  useLayoutEffect(() => {
    const scroller = scrollRef.current
    if (scroller) scroller.scrollTop = scrollPositions.current.get(tab) || 0
  }, [tab])

  // Phone: the header folds into one row once the page has scrolled past it.
  // Hysteresis (fold above 64 px, unfold below 8 px) and a room check (only
  // fold when the page stays scrolled after the header shrinks) stop the
  // header flickering on short tabs.
  useEffect(() => {
    const scroller = scrollRef.current
    if (!phone || !scroller) return
    let folded = false
    const onScroll = () => {
      const top = scroller.scrollTop
      if (!folded) {
        const main = scroller.querySelector('.dv-header-main') as HTMLElement | null
        const room = scroller.scrollHeight - scroller.clientHeight - (main?.offsetHeight ?? 0)
        if (top > 64 && room > 64) folded = true
      } else if (top < 8) folded = false
      setCollapsed(folded)
    }
    scroller.addEventListener('scroll', onScroll, { passive: true })
    return () => scroller.removeEventListener('scroll', onScroll)
  }, [phone])

  // Previous / Next walk the list the device was opened from (router state).
  const list = (location.state as { list?: unknown } | null)?.list
  const ids = Array.isArray(list) ? list.filter((value): value is string => typeof value === 'string') : []
  const position = ids.indexOf(seed.connector_id)
  const goTo = (id: string) => navigate(`/devices/${encodeURIComponent(id)}${location.search}`, { replace: true, state: location.state })
  const previous = position > 0 ? () => goTo(ids[position - 1]) : null
  const next = position >= 0 && position < ids.length - 1 ? () => goTo(ids[position + 1]) : null

  const titleId = 'dv-view-title'
  const descriptionId = 'dv-view-description'
  const menu = [
    {
      id: 'copy-link',
      label: DEVICE_FRAME.copyLink,
      icon: 'copy' as const,
      onSelect: () => {
        void navigator.clipboard?.writeText(window.location.href).then(() => kitToast.show({ tone: 'success', text: DEVICE_FRAME.linkCopied }), () => undefined)
      },
    },
    { id: 'people', label: DEVICE_FRAME.openPeople, icon: 'users' as const, onSelect: () => onManageUsers(device) },
  ]

  const openTab = (next: DeviceTab, extra: { advanced?: boolean } = {}) => {
    const scroller = scrollRef.current
    if (scroller) scrollPositions.current.set(tab, scroller.scrollTop)
    setUrl({ tab: next, ...(extra.advanced ? { advanced: true } : {}) })
  }

  const tabsNode = (
    <>
      <div className={`dv-tabs${phone ? ' is-phone' : ''}`}>
        <Tabs tabs={tabs} value={tab} onChange={changeTab} label={DEVICE_FRAME.sections} idPrefix={ID_PREFIX} />
        {needsAction.length > 0 && <span className="sr-only">{PROBLEMS_WORDS.newCount(needsAction.length)}</span>}
      </div>
    </>
  )
  const panelNode = (
    <>
      <TabPanel idPrefix={ID_PREFIX} id={tab} className="dv-tabpanel">
        <div className="dv-tab-content" key={tab}>
          {tab === 'summary' && <SummaryTab device={device} detailQuery={detailQuery} onOpenTab={openTab} onInventoryChanged={onInventoryChanged} />}
          {tab === 'activity' && (
            <Suspense fallback={<Skeleton variant="row" count={6} label="Loading activity" />}>
              <ActivityTab device={device} revision={revision} />
            </Suspense>
          )}
          {tab === 'problems' && <ProblemsTab device={device} activeAlerts={activeAlerts} toast={toast} onChanged={async () => { await Promise.all([refreshNow(), onInventoryChanged()]) }} />}
          {tab === 'people' && <PeopleTab device={device} revision={revision} onManageUsers={() => onManageUsers(device)} onRefreshed={refreshNow} />}
          {tab === 'tools' && (
            <ToolsTab
              device={device}
              revision={revision}
              custodyRevision={custodyRevision}
              advanced={url.advanced}
              toast={toast}
              hasOpenProblems={(activeAlerts.data || []).length > 0}
              onChanged={refreshNow}
              onInventoryChanged={onInventoryChanged}
            />
          )}
        </div>
      </TabPanel>
    </>
  )

  const header = (
    <DeviceHeader
        ref={closeRef}
        titleId={titleId}
        descriptionId={descriptionId}
        name={name}
        division={division}
        status={status}
        lastSeenAt={device.last_seen_at}
        phone={phone}
        collapsed={collapsed}
        refreshError={detailQuery.isError}
        onRetry={() => void detailQuery.refetch()}
        onClose={onClose}
        onPrevious={previous}
        onNext={next}
        menu={phone ? menu : undefined}
        announcement={announcement}
      />
  )

  return (
    <DeviceFrame phone={phone} titleId={titleId} descriptionId={descriptionId} onClose={onClose} initialFocus={closeRef} scrollRef={scrollRef}>
      {phone ? <div className="dv-sticky">{header}{tabsNode}</div> : header}
      {phone ? panelNode : <div className="dv-panel-body" ref={scrollRef}>{tabsNode}{panelNode}</div>}
    </DeviceFrame>
  )
}

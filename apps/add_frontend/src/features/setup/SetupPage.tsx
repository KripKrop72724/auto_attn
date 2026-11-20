// Set up a device (`/setup`, today Firmware › Prepare): 08-advanced 3.1,
// ADV-19 and ADV-21. Two reads decide the page: the State Life Device
// Installer apps (switched off answers INSTALLER_DISABLED) and the browser
// setup's capabilities. Never "switched off" before the answer arrives.
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../../api'
import { Callout, ErrorState, PageHeader, Skeleton } from '../../ui'
import type { Toast } from '../../ui/Toast'
import { SETUP_OFF_CARD, SETUP_PAGE } from '../../words/setup'
import InstallerAppDownload, { installerAppsSchema, touchDevice, visitorPlatform, type InstallerApps } from '../InstallerAppDownload'
import { PastSetups } from './PastSetups'
import './setup.css'

// The older browser wizard: loaded only when the server says it can start.
const FirmwareProvisioning = lazy(() => import('../FirmwareProvisioning'))

interface Capabilities {
  enabled: boolean
  can_start: boolean
}

type PageState =
  | { kind: 'loading' }
  | { kind: 'failed' }
  | { kind: 'off' }
  | { kind: 'on'; apps: InstallerApps; capabilities: Capabilities | null }

export default function SetupPage({
  revision,
  toast,
  username,
  onOpenDevice,
}: {
  revision: number
  toast: Toast
  username: string
  onOpenDevice: (connectorId: string) => void
}) {
  const [state, setState] = useState<PageState>({ kind: 'loading' })
  const [refresh, setRefresh] = useState(0)
  const touch = useMemo(() => touchDevice(navigator.userAgent, navigator.maxTouchPoints || 0), [])
  const desktop = useMemo(() => visitorPlatform(navigator.userAgent, navigator.maxTouchPoints || 0) !== null, [])

  const load = useCallback(async () => {
    const [apps, capabilities] = await Promise.allSettled([
      api<unknown>('/api/v1/installer/apps'),
      api<Capabilities>('/api/v1/provisioning/capabilities'),
    ])
    if (apps.status === 'rejected') {
      const reason = apps.reason
      setState(reason instanceof ApiError && reason.code === 'INSTALLER_DISABLED' ? { kind: 'off' } : { kind: 'failed' })
      return
    }
    const parsed = installerAppsSchema.safeParse(apps.value)
    if (!parsed.success) {
      setState({ kind: 'failed' })
      return
    }
    setState({ kind: 'on', apps: parsed.data, capabilities: capabilities.status === 'fulfilled' ? capabilities.value : null })
  }, [])

  useEffect(() => { void load() }, [load, refresh])

  const wizard = state.kind === 'on' && Boolean(state.capabilities?.enabled && state.capabilities.can_start) && desktop && !touch

  return (
    <div className="setup-page">
      <PageHeader
        title={SETUP_PAGE.title}
        description={SETUP_PAGE.intro}
        moreActions={[{ id: 'refresh', label: 'Refresh', icon: 'refresh', onSelect: () => setRefresh((value) => value + 1) }]}
      />
      {state.kind === 'loading' && (
        <div className="setup-card" aria-busy="true">
          <Skeleton variant="block" label="Loading Set up a device" />
        </div>
      )}
      {state.kind === 'failed' && (
        <ErrorState title={SETUP_PAGE.failed} onRetry={() => load()} />
      )}
      {state.kind === 'off' && (
        <Callout tone="info" title={SETUP_OFF_CARD.title} className="setup-off-card" role="status">
          <p>{SETUP_OFF_CARD.sentence}</p>
          <p><strong>{SETUP_OFF_CARD.ask}</strong></p>
        </Callout>
      )}
      {state.kind === 'on' && (
        <InstallerAppDownload toast={toast} apps={state.apps} />
      )}
      {state.kind === 'on' && !wizard && !touch && <p className="setup-note">{SETUP_PAGE.noBrowserSetup}</p>}
      {state.kind === 'on' && touch && <p className="setup-note">{SETUP_PAGE.phoneWizard}</p>}
      {wizard && (
        <Suspense fallback={<div className="setup-card"><Skeleton variant="block" label="Loading the setup steps" /></div>}>
          <FirmwareProvisioning embedded revision={revision} toast={toast} username={username} onSection={() => undefined} />
        </Suspense>
      )}
      {state.kind !== 'loading' && state.kind !== 'failed' && (
        <PastSetups username={username} refreshKey={refresh + revision} onOpenDevice={onOpenDevice} />
      )}
    </div>
  )
}

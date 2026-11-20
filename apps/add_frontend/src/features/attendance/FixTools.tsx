// Attendance › Fix tools (ATT-11): one card per tool with a plain name, one
// sentence and a status line. Opening a tool sets `tool=` in the URL and shows
// it in place with "‹ Fix tools" to go back. Every safeguard inside the tools
// (reasons, passwords, the typed phrase) and the one-click actions are unchanged.
import { useEffect, useState } from 'react'
import { api } from '../../api'
import { Icon } from '../../Icon'
import type { Device } from '../../types'
import { IdentityBacklogRelease } from '../IdentityBacklogRelease'
import { ManualForceRelease } from '../ManualForceRelease'
import { UnknownIdentityAutoRelease } from '../UnknownIdentityAutoRelease'
import { UnknownSyncedPlaces } from '../UnknownSyncedRelease'
import { remainingOf, useUnknownAutoReleaseStatus } from '../unknownAutoRelease'
import { AttendanceHistory, type HistoryKind } from './AttendanceHistory'
import type { FixTool } from './useAttendanceFilters'
import type { AttendanceAllTime } from './types'

type Toast = { notice: (text: string) => void; error: (text: string) => void }

const fmt = (value: number) => value.toLocaleString('en-PK')

function AutoSendCard({ onOpen, onShow }: { onOpen: () => void; onShow: () => void }) {
  const query = useUnknownAutoReleaseStatus(true)
  const status = query.data
  const line = query.isPending
    ? <span className="ui-skeleton ui-skeleton-line" aria-hidden="true" />
    : !status
      ? (query.isError ? "Couldn't load this. Trying again every 10 seconds." : 'Not available here.')
      : `${!status.allowed ? 'Switched off for now' : status.enabled ? 'On' : status.drain_active ? 'Off · sending all now' : 'Off'} · ${remainingOf(status) === null ? 'counting' : `${fmt(remainingOf(status) ?? 0)} still to send`} · ${fmt(status.progress.confirmed_last_hour)} sent in the last hour${status.rate_per_min ? ` · ${fmt(status.rate_per_min)}/min` : ''}`
  return <article className="fix-tool-card" aria-labelledby="fix-auto-send">
    <h3 id="fix-auto-send">Send not-matched check-ins automatically</h3>
    <p>Check-ins that arrived without a name are matched to the one person on the machine with that Employee ID and a CNIC, then sent to the HR system (Oracle). This runs by itself.</p>
    <p className="fix-tool-status">{line}</p>
    <div className="fix-tool-actions">
      <button type="button" className="button secondary" onClick={onShow}>Show these check-ins</button>
      <button type="button" className="button secondary" onClick={onOpen}>Open <Icon name="chevron" size="sm" /></button>
    </div>
  </article>
}

function AllTimeCard({ totals, loading }: { totals: AttendanceAllTime | null | undefined; loading: boolean }) {
  return <article className="fix-tool-card fix-tool-totals" aria-labelledby="fix-totals">
    <h3 id="fix-totals">All-time totals</h3>
    {totals
      ? <p className="fix-tool-status">{fmt(totals.sent)} sent · {fmt(totals.waiting)} waiting · {fmt(totals.not_matched)} not matched · {fmt(totals.set_aside)} set aside{totals.problem ? ` · ${fmt(totals.problem)} refused` : ''}</p>
      : loading ? <span className="ui-skeleton ui-skeleton-line" aria-hidden="true" /> : <p className="fix-tool-status">Couldn't load this. It is tried again with the rest of the page.</p>}
    <small>Every check-in the dashboard has kept, since the start.</small>
  </article>
}

export function FixTools({ tool, onTool, onShowMachineMatch, onChooseByHand, devices, toast, allTime, overviewLoading, deviceId, revision, historyKind, onHistoryKind, releaseJobId }: {
  tool: FixTool
  onTool: (tool: FixTool) => void
  onShowMachineMatch: () => void
  onChooseByHand: () => void
  devices: Device[]
  toast: Toast
  allTime: AttendanceAllTime | null | undefined
  overviewLoading: boolean
  deviceId: string | null
  revision: number
  historyKind: HistoryKind
  onHistoryKind: (kind: HistoryKind) => void
  releaseJobId: string | null
}) {
  const [forceEnabled, setForceEnabled] = useState<boolean | null>(null)
  useEffect(() => {
    if (tool) return
    const controller = new AbortController()
    api<{ enabled?: boolean }>('/api/v2/attendance-force-releases', { signal: controller.signal })
      .then((value) => setForceEnabled(value?.enabled !== false))
      .catch(() => { if (!controller.signal.aborted) setForceEnabled(null) })
    return () => controller.abort()
  }, [tool])

  if (tool) {
    return <div className="fix-tool-open">
      <button type="button" className="text-button fix-tool-back" onClick={() => onTool('')}><Icon name="chevronLeft" size="sm" /> Fix tools</button>
      {tool === 'auto-send' && <>
        <UnknownIdentityAutoRelease toast={toast} />
        <section className="panel fix-tool-places" aria-label="Where these check-ins are">
          <UnknownSyncedPlaces devices={devices} active />
          <button type="button" className="button secondary" onClick={onShowMachineMatch}>Show these check-ins</button>
        </section>
      </>}
      {tool === 'held-staff' && <ManualForceRelease devices={devices} toast={toast} initialConnectorId={deviceId} />}
      {tool === 'held-old' && <IdentityBacklogRelease devices={devices} toast={toast} />}
      {tool === 'history' && <AttendanceHistory devices={devices} toast={toast} kind={historyKind} onKind={onHistoryKind} releaseJobId={releaseJobId} revision={revision} />}
    </div>
  }

  const off = forceEnabled === false
  return <div className="fix-tools" aria-label="Fix tools">
    <AutoSendCard onOpen={() => onTool('auto-send')} onShow={onShowMachineMatch} />
    <article className="fix-tool-card" aria-labelledby="fix-hand">
      <h3 id="fix-hand">Send by hand</h3>
      <p>Pick check-ins in the list and send them yourself. You choose the CNIC when it changed, and confirm with your password.</p>
      <div className="fix-tool-actions"><button type="button" className="button secondary" onClick={onChooseByHand}>Choose check-ins</button></div>
    </article>
    <article className={`fix-tool-card ${off ? 'is-off' : ''}`} aria-labelledby="fix-held-staff">
      <h3 id="fix-held-staff">Re-check not-matched check-ins</h3>
      <p>Refresh the machines' people lists, see which not-matched check-ins now match a person with a CNIC, and send them after your approval.</p>
      {off ? <p className="fix-tool-status">This tool isn't switched on yet.</p>
        : <div className="fix-tool-actions"><button type="button" className="button secondary" onClick={() => onTool('held-staff')}>Open <Icon name="chevron" size="sm" /></button></div>}
    </article>
    <article className="fix-tool-card" aria-labelledby="fix-held-old">
      <h3 id="fix-held-old">Send older not-matched check-ins (one area)</h3>
      <p>For check-ins collected after a machine came back online: send the ones where the person's CNIC then is still their CNIC today.</p>
      <div className="fix-tool-actions"><button type="button" className="button secondary" onClick={() => onTool('held-old')}>Open <Icon name="chevron" size="sm" /></button></div>
    </article>
    <AllTimeCard totals={allTime} loading={overviewLoading} />
    <article className="fix-tool-card" aria-labelledby="fix-history">
      <h3 id="fix-history">History</h3>
      <p>Past sends and checks, newest first.</p>
      <div className="fix-tool-actions"><button type="button" className="button secondary" onClick={() => onTool('history')}>Open <Icon name="chevron" size="sm" /></button></div>
    </article>
  </div>
}

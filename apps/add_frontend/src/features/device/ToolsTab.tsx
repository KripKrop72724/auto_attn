// Tools (04-device §6): everyday actions first, each with a plain
// explanation and visible steps; engineering tools in a closed
// "Advanced tools · for IT" section (its own chunk, loaded when opened).
import { Suspense, lazy, useEffect, useState } from 'react'
import { Icon } from '../../Icon'
import { Skeleton } from '../../ui'
import type { Toast } from '../../ui/Toast'
import { useShowTechnical } from '../../ui/TechnicalDetailsContext'
import { TOOLS_WORDS, type DeviceDetail } from '../../words/device'
import { RecoverAttendanceAction } from './RecoverAttendanceAction'
import { RefreshPeopleAction } from './RefreshPeopleAction'
import { RestartMachineAction } from './RestartMachineAction'
import { serialMismatch } from './SummaryTab'

const AdvancedTools = lazy(() => import('./AdvancedTools').then((module) => ({ default: module.AdvancedTools })))

function ToolCard({ id, icon, title, children }: { id: string; icon: 'refresh' | 'download' | 'power'; title: string; children: React.ReactNode }) {
  return (
    <section className="dv-tool-card" aria-labelledby={`dv-tool-${id}`}>
      <h4 id={`dv-tool-${id}`}><Icon name={icon} size="sm" />{title}</h4>
      {children}
    </section>
  )
}

export function ToolsTab({ device, revision, custodyRevision, advanced, toast, hasOpenProblems, onChanged, onInventoryChanged }: {
  device: DeviceDetail
  revision: number
  custodyRevision: number
  advanced: boolean
  toast: Toast
  hasOpenProblems: boolean
  onChanged: () => Promise<void>
  onInventoryChanged: () => Promise<void>
}) {
  const showTechnical = useShowTechnical()
  const wantOpen = advanced || serialMismatch(device) || showTechnical
  const [open, setOpen] = useState(wantOpen)
  useEffect(() => { if (wantOpen) setOpen(true) }, [wantOpen])
  return (
    <div className="dv-tools">
      <section aria-labelledby="dv-tools-everyday">
        <h3 id="dv-tools-everyday">{TOOLS_WORDS.everyday}</h3>
        <div className="dv-tool-list">
          <ToolCard id="refresh" icon="refresh" title={TOOLS_WORDS.refreshTitle}>
            <RefreshPeopleAction device={device} revision={revision} onRefreshed={onChanged} />
          </ToolCard>
          <ToolCard id="recover" icon="download" title={TOOLS_WORDS.recoverTitle}>
            <RecoverAttendanceAction device={device} custodyRevision={custodyRevision} />
          </ToolCard>
          <ToolCard id="restart" icon="power" title={TOOLS_WORDS.restartTitle}>
            <RestartMachineAction device={device} revision={revision} />
          </ToolCard>
        </div>
      </section>
      <section className="dv-advanced" aria-labelledby="dv-advanced-toggle">
        <h3>
          <button
            id="dv-advanced-toggle"
            type="button"
            className="dv-advanced-toggle"
            aria-expanded={open}
            aria-controls="dv-advanced-body"
            onClick={() => setOpen((value) => !value)}
          >
            <Icon name={open ? 'chevronDown' : 'chevron'} size="sm" />
            <span>{TOOLS_WORDS.advanced}</span>
          </button>
        </h3>
        {open && (
          <div id="dv-advanced-body">
            <p className="dv-muted">{TOOLS_WORDS.advancedIntro}</p>
            <Suspense fallback={<Skeleton variant="block" label={TOOLS_WORDS.advanced} />}>
              <AdvancedTools
                device={device}
                custodyRevision={custodyRevision}
                toast={toast}
                hasOpenProblems={hasOpenProblems}
                onChanged={onChanged}
                onInventoryChanged={onInventoryChanged}
              />
            </Suspense>
          </div>
        )}
      </section>
    </div>
  )
}

// The device header (04-device §1): friendly name, Life / Health tag, one
// status chip and one plain sentence with since, then "Last heard from …".
// Desktop panel: name row with Previous / Next / Close. Phone page: Back and
// the ⋯ menu, the name as the page's h1, and a one-row header after scrolling.
import { forwardRef, useEffect, useState } from 'react'
import { Icon } from '../../Icon'
import { ago } from '../../lib/time'
import { ActionMenu, Badge, DivisionTag, IconButton, StatusChip, type ActionMenuItem, type Tone as ChipTone } from '../../ui'
import type { Tone } from '../../words/types'
import { DEVICE_FRAME, type HeaderStatus } from '../../words/device'

export const chipTone = (tone: Tone): ChipTone =>
  tone === 'good' ? 'working' : tone === 'warning' ? 'attention' : tone === 'problem' ? 'offline' : 'neutral'

/** "Last heard from 5 s ago", ticking every 10 s. */
export function LastHeard({ at }: { at: string | null | undefined }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 10_000)
    return () => window.clearInterval(timer)
  }, [])
  return <p className="dv-last-heard">{DEVICE_FRAME.lastHeard(at ? ago(at, now) : DEVICE_FRAME.never)}</p>
}

export interface DeviceHeaderProps {
  titleId: string
  descriptionId: string
  name: string
  division: 'life' | 'health'
  status: HeaderStatus
  lastSeenAt: string | null | undefined
  phone: boolean
  collapsed?: boolean
  refreshError?: boolean
  onRetry?: () => void
  onClose: () => void
  onPrevious?: (() => void) | null
  onNext?: (() => void) | null
  menu?: ActionMenuItem[]
  /** Polite announcement when the status changes while open. */
  announcement?: string
}

export const DeviceHeader = forwardRef<HTMLButtonElement, DeviceHeaderProps>(function DeviceHeader(
  { titleId, descriptionId, name, division, status, lastSeenAt, phone, collapsed = false, refreshError, onRetry, onClose, onPrevious, onNext, menu, announcement },
  closeRef,
) {
  const chip = (
    <StatusChip
      tone={chipTone(status.stale ? 'neutral' : status.status.tone)}
      label={status.stale ? 'Not confirmed' : status.status.chip}
      code={status.status.code}
      className="dv-header-chip"
    />
  )
  const sentence = <p id={descriptionId} className="dv-header-sentence">{status.sentence}</p>
  const retry = refreshError && (
    <p className="dv-header-retry" role="status">
      {DEVICE_FRAME.refreshFailed}{' '}
      {onRetry && <button type="button" className="text-button" onClick={onRetry}>Try again</button>}
    </p>
  )
  const live = <span className="sr-only" aria-live="polite">{announcement}</span>
  if (phone) {
    return (
      <header className={`dv-header is-phone${collapsed ? ' is-collapsed' : ''}`} data-collapsed={collapsed || undefined}>
        <div className="dv-header-bar">
          <button ref={closeRef} type="button" className="dv-back" onClick={onClose}>
            <Icon name="chevronLeft" size="sm" /><span>{DEVICE_FRAME.back}</span>
          </button>
          <span className="dv-header-compact" aria-hidden={!collapsed || undefined}>
            <span className="dv-header-compact-name">{name}</span>
            <span className={`dv-status-dot tone-${chipTone(status.status.tone)}`} role="img" aria-label={`Status: ${status.stale ? 'Not confirmed' : status.status.chip}`} />
          </span>
          {menu && menu.length > 0 && <ActionMenu items={menu} />}
        </div>
        <div className="dv-header-main">
          <h1 id={titleId} className="dv-header-name" tabIndex={-1}>{name}</h1>
          <div className="dv-header-tags">
            <DivisionTag division={division} />
            {chip}
            {status.noteTag && <Badge>{status.noteTag}</Badge>}
          </div>
          {sentence}
          <LastHeard at={lastSeenAt} />
          {retry}
        </div>
        {live}
      </header>
    )
  }
  return (
    <header className="dv-header is-panel">
      <div className="dv-header-top">
        <div className="dv-header-title">
          <h2 id={titleId} className="dv-header-name">{name}</h2>
          <DivisionTag division={division} />
        </div>
        <div className="dv-header-nav">
          {onPrevious && <IconButton label={DEVICE_FRAME.previous} icon="chevronLeft" onClick={onPrevious} />}
          {onNext && <IconButton label={DEVICE_FRAME.next} icon="chevron" onClick={onNext} />}
          <IconButton ref={closeRef} label="Close" icon="x" onClick={onClose} />
        </div>
      </div>
      <div className="dv-header-status">
        <div className="dv-header-tags">
          {chip}
          {status.noteTag && <Badge>{status.noteTag}</Badge>}
        </div>
        <div className="dv-header-text">
          {sentence}
          <LastHeard at={lastSeenAt} />
          {retry}
        </div>
      </div>
      {live}
    </header>
  )
})

// Status pills: text, icon and border pattern, never colour alone (moved from App.tsx).
import { Icon } from '../Icon'
import { humanizeStatus, normalizedStatus, statusPattern, terminalLinkPattern } from '../status'
import { terminalLinkLabel } from '../words/health'
import type { TerminalLink } from '../types'

export const statusIcon = {
  confirmed: 'check',
  blocked: 'alert',
  waiting: 'clock',
  notice: 'info',
} as const

export function StatusBadge({
  state,
  live = false,
}: {
  state?: string | null
  live?: boolean
}) {
  const status = normalizedStatus(state)
  const label = status.replaceAll('_', ' ')
  const pattern = statusPattern(status)
  return (
    <span className={`status-badge pattern-${pattern}`} data-pattern={pattern} aria-label={`Status: ${label}`} title={label}>
      <Icon name={statusIcon[pattern]} />
      <span>{humanizeStatus(status)}</span>
      {live && <i aria-hidden="true" />}
    </span>
  )
}

// The ESP lifecycle badge and this terminal-link badge are deliberately separate.
export function TerminalLinkBadge({ link }: { link: TerminalLink }) {
  const pattern = terminalLinkPattern(link.state)
  const label = terminalLinkLabel(link.state)
  return (
    <span className={`status-badge terminal-link-badge pattern-${pattern}`} data-pattern={pattern} aria-label={`Terminal link: ${humanizeStatus(link.state)}`} title={link.message || label}>
      <Icon name={statusIcon[pattern]} />
      <span>{label}</span>
    </span>
  )
}

export const showsTerminalLinkBadge = (link?: TerminalLink | null): link is TerminalLink =>
  Boolean(link && !['CONNECTED', 'UNKNOWN'].includes(normalizedStatus(link.state).toUpperCase()))

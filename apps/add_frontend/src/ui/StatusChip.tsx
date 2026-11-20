import { Icon, type IconName } from '../Icon'
import type { StatusPattern } from '../status'
import { cx } from './cx'
import './chips.css'

/** One meaning per tone; the label always says it in words. */
export type Tone = 'working' | 'attention' | 'offline' | 'neutral' | 'info'

const toneIcon: Record<Tone, IconName> = {
  working: 'check',
  attention: 'alert',
  offline: 'power',
  neutral: 'info',
  info: 'info',
}

// data-pattern keeps the non-colour contract of the old StatusBadge
// (confirmed / waiting / blocked / notice) so existing checks still apply.
const tonePattern: Record<Tone, StatusPattern> = {
  working: 'confirmed',
  attention: 'waiting',
  offline: 'blocked',
  neutral: 'notice',
  info: 'notice',
}

const patternTone: Record<StatusPattern, Tone> = {
  confirmed: 'working',
  waiting: 'attention',
  blocked: 'offline',
  notice: 'neutral',
}

export const toneForPattern = (pattern: StatusPattern): Tone => patternTone[pattern]
export const patternForTone = (tone: Tone): StatusPattern => tonePattern[tone]

export type StatusChipProps = {
  tone: Tone
  /** Plain words from fe/words (the caller looks them up); never a code. */
  label: string
  /** The raw code, kept on data-status-code for tests and engineers; never shown. */
  code?: string
  /** Override the tone's icon; null hides it. */
  icon?: IconName | null
  /** Prefix for the spoken name, "Status: {label}". null speaks the label alone. */
  labelPrefix?: string | null
  size?: 'sm' | 'md'
  className?: string
}

// Icon + text, one row, never truncated, no pulse, no hover title.
export function StatusChip({ tone, label, code, icon, labelPrefix = 'Status', size = 'md', className }: StatusChipProps) {
  const iconName = icon === undefined ? toneIcon[tone] : icon
  return (
    <span
      className={cx('ui-chip', `tone-${tone}`, size === 'sm' && 'ui-chip-sm', className)}
      data-tone={tone}
      data-pattern={tonePattern[tone]}
      data-status-code={code || undefined}
      aria-label={labelPrefix ? `${labelPrefix}: ${label}` : undefined}
    >
      {iconName && <Icon name={iconName} size="sm" />}
      <span>{label}</span>
    </span>
  )
}

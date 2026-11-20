import type { ReactNode } from 'react'
import { Icon, type IconName } from '../Icon'
import { cx } from './cx'
import './content.css'

export type CalloutTone = 'info' | 'success' | 'warning' | 'danger'

const calloutIcon: Record<CalloutTone, IconName> = { info: 'info', success: 'check', warning: 'alert', danger: 'alert' }

// A short message in the page: a title, one sentence, an optional action.
export function Callout({ tone = 'info', title, children, action, icon, role, className }: { tone?: CalloutTone; title?: ReactNode; children?: ReactNode; action?: ReactNode; icon?: IconName | null; role?: 'status' | 'alert' | 'note'; className?: string }) {
  const iconName = icon === undefined ? calloutIcon[tone] : icon
  return (
    <div className={cx('ui-callout', `tone-${tone}`, className)} role={role} data-tone={tone}>
      {iconName && <Icon name={iconName} className="ui-callout-icon" />}
      <div className="ui-callout-content">
        {title && <p className="ui-callout-title">{title}</p>}
        {children && <div className="ui-callout-text">{children}</div>}
      </div>
      {action && <div className="ui-callout-action">{action}</div>}
    </div>
  )
}

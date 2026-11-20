import type { ReactNode } from 'react'
import { Icon, type IconName } from '../Icon'
import { UI_TEXT } from './uiText'
import { cx } from './cx'
import './content.css'

export function EmptyState({ icon = 'list', title = UI_TEXT.emptyTitle, children, action, headingLevel = 3, className }: { icon?: IconName | null; title?: ReactNode; children?: ReactNode; action?: ReactNode; headingLevel?: 2 | 3 | 4; className?: string }) {
  const Heading = `h${headingLevel}` as 'h2' | 'h3' | 'h4'
  return (
    <div className={cx('ui-empty-state', className)}>
      {icon && <span className="ui-empty-icon"><Icon name={icon} size="lg" /></span>}
      <Heading className="ui-empty-title">{title}</Heading>
      {children && <p className="ui-empty-text">{children}</p>}
      {action && <div className="ui-empty-action">{action}</div>}
    </div>
  )
}

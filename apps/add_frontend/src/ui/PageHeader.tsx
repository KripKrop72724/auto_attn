import type { ReactNode, Ref } from 'react'
import { ActionMenu, type ActionMenuItem } from './ActionMenu'
import { cx } from './cx'

export type PageHeaderProps = {
  title: string
  /** One plain sentence; hidden on phones. */
  description?: ReactNode
  /** The page's one main button. */
  primaryAction?: ReactNode
  /** Old PageHeader prop name, kept so pages can move over without edits. */
  action?: ReactNode
  /** The rest of the actions, in the "More actions" (⋯) menu. */
  moreActions?: ActionMenuItem[]
  /** Under the title, e.g. the DivisionScopeChip. */
  children?: ReactNode
  /** The frame focuses this h1 after in-app navigation. */
  headingRef?: Ref<HTMLHeadingElement>
  className?: string
}

export function PageHeader({ title, description, primaryAction, action, moreActions, children, headingRef, className }: PageHeaderProps) {
  const main = primaryAction ?? action
  return (
    <header className={cx('ui-page-header', className)}>
      <div className="ui-page-header-text">
        <h1 ref={headingRef} tabIndex={-1}>{title}</h1>
        {description && <p className="page-header-description">{description}</p>}
        {children}
      </div>
      {(main || moreActions?.length) && (
        <div className="ui-page-header-actions">
          {main}
          {moreActions && <ActionMenu items={moreActions} />}
        </div>
      )}
    </header>
  )
}

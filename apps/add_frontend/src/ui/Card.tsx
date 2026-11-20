import { useId, type ReactNode } from 'react'
import type { Tone } from './StatusChip'
import { cx } from './cx'

export type CardProps = {
  title?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  /** Coloured left edge; the content must still say the state in words. */
  tone?: Tone
  headingLevel?: 2 | 3 | 4
  as?: 'section' | 'article' | 'div'
  children?: ReactNode
  className?: string
}

export function Card({ title, subtitle, actions, tone, headingLevel = 2, as: Tag = 'section', children, className }: CardProps) {
  const id = useId()
  const Heading = `h${headingLevel}` as 'h2' | 'h3' | 'h4'
  return (
    <Tag className={cx('ui-card', tone && `tone-${tone}`, className)} aria-labelledby={title ? `${id}-title` : undefined} data-tone={tone}>
      {(title || actions) && (
        <header className="ui-card-header">
          <div>
            {title && <Heading id={`${id}-title`} className="ui-card-title">{title}</Heading>}
            {subtitle && <p className="ui-card-subtitle">{subtitle}</p>}
          </div>
          {actions && <div className="ui-card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </Tag>
  )
}

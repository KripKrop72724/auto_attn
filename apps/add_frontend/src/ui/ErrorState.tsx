import type { ReactNode } from 'react'
import { Icon } from '../Icon'
import { Button } from './Button'
import { TechnicalDetails } from './TechnicalDetails'
import { UI_TEXT } from './uiText'
import { cx } from './cx'
import './content.css'

type ErrorLike = { status?: unknown; requestId?: unknown; plainMessage?: unknown; message?: unknown }

const asErrorLike = (error: unknown): ErrorLike =>
  error && typeof error === 'object' ? error as ErrorLike : {}

// "Couldn't load this" + one plain sentence + [Try again]. The sentence comes
// from the caller (plainApiError) or from error.plainMessage; the raw message,
// HTTP status and request reference go into Technical details, never the text.
export function ErrorState({
  title = UI_TEXT.errorTitle,
  message,
  error,
  onRetry,
  retryLabel = UI_TEXT.tryAgain,
  busyLabel = UI_TEXT.trying,
  technical,
  compact = false,
  headingLevel = 3,
  className,
}: {
  title?: ReactNode
  message?: ReactNode
  error?: unknown
  /** May return a promise: the button shows "Trying…" until it settles. */
  onRetry?: () => unknown
  retryLabel?: string
  busyLabel?: string
  technical?: ReactNode
  compact?: boolean
  headingLevel?: 2 | 3 | 4
  className?: string
}) {
  const details = asErrorLike(error)
  const sentence = message ?? (typeof details.plainMessage === 'string' && details.plainMessage ? details.plainMessage : UI_TEXT.errorFallback)
  const status = typeof details.status === 'number' && details.status > 0 ? details.status : null
  const reference = typeof details.requestId === 'string' && details.requestId ? details.requestId : null
  const raw = typeof details.message === 'string' && details.message ? details.message : null
  const hasTechnical = Boolean(technical || status || reference || raw)
  const Heading = `h${headingLevel}` as 'h2' | 'h3' | 'h4'
  return (
    <div className={cx('ui-error-state', compact && 'is-compact', className)} role="alert">
      <span className="ui-error-icon"><Icon name="alert" /></span>
      <div className="ui-error-content">
        <Heading className="ui-error-title">{title}</Heading>
        <p className="ui-error-text">{sentence}</p>
        {onRetry && <Button variant="secondary" size="sm" icon="refresh" busyLabel={busyLabel} onClick={() => onRetry()}>{retryLabel}</Button>}
        {hasTechnical && (
          <TechnicalDetails>
            {technical}
            {status && <p>HTTP {status}</p>}
            {reference && <p>{UI_TEXT.reference}: {reference}</p>}
            {raw && <p>{raw}</p>}
          </TechnicalDetails>
        )}
      </div>
    </div>
  )
}

// Plain sentence for a failed People request; the backend text goes only to
// Technical details (page-local until the kit has plainApiError, FND-12).
import { ApiError } from '../../api'
import { Icon } from '../../Icon'
import { TechnicalDetails } from '../../ui'
import { errorWords } from '../../words/errors'
import { peopleError } from '../../words/people'

const NETWORK = "Can't reach the dashboard. Check your internet and try again."

export function peopleErrorText(reason: unknown, fallback: string): { sentence: string; raw: string } {
  const raw = reason instanceof Error ? reason.message : ''
  if (reason instanceof ApiError) {
    const known = peopleError(reason.message)
    if (known) return { sentence: known, raw }
    if (reason.code) {
      const coded = errorWords(reason.code)
      if (!coded.unknown) return { sentence: coded.sentence, raw }
    }
    if (reason.status === 422 && /at least (\d+) characters/.test(reason.message)) {
      return { sentence: `Write at least ${/at least (\d+) characters/.exec(reason.message)?.[1]} letters.`, raw }
    }
    return { sentence: fallback, raw }
  }
  if (reason instanceof TypeError) return { sentence: NETWORK, raw }
  const known = peopleError(raw)
  return { sentence: known || fallback, raw }
}

/** The error line inside a dialog: the plain sentence, the raw text folded away. */
export function DialogError({ error }: { error: { sentence: string; raw: string } | null }) {
  if (!error) return null
  return (
    <div className="message pattern-blocked people-dialog-error" role="alert">
      <Icon name="alert" />
      <span>{error.sentence}</span>
      {error.raw && error.raw !== error.sentence && <TechnicalDetails>{error.raw}</TechnicalDetails>}
    </div>
  )
}

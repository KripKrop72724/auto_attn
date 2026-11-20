// One plain sentence for any failed request (FND-12, writing rule 6). The
// backend's own message is never shown as text: it is returned as `raw` for a
// Technical details line.
import { ApiError } from '../api'
import { ERROR_WORDS, NETWORK_ERROR } from '../words/errors'

export const SERVER_DOWN = "The server didn't answer. Try again in a minute."
export const WRONG_PASSWORD = "That password isn't right."

export interface PlainApiError {
  sentence: string
  /** The backend's text (or the code), for Technical details only. */
  raw: string
  code: string | null
}

const PASSWORD_REFUSED = /password confirmation failed/i

/**
 * `known`: refusal codes of this page (falls back to the shared ERROR_WORDS).
 * Network errors, 502/503/504 and a refused password get their own sentence;
 * everything else gets the caller's `fallback`.
 */
export function plainApiError(error: unknown, fallback: string, known: Record<string, string> = ERROR_WORDS): PlainApiError {
  if (error instanceof ApiError) {
    const raw = [error.code, error.message].filter(Boolean).join(' · ')
    const code = error.code ? error.code.toUpperCase() : null
    const sentence = (code && (known[code] ?? ERROR_WORDS[code]))
      || (error.status === 403 && PASSWORD_REFUSED.test(error.message) ? WRONG_PASSWORD : '')
      || (error.status === 502 || error.status === 503 || error.status === 504 ? SERVER_DOWN : '')
      || fallback
    return { sentence, raw, code: error.code }
  }
  // fetch() rejects with a TypeError when the network is down.
  if (error instanceof TypeError) return { sentence: NETWORK_ERROR, raw: error.message, code: null }
  const raw = error instanceof Error ? error.message : typeof error === 'string' ? error : ''
  return { sentence: fallback, raw, code: null }
}

/** Just the sentence, for toasts. */
export const plainErrorText = (error: unknown, fallback: string, known?: Record<string, string>) =>
  plainApiError(error, fallback, known).sentence

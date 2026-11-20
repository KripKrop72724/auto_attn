// Codes as engineers read them, for Technical details only (data-technical).
// Plain pages never call this: they look the code up in a words/ dictionary
// (plainStatus, problemWords, …) and fall back by group, never to the code.

/** "WAITING_RESOURCE" → "waiting resource". Technical details only. */
export const technicalCode = (value?: string | null): string =>
  value ? value.split('_').join(' ').toLowerCase() : ''

/** "WAITING_RESOURCE" → "WAITING RESOURCE" (case kept). Technical details only. */
export const spacedCode = (value?: string | null): string =>
  value ? value.split('_').join(' ') : ''

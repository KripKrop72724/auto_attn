// Refusal words (02-words WORDS-8, Appendix H3): every HTTP 409 `{code, message}`
// the attendance tools and the dashboard send. Passed as `known` to the
// Foundation's `plainApiError(error, known)` (FND-12). The backend message is
// never shown as text; it goes to Technical details.

const all = (codes: string[], sentence: string) => Object.fromEntries(codes.map((code) => [code, sentence]))

export const ERROR_WORDS: Record<string, string> = {
  IDEMPOTENCY_CONFLICT: 'This request was already used for a different selection. Start again.',
  CONFIRMATION_MISMATCH: "The confirmation words don't match. Type them exactly as shown.",
  PREVIEW_EXPIRED: 'This check has expired. Check again.',
  ...all(['PREVIEW_DRIFT', 'STATE_DRIFT', 'SCOPE_CHANGED', 'ZONE_CHANGED'], 'Things changed since you checked. Check again.'),
  PREVIEW_INVALID: 'This check no longer matches the area. Check the area again.',
  PREVIEW_BUSY: 'Another check of this area is running. Trying again in a moment.',
  ...all(
    ['PREVIEW_DISABLED', 'EXECUTION_DISABLED', 'CORRECTION_DISABLED', 'FORCE_DISABLED', 'REPAIR_DISABLED', 'RELEASE_PROOF_DISABLED', 'ZONE_NOT_ENABLED'],
    'This is switched off for now. Ask IT to switch it on.',
  ),
  REPAIR_RETIRED: 'Automatic attendance repair has been removed.',
  GROUPS_REQUIRED: 'Choose at least one group to release.',
  APPROVED_COUNT_INVALID: 'Enter a number between 1 and the number of check-ins waiting in the chosen groups.',
  REASON_REQUIRED: 'Enter a short reason.',
  CORRECTED_TIME_REQUIRED: 'Enter the corrected time.',
  CORRECTED_TIME_INVALID: "That time isn't possible for this check-in.",
  ...all(['NOTHING_TO_RELEASE', 'NO_ELIGIBLE_EVENTS', 'NO_ELIGIBLE_RECORDS', 'NO_ELIGIBLE_CORRECTIONS'], 'Nothing in this selection can be sent safely.'),
  RECOVERY_NOT_RETRYABLE: 'Nothing failed that can be tried again. Check again for changed items.',
  ...all(['RELEASE_ALREADY_RUNNING', 'REPAIR_ALREADY_RUNNING'], 'Another run is already working on this device or area. Open that run first.'),
  ...all(['CONTROL_CONFLICT', 'CONTROL_STATE_CONFLICT'], "That action isn't possible at this stage of the run."),
  ...all(['ACTOR_MISMATCH', 'CHECK_ACTOR_MISMATCH'], 'This check was made by another admin. Open one you made.'),
  CHECK_INCOMPLETE: 'The check is still running. Wait for it to finish.',
  ...all(['RUN_MISMATCH', 'WORKFLOW_MISMATCH'], 'This run belongs to another tool. Open it from its own page.'),
  ZONE_NOT_FOUND: 'This area has no machine this tool can work with.',
  SCOPE_TOO_LARGE: 'Too many items. Choose fewer and try again.',
  SOURCE_MISSING: 'One of the chosen check-ins is no longer saved. Check again.',
  RECHECK_UNAVAILABLE: 'The saved approvals or check-ins changed. Nothing was checked.',
  ...all(['IDENTITY_HELD', 'HISTORICAL_IDENTITY_EVIDENCE_REQUIRED'], "The dashboard can't prove which employee this belongs to."),
  ...all(['SOURCE_EVIDENCE_INVALID', 'SOURCE_NOT_PARSEABLE', 'SOURCE_IDENTITY_INVALID', 'SOURCE_REFERENCE_INVALID'], "The saved machine record is damaged and can't be used."),
  ...all(['SOURCE_IDENTITY_DRIFT', 'SOURCE_FACT_CONFLICT', 'SOURCE_SCOPE_MISMATCH'], "The saved machine record doesn't match; it needs review."),
  SOURCE_NOT_CORRECTABLE: 'Only check-ins with a wrong time can be corrected here.',
  DUPLICATE_SOURCE: 'The same record is listed twice. Remove one.',
  ACTION_NOT_ALLOWED: "That action isn't allowed here.",
  SIGNING_UNAVAILABLE: "The dashboard can't sign this request right now. Tell IT.",
  RECOVERY_INVALID: "That didn't work. Check again.",
  ADD_BUSY: 'The dashboard is busy. Trying again shortly.',
  ALERT_NOT_ACTIVE: 'This problem is already fixed.',
  // Force-release codes that also arrive as 409s use the H1 sentence.
  ROLLOUT_RESTRICTED: "Sending anyway isn't switched on for this device yet.",
  DEVICE_BUSY: 'Another change to people is using this machine. Try again when it finishes.',
}

export const ERROR_FALLBACK = "That didn't work. Try again. If it keeps happening, tell IT."
export const NETWORK_ERROR = "Can't reach the dashboard. Check your internet and try again."

/** Codes where pressing "Check again" (not "Try again") helps. */
export const CHECK_AGAIN_CODES = new Set([
  'PREVIEW_EXPIRED', 'PREVIEW_DRIFT', 'STATE_DRIFT', 'SCOPE_CHANGED', 'ZONE_CHANGED', 'PREVIEW_INVALID',
  'SOURCE_MISSING', 'RECOVERY_NOT_RETRYABLE', 'RECOVERY_INVALID',
])

/** The plain sentence of a refusal code. Unknown codes get the safe fallback. */
export function errorWords(code: string | null | undefined): { sentence: string; unknown: boolean; action: 'Check again' | 'Try again' } {
  const key = (code ?? '').toUpperCase()
  const sentence = ERROR_WORDS[key]
  return {
    sentence: sentence ?? ERROR_FALLBACK,
    unknown: !sentence,
    action: CHECK_AGAIN_CODES.has(key) ? 'Check again' : 'Try again',
  }
}

// Recover attendance words (02-words WORDS-10, Appendix G). Shared with
// 08-advanced ADV-2, which adds the page words to this file. The one-click
// "Recover attendance" and bulk start stay one click; only words change.

import { plainStatus } from './states'
import type { PlainStatus } from './types'

export const recoverJobStatus = (status: string | null | undefined): PlainStatus => plainStatus('recoverJob', status)
export const recoverStep = (operatorState: string | null | undefined): PlainStatus => plainStatus('recoverStep', operatorState)
export const recoverPhase = (phase: string | null | undefined): PlainStatus => plainStatus('recoverPhase', phase)
export const recoverScope = (scope: string | null | undefined): PlainStatus => plainStatus('recoverScope', scope)

/** ETA label (Reconciliation.tsx eta labels "Measuring", "Device wait", …). */
export const recoverEta = (label: string | null | undefined): PlainStatus => plainStatus('recoverEta', label)

/** The six milestones a check's history is shown as (G5). */
export const RECOVER_MILESTONES = ['QUEUED', 'ANCHORED', 'CHUNK_COMMITTED', 'ORACLE_MEMBERSHIP_RETRYING', 'SAFETY_HOLD', 'ORACLE_MEMBERSHIP_CERTIFIED'] as const

/** History words of one ledger event; other events stay in Technical details with these words. */
export const recoverEvent = (state: string | null | undefined): PlainStatus => plainStatus('recoverEvent', state)

/** Recovery tab (G6). */
export const recoveryAction = (action: string | null | undefined): PlainStatus => plainStatus('recoveryAction', action)
export const recoveryLane = (lane: string | null | undefined): PlainStatus => plainStatus('recoveryLane', lane)
export const recoveryJob = (status: string | null | undefined): PlainStatus => plainStatus('recoveryJob', status)
export const recoveryItem = (status: string | null | undefined): PlainStatus => plainStatus('recoveryItem', status)

/** The page and the action name. Automatic background checks are "record checks". */
export const RECOVER_WORDS = {
  page: 'Recover attendance',
  action: 'Recover attendance',
  automatic: 'record check',
  checking: 'Checking…',
} as const

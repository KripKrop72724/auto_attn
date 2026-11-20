// Recover attendance words (02-words WORDS-10, Appendix G). Shared with
// 08-advanced ADV-2, which adds the page words to this file. The one-click
// "Recover attendance" and bulk start stay one click; only words change.

import { plainStatus } from './states'
import { formatCount, plural } from './glossary'
import type { PlainStatus, Tone } from './types'

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

// ---------------------------------------------------------------------------
// Page words (08-advanced ADV-2 … ADV-9, W1). Backend strings stay as they are;
// they are shown only inside Technical details. Everything here is plain words.
// ---------------------------------------------------------------------------

/** One sentence for the tool (finalizer), used by the page intro. */
export const RECOVER_TOOL_SENTENCE = 'Reads every check-in saved on a machine and sends any the HR system is missing.'

export const RECOVER_PAGE = {
  title: 'Recover attendance',
  intro: 'Reads every check-in saved on a machine and sends any the HR system (Oracle) is missing. Use it after a device was offline, or when someone’s check-in is missing.',
  introPhone: 'Reads every check-in saved on a machine and sends any the HR system (Oracle) is missing.',
  sections: 'Recover attendance sections',
  several: 'Recover several devices',
  refresh: 'Refresh',
  refreshing: 'Refreshing…',
  updatedAt: (time: string) => `Updated ${time}`,
  switchedOff: 'Recover attendance is switched off for now. Ask IT to switch it on.',
  loadFailed: "Couldn't load recoveries.",
  nothingRunning: 'Nothing is running. Choose a device below to recover its check-ins.',
  backlogFull: 'Recoveries are waiting for a while: many older check-ins are still being sent to the HR system. They carry on by themselves.',
} as const

export type RecoverTab = 'recoveries' | 'unreadable' | 'held'

export const RECOVER_TABS: Record<RecoverTab, { label: string; phone: string }> = {
  recoveries: { label: 'Recoveries', phone: 'Recoveries' },
  unreadable: { label: 'Unreadable records', phone: 'Unreadable' },
  held: { label: 'Not in HR system yet', phone: 'Not in HR' },
}

/** "999+" for big counts on a tab. */
export const tabCount = (value: number) => (value > 999 ? '999+' : formatCount(value))

// --- Status line (replaces the four tiles) --------------------------------

export type RecoverStatusFilter = 'attention' | 'running' | 'waiting' | 'paused' | 'done' | 'stopped'

/** URL value → the server's status_group. */
export const STATUS_GROUP: Record<RecoverStatusFilter, string> = {
  attention: 'ATTENTION',
  running: 'ACTIVE',
  waiting: 'QUEUED_WAITING',
  paused: 'PAUSED',
  done: 'COMPLETED',
  stopped: 'CANCELLED',
}

export const STATUS_FILTER_WORDS: Record<RecoverStatusFilter, string> = {
  attention: 'Waiting for you',
  running: 'Running',
  waiting: 'Waiting to start',
  paused: 'Paused',
  done: 'Done',
  stopped: 'Stopped',
}

export interface StatusTotals { attention: number; active: number; queued_waiting: number; paused: number }

export interface StatusSegment { filter: RecoverStatusFilter; count: number; text: string; urgent: boolean }

/** "● 1 recovery is waiting for you · 2 running · 0 waiting to start · 1 paused". */
export function statusSegments(totals: StatusTotals): StatusSegment[] {
  const attention = totals.attention === 1
    ? '1 recovery is waiting for you'
    : `${formatCount(totals.attention)} recoveries are waiting for you`
  return [
    { filter: 'attention', count: totals.attention, text: attention, urgent: totals.attention > 0 },
    { filter: 'running', count: totals.active, text: `${formatCount(totals.active)} running`, urgent: false },
    { filter: 'waiting', count: totals.queued_waiting, text: `${formatCount(totals.queued_waiting)} waiting to start`, urgent: false },
    { filter: 'paused', count: totals.paused, text: `${formatCount(totals.paused)} paused`, urgent: false },
  ]
}

export const schedulerLines = (scheduler: { active_scan_jobs: number; device_concurrency: number; available_scan_slots: number; history_backlog?: number; history_backlog_limit?: number }) => [
  `Machines being read now: ${formatCount(scheduler.active_scan_jobs)}`,
  `Can read at once: ${formatCount(scheduler.device_concurrency)} (${formatCount(scheduler.available_scan_slots)} free)`,
  `Older check-ins waiting for the HR system: ${formatCount(scheduler.history_backlog ?? 0)} (recoveries pause above ${formatCount(scheduler.history_backlog_limit ?? 0)})`,
]

// --- Recovery card: stages, chip, sentence, action ------------------------

export type RecoverStageId = 'waiting' | 'reading' | 'checking' | 'done'
export const RECOVER_STAGES: ReadonlyArray<{ id: RecoverStageId; label: string; short: string }> = [
  { id: 'waiting', label: 'Waiting to start', short: 'Waiting' },
  { id: 'reading', label: 'Reading the machine', short: 'Reading' },
  { id: 'checking', label: 'Checking with the HR system', short: 'Checking with HR' },
  { id: 'done', label: 'Done', short: 'Done' },
]

/** How the current stage is drawn: still going, stopped by a person, stopped by a problem, or finished. */
export type StageState = 'active' | 'paused' | 'stopped' | 'failed' | 'done' | 'unknown'

/** What the card's one main button does. */
export type CardAction = 'pause' | 'resume' | 'retry' | 'cancel' | 'archive' | 'review' | 'records' | 'details' | 'attendance'

export interface RecoveryCardWords {
  stage: RecoverStageId
  stageState: StageState
  chip: string
  tone: Tone
  live: boolean
  sentence: string
  action: CardAction | null
  actionLabel: string
  /** True when Try again is pointless and must not be offered. */
  noRetry: boolean
}

/** The job fields the card words read. */
export interface RecoveryJobFacts {
  status: string
  operator_state?: string | null
  error_code?: string | null
  wait_reason?: string | null
  phase?: string | null
  capture_certified_at?: string | null
  scope?: string | null
  progress?: { oracle_review_required?: number | null } | null
  recent_tail?: { caught_up?: boolean } | null
  source_exception_assurance?: { state?: string; open?: number; newest_punch_unreceived?: number | null } | null
  /** Seconds between checks while watching for new check-ins (Hikvision catch-up). */
  poll_seconds?: number | null
}

const NEWEST_PUNCH_SENTENCE = 'The machine’s latest check-in hasn’t reached us. It is picked up by itself when the link box sends it. If it doesn’t arrive, stop this recovery and start a new one after the next check-in.'
export const newestPunchSentence = () => NEWEST_PUNCH_SENTENCE

const COVERAGE_SENTENCE = "Couldn't finish safely. Check-ins are still collected normally. Stop this recovery; IT should look at it."

export interface JobProblemWords { sentence: string; action: CardAction; label: string; noRetry: boolean }

const recordsLabel = (count: number) => `Look at ${plural(count, 'record')}`

/** W1.5: the card sentence and main button for a job's problem code. */
export function jobProblem(code: string | null | undefined, count = 1): JobProblemWords {
  const value = (code || '').toUpperCase()
  if (value === 'SOURCE_QUARANTINE_REQUIRES_REVIEW') {
    return { sentence: `${plural(count, 'record')} to look at. Mark ${count === 1 ? 'it' : 'each'} as seen and the recovery finishes by itself.`, action: 'review', label: recordsLabel(count), noRetry: true }
  }
  if (value === 'SOURCE_IDENTITY_EVIDENCE_REQUIRED') {
    return { sentence: "Some check-ins aren't matched to an employee. Marking them as seen won't send them: open Attendance with the Not matched filter and fix the employee, or stop this recovery.", action: 'records', label: 'See records not sent', noRetry: true }
  }
  if (value === 'SOURCE_INTERPRETATION_REQUIRED') {
    return { sentence: "Some records can't be read yet. IT must look at them. Nothing was sent for them.", action: 'details', label: 'Details', noRetry: true }
  }
  if (value === 'SOURCE_NEWEST_PUNCH_UNRECEIVED') {
    return { sentence: NEWEST_PUNCH_SENTENCE, action: 'records', label: 'See records not sent', noRetry: true }
  }
  if (value === 'SOURCE_EXCEPTION_SCOPE_MISMATCH') {
    return { sentence: "The numbers don't add up. Ask IT to look at this recovery's details.", action: 'details', label: 'Details', noRetry: true }
  }
  if (value === 'ORACLE_TERMINAL_OUTCOME_REQUIRES_REVIEW') {
    return { sentence: `The HR system refused ${plural(count, 'check-in')}. See them in Attendance with the Not sent filter.`, action: 'attendance', label: 'Open Attendance', noRetry: false }
  }
  if (value.startsWith('SOURCE_COVERAGE')) {
    return { sentence: COVERAGE_SENTENCE, action: 'cancel', label: 'Stop', noRetry: true }
  }
  if (value.startsWith('SOURCE_GENERATION')) {
    return { sentence: 'This device was set up again. Ask IT to compare old and new records.', action: 'details', label: 'Details', noRetry: false }
  }
  if (value === 'SOURCE_EPOCH_RECOVERY_LIMIT' || value === 'SOURCE_DIVERGENCE_UNSTABLE') {
    return { sentence: "The machine's records keep changing. Ask IT.", action: 'details', label: 'Details', noRetry: false }
  }
  if (value === 'SELF_HEALING_RECOVERY_DISABLED') {
    return { sentence: 'Automatic retry is switched off. Ask IT.', action: 'details', label: 'Details', noRetry: false }
  }
  return { sentence: 'Something unusual happened. IT can see the details below.', action: 'details', label: 'Details', noRetry: false }
}

/** True when the dictionary knows the problem code (unknown codes use the safe fallback). */
export const knowsJobProblem = (code: string) => {
  const value = code.toUpperCase()
  return ['SOURCE_QUARANTINE_REQUIRES_REVIEW', 'SOURCE_IDENTITY_EVIDENCE_REQUIRED', 'SOURCE_INTERPRETATION_REQUIRED',
    'SOURCE_NEWEST_PUNCH_UNRECEIVED', 'SOURCE_EXCEPTION_SCOPE_MISMATCH', 'ORACLE_TERMINAL_OUTCOME_REQUIRES_REVIEW',
    'SOURCE_EPOCH_RECOVERY_LIMIT', 'SOURCE_DIVERGENCE_UNSTABLE', 'SELF_HEALING_RECOVERY_DISABLED'].includes(value)
    || value.startsWith('SOURCE_COVERAGE') || value.startsWith('SOURCE_GENERATION')
}

/** W1.4 wait reasons; `running` is the number of recoveries already running. */
export function waitReasonText(code: string | null | undefined, running?: number | null): string | null {
  switch ((code || '').toUpperCase()) {
    case 'WAITING_FOR_SAFE_WINDOW': return 'Waiting for the machine to be free'
    case 'WAITING_FOR_SCAN_SLOT': return running ? `Waiting for its turn: ${plural(running, 'recovery', 'recoveries')} ${running === 1 ? 'is' : 'are'} already running` : 'Waiting for its turn'
    case 'WAITING_FOR_CAPACITY': return 'Waiting for its turn: the dashboard is busy'
    case 'WAITING_FOR_DEVICE': return 'Waiting for the link box to come online'
    case 'WAITING_FOR_ZKT': return 'Waiting for the machine to respond'
    case 'WAITING_FOR_SNAPSHOT': return "Waiting for the machine's people list"
    case 'WAITING_FOR_IDENTITY': return 'Waiting for check-ins to be matched to employees'
    case 'WAITING_FOR_PROTOCOL': return 'Waiting for newer link box software'
    case 'HISTORY_BACKLOG_BACKPRESSURE':
    case 'HISTORY_BACKLOG_CREDIT_EXHAUSTED':
    case 'WAITING_FOR_ORACLE_CAPACITY': return 'Waiting for the HR system to catch up'
    case 'COMMAND_ACTIVE': return 'Waiting for another task on this device'
    case 'LEASE_ACTIVE': return 'Waiting for temporary admin access to end'
    case 'TRANSIENT_STEP_RETRY': return 'Trying a step again'
    default: return null
  }
}

const isDoneOperator = (value: string) => value === 'COMPLETED' || value.startsWith('COMPLETED_WITH') || value === 'ALL_CONFIRMED' || value === 'CERTIFIED_WITH_REVIEWED_SOURCE_EXCEPTIONS'

/** Which stage a job has reached, read from what it is doing. */
export function stageOf(job: RecoveryJobFacts): RecoverStageId {
  const status = (job.status || '').toUpperCase()
  const operator = (job.operator_state || '').toUpperCase()
  const phase = (job.phase || '').toUpperCase()
  if (status === 'COMPLETED' || isDoneOperator(operator) || operator === 'TAIL_RECOVERY_ENDED') return 'done'
  if (status === 'QUEUED') return 'waiting'
  if (operator === 'VERIFYING_ORACLE' || phase === 'DRAINING_ORDS' || phase === 'FINAL_ASSURANCE') return 'checking'
  if (job.capture_certified_at && operator !== 'FOLLOWING_TERMINAL_TAIL') return 'checking'
  if (!job.capture_certified_at && (phase === 'PREFLIGHT' || phase === 'WAITING_FOR_SAFE_WINDOW' || phase === 'WAITING_FOR_SCAN_SLOT' || phase === 'WAITING_FOR_CAPACITY') && operator !== 'CAPTURING_SOURCE') return 'waiting'
  return 'reading'
}

const action = (value: CardAction, label: string) => ({ action: value, actionLabel: label })

/**
 * 1.3 stage table: chip, sentence, stage and main action for one recovery,
 * keyed by `status`, then `operator_state`, then `error_code`. A QUEUED job is
 * always "Waiting to start", whatever the server's message says.
 */
export function recoveryCard(job: RecoveryJobFacts, extra: { queuePosition?: number | null; running?: number | null } = {}): RecoveryCardWords {
  const status = (job.status || '').toUpperCase()
  const operator = (job.operator_state || '').toUpperCase()
  const stage = stageOf(job)
  const base = { stage, live: false, noRetry: false }
  if (status === 'QUEUED') {
    const reason = waitReasonText(job.wait_reason, extra.running)
    return { ...base, stage: 'waiting', stageState: 'active', chip: 'Waiting to start', tone: 'warning',
      sentence: extra.queuePosition ? `Number ${formatCount(extra.queuePosition)} in line.` : reason ? `${reason}.` : 'It starts by itself when the machine is free.',
      ...action('pause', 'Pause') }
  }
  if (status === 'PAUSED' || status === 'PAUSE_REQUESTED' || operator === 'PAUSED') {
    return { ...base, stageState: 'paused', chip: status === 'PAUSE_REQUESTED' ? 'Pausing' : 'Paused', tone: 'warning',
      sentence: 'Nothing is lost. Press Resume to carry on.', ...(status === 'PAUSED' ? action('resume', 'Resume') : action('pause', 'Pause')) }
  }
  if (status === 'CANCEL_REQUESTED') {
    return { ...base, stageState: 'stopped', chip: 'Stopping', tone: 'neutral', sentence: 'Check-ins already copied are kept.', action: null, actionLabel: '' }
  }
  if (status === 'CANCELLED' || operator === 'CANCELLED') {
    return { ...base, stageState: 'stopped', chip: 'Stopped', tone: 'neutral', sentence: 'Check-ins already copied are kept.', ...action('archive', 'Hide') }
  }
  if (operator === 'TAIL_RECOVERY_ENDED') {
    return { ...base, stage: 'done', stageState: 'done', chip: 'Catch-up finished', tone: 'good', sentence: 'The device is sending check-ins normally again.', ...action('archive', 'Hide') }
  }
  if (operator === 'FOLLOWING_TERMINAL_TAIL') {
    return { ...base, stage: 'reading', stageState: 'active', live: true, chip: 'Watching for new check-ins', tone: 'neutral',
      sentence: job.poll_seconds ? `Caught up. Checking every ${plural(job.poll_seconds, 'second')} until you stop it.` : 'Caught up. Checking for new check-ins until you stop it.',
      ...action('cancel', 'Stop') }
  }
  if (status === 'COMPLETED' || isDoneOperator(operator)) {
    const done = { ...base, stage: 'done' as const, stageState: 'done' as const, ...action('archive', 'Hide') }
    if (operator === 'COMPLETED_WITH_REVIEW' || operator === 'CERTIFIED_WITH_REVIEWED_SOURCE_EXCEPTIONS') return { ...done, chip: 'Done, some records not sent', tone: 'good', sentence: 'Everything else reached the HR system.' }
    if (operator === 'COMPLETED_WITH_IDENTITY_HOLDS') return { ...done, chip: 'Done, some not matched', tone: 'good', sentence: "Some check-ins aren't matched to an employee. Fix them in Attendance with the Not matched filter." }
    if (operator === 'COMPLETED_WITH_CORRECTIONS') return { ...done, chip: 'Done, some times corrected', tone: 'good', sentence: 'Corrected check-ins were sent to the HR system.' }
    return { ...done, chip: 'Done', tone: 'good', sentence: 'Every check-in on the machine reached the HR system.' }
  }
  if (status === 'NEEDS_ATTENTION' || status === 'FAILED' || status === 'INVALIDATED' || operator === 'REVIEW_REQUIRED' || operator === 'NEEDS_ATTENTION') {
    const assurance = job.source_exception_assurance
    const gate = assurance?.state || ''
    const code = gate === 'REVIEW_REQUIRED' ? 'SOURCE_QUARANTINE_REQUIRES_REVIEW'
      : gate === 'IDENTITY_EVIDENCE_REQUIRED' ? 'SOURCE_IDENTITY_EVIDENCE_REQUIRED'
        : gate === 'INTERPRETATION_REQUIRED' ? 'SOURCE_INTERPRETATION_REQUIRED'
          : gate === 'NEWEST_PUNCH_UNRECEIVED' ? 'SOURCE_NEWEST_PUNCH_UNRECEIVED'
            : gate === 'SCOPE_MISMATCH' ? 'SOURCE_EXCEPTION_SCOPE_MISMATCH'
              : job.error_code || job.wait_reason || ''
    const count = code === 'SOURCE_QUARANTINE_REQUIRES_REVIEW' ? assurance?.open ?? 1
      : code === 'ORACLE_TERMINAL_OUTCOME_REQUIRES_REVIEW' ? job.progress?.oracle_review_required ?? 1
        : 1
    const problem = jobProblem(code, Math.max(1, count))
    const known = knowsJobProblem(code)
    if (status === 'FAILED' && !known) {
      return { ...base, stageState: 'failed', chip: "Couldn't finish", tone: 'problem', sentence: "This recovery couldn't finish. IT can see the details below.", ...action('details', 'Details'), noRetry: false }
    }
    if (status === 'INVALIDATED') {
      return { ...base, stageState: 'stopped', chip: 'Replaced by a newer check', tone: 'neutral', sentence: 'A newer check replaced this one.', ...action('archive', 'Hide') }
    }
    if (!known && status === 'NEEDS_ATTENTION') {
      // A step failed with no special meaning: Try again is the way on.
      return { ...base, stageState: 'failed', chip: 'Waiting for you', tone: 'problem', sentence: 'A step didn’t work. Press Try again, or stop this recovery.', ...action('retry', 'Try again') }
    }
    return { ...base, stageState: 'failed', chip: 'Waiting for you', tone: 'problem', sentence: problem.sentence, action: problem.action, actionLabel: problem.label, noRetry: problem.noRetry }
  }
  if (status === 'RUNNING' || operator) {
    if (operator === 'VERIFYING_SOURCE_CHANGE') return { ...base, stageState: 'active', live: true, chip: 'Double-checking a record', tone: 'neutral', sentence: 'A record looks different from before; reading it again to be sure.', ...action('pause', 'Pause') }
    if (operator === 'WAITING_FOR_DEVICE') return { ...base, stageState: 'active', chip: 'Waiting for the link box', tone: 'warning', sentence: 'It is offline. The recovery carries on by itself when it is back.', ...action('pause', 'Pause') }
    if (operator === 'WAITING_FOR_ORACLE_CAPACITY') return { ...base, stageState: 'active', chip: 'Waiting for the HR system', tone: 'warning', sentence: 'Many check-ins are still being sent. This carries on when they are through.', ...action('pause', 'Pause') }
    if (operator === 'VERIFYING_ORACLE' || stage === 'checking') return { ...base, stage: 'checking', stageState: 'active', live: true, chip: 'Checking with the HR system', tone: 'neutral', sentence: 'All check-ins are copied. Making sure each one reached the HR system.', ...action('pause', 'Pause') }
    if (operator === 'RECOVERING') return { ...base, stageState: 'active', live: true, chip: 'Reading the machine', tone: 'neutral', sentence: 'It was interrupted and carries on from where it stopped.', ...action('pause', 'Pause') }
    if (operator === 'CAPTURING_SOURCE' || operator === '' || status === 'RUNNING') {
      if (operator && operator !== 'CAPTURING_SOURCE' && status !== 'RUNNING') {
        return { ...base, stageState: 'unknown', chip: 'Needs IT', tone: 'warning', sentence: 'Something unusual happened. IT can see the details below.', ...action('details', 'Details') }
      }
      return { ...base, stageState: 'active', live: true, chip: 'Reading the machine', tone: 'neutral', sentence: 'Copying check-ins from the machine.', ...action('pause', 'Pause') }
    }
  }
  return { ...base, stageState: 'unknown', chip: 'Needs IT', tone: 'warning', sentence: 'Something unusual happened. IT can see the details below.', ...action('details', 'Details') }
}

/** 1.3 rule 4: time left, with no confidence word. */
export function recoveryEta(job: { status: string; eta?: { high_seconds?: number | null; unavailable_reason?: string | null } | null }): string {
  const status = (job.status || '').toUpperCase()
  const high = job.eta?.high_seconds
  if (high != null) return `About ${plural(Math.max(1, Math.ceil(high / 60)), 'min', 'min')} left`
  if (status === 'PAUSED' || status === 'PAUSE_REQUESTED') return 'Paused'
  const reason = (job.eta?.unavailable_reason || '').toUpperCase()
  if (reason === 'COMPLETED' || status === 'COMPLETED') return 'Done'
  if (['CANCELLED', 'FAILED', 'INVALIDATED'].includes(reason) || status === 'CANCELLED') return 'Stopped'
  if (status === 'NEEDS_ATTENTION' || reason.includes('REVIEW') || reason.startsWith('SOURCE_') && reason.includes('REQUIRE')) return 'Waiting for you'
  if (reason.includes('ORACLE')) return 'Checking with the HR system'
  if (reason === 'WAITING_FOR_DEVICE') return 'Waiting for the link box'
  return 'Working out time left'
}

/** "{n} read · {n} saved · {n} not matched to an employee · {n} not sent (unreadable)"; zero parts left out. */
export function readCountsLine(progress: { scanned: number; add_durable: number; blocked_identity: number; quarantined: number; raw_preserved?: number }): string {
  const parts = [
    progress.scanned ? `${formatCount(progress.scanned)} read` : '',
    progress.add_durable ? `${formatCount(progress.add_durable)} saved` : '',
    progress.blocked_identity ? `${formatCount(progress.blocked_identity)} not matched to an employee` : '',
    progress.quarantined ? `${formatCount(progress.quarantined)} not sent (unreadable)` : '',
    progress.raw_preserved ? `${formatCount(progress.raw_preserved)} can't be read yet` : '',
  ].filter(Boolean)
  return parts.join(' · ')
}

/** "{n} reached the HR system · {n} on the way · {n} refused by the HR system · {n} not matched to an employee". */
export function hrCountsLine(progress: { oracle_confirmed: number; oracle_pending: number; oracle_review_required?: number; blocked_identity: number }): string {
  return [
    progress.oracle_confirmed ? `${formatCount(progress.oracle_confirmed)} reached the HR system` : '',
    progress.oracle_pending ? `${formatCount(progress.oracle_pending)} on the way` : '',
    progress.oracle_review_required ? `${formatCount(progress.oracle_review_required)} refused by the HR system` : '',
    progress.blocked_identity ? `${formatCount(progress.blocked_identity)} not matched to an employee` : '',
  ].filter(Boolean).join(' · ')
}

/** Finalizer done sentence: "Read 3,214 records. 12 check-ins were missing … and are now added; 3,202 were already here." */
export function doneSentence(progress: { scanned: number; add_durable: number; already_present: number }): string | null {
  if (!progress.scanned) return null
  const added = Math.max(0, progress.add_durable - progress.already_present)
  return `Read ${plural(progress.scanned, 'record')}. ${plural(added, 'check-in')} ${added === 1 ? 'was' : 'were'} missing from the dashboard and ${added === 1 ? 'is' : 'are'} now added; ${formatCount(progress.already_present)} ${progress.already_present === 1 ? 'was' : 'were'} already here.`
}

/** The source assurance strip on the card (one plain line, W1.4). */
export function sourceStripText(state: string | undefined, status: string, open: number): string | null {
  if ((status || '').toUpperCase() === 'CANCELLED') return 'Stopped. Unreadable records are kept.'
  switch (state) {
    case 'INTERPRETATION_REQUIRED': return "Some records can't be read yet"
    case 'IDENTITY_EVIDENCE_REQUIRED': return "Some check-ins aren't matched to an employee"
    case 'NEWEST_PUNCH_UNRECEIVED': return 'Latest check-in not received yet'
    case 'REVIEW_REQUIRED': return `${plural(open, 'record')} to look at`
    case 'SCOPE_MISMATCH': return "The numbers don't add up. Ask IT."
    case 'REVIEWED_EXCLUSIONS':
    case 'CORRECTED_DERIVED_EVENTS':
      return (status || '').toUpperCase() === 'COMPLETED' ? 'Done. Some records were not sent.' : 'All records looked at'
    default: return null
  }
}

// --- Scope (Hikvision only) ------------------------------------------------

export const SCOPE_WORDS: Record<'ALL_RECORDS' | 'ACTIVE_USERS' | 'RECENT_TAIL', { label: string; help: string }> = {
  ALL_RECORDS: { label: 'Everything on the machine', help: 'Reads every record on the machine, including people who have left.' },
  ACTIVE_USERS: { label: 'Current staff only', help: 'Reads only the check-ins of the people now on the machine. People who have left are skipped.' },
  RECENT_TAIL: { label: 'Only the newest check-ins (catch up)', help: 'Reads every check-in after the last one we received and keeps watching for new ones.' },
}

export const scopeCardLine = (job: { mode?: string; scope?: string | null; active_user_scope?: { completed_users: number; user_count: number } | null; recent_tail?: { records?: number; anchor_serial?: number | null } | null }): string | null => {
  if (job.scope === 'ACTIVE_USERS') return `Current staff only · ${formatCount(job.active_user_scope?.completed_users ?? 0)} of ${plural(job.active_user_scope?.user_count ?? 0, 'person')} done`
  if (job.scope === 'RECENT_TAIL') return `Newest check-ins · ${formatCount(job.recent_tail?.records ?? 0)} new since record ${job.recent_tail?.anchor_serial ?? '?'}`
  return 'Everything on the machine'
}

export function activeUsersHelp(count: number | null | undefined): string {
  return count ? `Reads only the check-ins of the ${plural(count, 'person')} now on the machine. People who have left are skipped.` : SCOPE_WORDS.ACTIVE_USERS.help
}

export function recentTailHelp(begin: number | null | undefined, earlier: number[] | null | undefined): string {
  const start = begin != null ? `Starts from record ${formatCount(begin)} (the last one we received) and keeps watching for new check-ins until you stop it. It does not check older records.` : SCOPE_WORDS.RECENT_TAIL.help
  if (!earlier?.length) return start
  return `${start} There are older gaps after record ${earlier.map(formatCount).join(', ')}: use "Everything on the machine" to cover them.`
}

/** A backend reason for a scope that can't be used, in plain words. */
export function scopeUnavailable(scope: 'ACTIVE_USERS' | 'RECENT_TAIL', reason: string | null | undefined): string {
  const text = (reason || '').toLowerCase()
  if (scope === 'RECENT_TAIL' && text.includes('checkpointed')) return 'We have no saved starting point for this machine. Use "Everything on the machine".'
  if (scope === 'ACTIVE_USERS' && text.includes('not qualified')) return 'This machine model can’t do "Current staff only".'
  if (scope === 'ACTIVE_USERS' && (text.includes('snapshot') || text.includes('refresh'))) return 'Refresh the people list on this machine first (it must be less than 15 minutes old).'
  if (text.includes('active reconciliation') || text.includes('finish or cancel')) return 'A recovery is already running on this device. Stop it first.'
  return scope === 'RECENT_TAIL' ? '"Only the newest check-ins" can’t be used for this machine.' : '"Current staff only" can’t be used for this machine.'
}

// --- Readiness: verdict, blockers, the eight checks -----------------------

/** 3.6 plain blocker titles, read after "Can't start:" (detail stays in Technical details). */
export const BLOCKER_TITLES: Record<string, string> = {
  FEATURE_DISABLED: 'Recover attendance is switched off',
  CONNECTOR_INACTIVE: 'this device is switched off in the dashboard',
  DUPLICATE_SERIAL_QUARANTINE: 'it is blocked as a duplicate device. Fix that first',
  NO_ZKT: 'no attendance machine is linked to this device',
  FIRMWARE_UNSUPPORTED: 'the link box needs a software update (2.3.0 or newer)',
  HISTORY_STREAM_UNCERTIFIED: "this link box can't read old records yet",
  RANGE_RESUME_UNCERTIFIED: "this machine model can't be recovered yet",
  SERIAL_UNKNOWN: "the machine's serial number isn't known yet",
  IDENTITY_SNAPSHOT_UNSTABLE: "the machine's people list isn't ready yet",
  WAITING_FOR_ZKT: 'the machine is not responding',
  WAITING_FOR_DEVICE: 'the link box is offline',
  WAITING_FOR_SNAPSHOT: "the machine's people list isn't ready yet",
  COMMAND_ACTIVE: 'another task is running on this device',
  LEASE_ACTIVE: 'temporary admin access is on',
  SOURCE_GENERATION_STALE: 'this device was set up again. Ask IT',
  RECONCILIATION_ACTIVE: 'a recovery is already running for this device',
  RECONCILIATION_HELD: 'a recovery for this device is waiting for you',
  TERMINAL_BINDING_REQUIRED: "the machine isn't confirmed yet",
  HIKVISION_POLICY_REQUIRED: "this machine's settings must be approved first",
}

/** Waits, read after "Will start by itself when". */
export const WAIT_TITLES: Record<string, string> = {
  WAITING_FOR_ZKT: 'the machine responds',
  WAITING_FOR_DEVICE: 'the link box is back online',
  IDENTITY_SNAPSHOT_UNSTABLE: "the machine's people list is ready",
  WAITING_FOR_SNAPSHOT: "the machine's people list is ready",
  COMMAND_ACTIVE: 'another task on this device finishes',
  LEASE_ACTIVE: 'temporary admin access ends',
  WAITING_FOR_SAFE_WINDOW: 'the machine is free',
  WAITING_FOR_CAPACITY: 'the dashboard has room',
  WAITING_FOR_SCAN_SLOT: 'another recovery finishes',
  HISTORY_BACKLOG_BACKPRESSURE: 'the HR system catches up',
}

/** "no attendance machine is linked…" (blockers) or, with `wait`, "the link box is back online". */
export const blockerTitle = (code: string | null | undefined, wait = false): string => {
  const key = (code || '').toUpperCase()
  if (wait) return WAIT_TITLES[key] || 'the machine is free'
  return BLOCKER_TITLES[key] || 'something on this device needs IT'
}

export type VerdictKind = 'checking' | 'ready' | 'waits' | 'blocked' | 'error' | 'off' | 'running'

export interface Verdict { kind: VerdictKind; text: string; tone: Tone; canStart: boolean; showIt: boolean }

const capitalise = (text: string) => text.charAt(0).toUpperCase() + text.slice(1)

/** 1.2 verdict line, from the preflight answer. */
export function verdict(input: {
  enabled: boolean
  loading: boolean
  error: boolean
  started?: boolean
  preflight: { eligible: boolean; ready_now: boolean; hard_blockers: Array<{ code: string }>; waitable_blockers: Array<{ code: string }> } | null
}): Verdict {
  if (!input.enabled) return { kind: 'off', text: RECOVER_PAGE.switchedOff, tone: 'neutral', canStart: false, showIt: false }
  const hard = input.preflight?.hard_blockers || []
  const active = hard.find((item) => item.code === 'RECONCILIATION_ACTIVE' || item.code === 'RECONCILIATION_HELD')
  if (input.started && (active || !input.preflight)) return { kind: 'running', text: 'Running now.', tone: 'good', canStart: false, showIt: true }
  if (!input.preflight) {
    if (input.error) return { kind: 'error', text: "Couldn't check this device.", tone: 'warning', canStart: false, showIt: false }
    return { kind: 'checking', text: 'Checking this device…', tone: 'neutral', canStart: false, showIt: false }
  }
  if (hard.length) {
    if (active) return { kind: 'blocked', text: `Can't start: ${active.code === 'RECONCILIATION_HELD' ? 'a recovery for this device is waiting for you' : 'a recovery is already running for this device'}.`, tone: 'problem', canStart: false, showIt: true }
    return { kind: 'blocked', text: `Can't start: ${blockerTitle(hard[0].code)}.`, tone: 'problem', canStart: false, showIt: false }
  }
  if (input.preflight.ready_now) return { kind: 'ready', text: 'Ready. Starts now.', tone: 'good', canStart: input.preflight.eligible, showIt: false }
  if (input.preflight.eligible) {
    const wait = input.preflight.waitable_blockers[0]
    return { kind: 'waits', text: `Will start by itself when ${blockerTitle(wait?.code, true)}.`, tone: 'warning', canStart: true, showIt: false }
  }
  return { kind: 'blocked', text: "Can't start: something on this device needs IT.", tone: 'problem', canStart: false, showIt: false }
}

export const capitalised = capitalise

// --- Bulk start -----------------------------------------------------------

export const BULK_OUTCOME: Record<string, { label: string; icon: '✓' | '↻' | '✕' }> = {
  CREATED: { label: 'Started', icon: '✓' },
  ALREADY_ACTIVE: { label: 'Already running', icon: '↻' },
  BLOCKED: { label: "Couldn't start", icon: '✕' },
}

/** A bulk start refusal (server sentence, no code) in plain words. */
export function bulkReason(message: string | null | undefined): string {
  const text = (message || '').toLowerCase()
  if (!text) return 'ask IT to look at this device'
  if (/zone lite|firmware|newer is required/.test(text)) return 'the link box needs a software update first'
  if (/duplicate/.test(text)) return 'it is blocked as a duplicate device'
  if (/inactive|disabled/.test(text)) return 'this device is switched off in the dashboard'
  if (/no zkt|not assigned|no terminal/.test(text)) return 'no attendance machine is linked to this device'
  if (/serial/.test(text)) return "the machine's serial number isn't known yet"
  if (/active reconciliation|already/.test(text)) return 'a recovery is already running for this device'
  if (/binding/.test(text)) return "the machine isn't confirmed yet"
  if (/policy|profile/.test(text)) return "approve this machine's settings first"
  return 'ask IT to look at this device'
}

export const bulkSummary = (created: number, already: number, blocked: number) =>
  `${formatCount(created)} started · ${formatCount(already)} already running · ${formatCount(blocked)} couldn't start`

// --- Clear history --------------------------------------------------------

export const CLEAR_HISTORY = {
  label: 'Clear history',
  busy: 'Clearing…',
  helper: 'Stops running recoveries and removes finished ones from this list. Check-ins are not changed.',
  again: 'Clear the rest',
}

export function clearHistoryBanner(result: { cancelled: number; deleted: number; archived: number; busy?: number }, running: number): string {
  let text = `History cleared: ${formatCount(result.cancelled)} stopped, ${formatCount(result.deleted)} removed, ${formatCount(result.archived)} hidden.`
  if (running) text += ` ${formatCount(running)} still running on purpose (catching up on new check-ins, or an open attendance fix).`
  if (result.busy) text += ` ${formatCount(result.busy)} ${result.busy === 1 ? 'was' : 'were'} busy. Press Clear history again to finish them.`
  return text
}

// --- Unreadable records ---------------------------------------------------

export type ProblemFilter = 'time' | 'damaged' | 'unmatched'
export const PROBLEM_DISPOSITION: Record<ProblemFilter, string> = { time: 'INVALID_TIME', damaged: 'MALFORMED', unmatched: 'IDENTITY_UNRESOLVED' }
export const PROBLEM_FILTER_WORDS: Record<ProblemFilter, string> = { time: 'Impossible time', damaged: 'Damaged record', unmatched: 'Not matched to an employee' }

export type ReviewFilter = 'open' | 'reviewed' | 'all'
export const REVIEW_FILTER_WORDS: Record<ReviewFilter, string> = { open: 'Not seen yet', reviewed: 'Seen', all: 'All' }

/** W1.6 problem name for one row, from `disposition` + `source_condition`. */
export function problemName(disposition: string | null | undefined, condition?: string | null): string {
  switch (condition) {
    case 'TERMINAL_NEWEST_RECORD_UNSETTLED': return 'Not a real check-in: the machine was still saving it'
    case 'TERMINAL_NEWEST_RECORD_UNCONFIRMED': return 'Latest check-in not received yet: waiting for the link box to send it again'
    case 'TERMINAL_NEWEST_RECORD_CORRECTED': return 'Time was corrected. HR should check this check-in.'
    case 'TERMINAL_NEWEST_RECORD_OUT_OF_ORDER': return 'Time is out of order, so it was not sent'
    default: break
  }
  if (disposition === 'INVALID_TIME') return 'Impossible time'
  if (disposition === 'MALFORMED') return 'Damaged record'
  if (disposition === 'IDENTITY_UNRESOLVED') return 'Not matched to an employee'
  return 'Record not sent'
}

/** Short problem name for filters and chips. */
export const problemShort = (disposition: string | null | undefined) =>
  disposition === 'INVALID_TIME' ? 'Impossible time' : disposition === 'MALFORMED' ? 'Damaged record' : disposition === 'IDENTITY_UNRESOLVED' ? 'Not matched to an employee' : 'Record not sent'

/** Problem codes people can pick in "More filters" (value = backend error_code). */
export const PROBLEM_CODES: ReadonlyArray<{ code: string; label: string }> = [
  { code: 'IMPLAUSIBLE_TERMINAL_TIME', label: 'Impossible time' },
  { code: 'TERMINAL_NEWEST_RECORD_UNCONFIRMED', label: 'Latest check-in not received yet' },
  { code: 'HISTORICAL_IDENTITY_EVIDENCE_REQUIRED', label: 'Not matched to an employee' },
  { code: 'MALFORMED_RECORD', label: 'Damaged record' },
]

export const problemCodeLabel = (code: string) => PROBLEM_CODES.find((item) => item.code === code)?.label || 'Other problem'

/** W1.7 plain explanation of one record. */
export function recordExplanation(row: { disposition: string; source_condition?: string | null }): { title: string; body: string } {
  switch (row.source_condition) {
    case 'TERMINAL_NEWEST_RECORD_UNSETTLED': return { title: 'Not a real check-in', body: 'The machine was still saving this record when it was read. It is not a check-in, so nothing was sent.' }
    case 'TERMINAL_NEWEST_RECORD_UNCONFIRMED': return { title: 'Latest check-in not received yet', body: 'This looks like the machine’s newest check-in, but the link box hasn’t sent it to us yet. It is sent by itself when it arrives. Marking it as seen will not send it.' }
    case 'TERMINAL_NEWEST_RECORD_CORRECTED': return { title: 'Time was corrected', body: 'A corrected time was made for this record before we knew it was still being saved. HR should check this check-in.' }
    case 'TERMINAL_NEWEST_RECORD_OUT_OF_ORDER': return { title: 'Time is out of order', body: 'Its time does not fit between the check-ins before and after it, so it may belong to someone else. It was not sent.' }
    default: break
  }
  if (row.disposition === 'IDENTITY_UNRESOLVED') return { title: 'Not matched to an employee', body: "We can't tell which employee this is. Open Attendance with the Not matched filter and fix the employee. Marking it as seen will not send it." }
  if (row.disposition === 'MALFORMED') return { title: 'Damaged record', body: 'The machine saved this record in a form we can’t read. Other check-ins are not affected.' }
  return { title: 'Impossible time', body: 'The machine saved this record with a time that can’t be right, so it was not sent. Other check-ins are not affected.' }
}

export const RECORD_SHEET = {
  title: 'Unreadable record',
  notSent: 'Not sent to the HR system (Oracle)',
  seenChanges: 'Marking as seen changes nothing. When every record in the recovery is seen, it finishes by itself.',
  whoLooked: 'Who looked at it',
  nobody: 'Nobody has looked at this yet.',
  note: 'Note (why you are marking this)',
  notePlaceholder: 'At least 10 letters',
  password: 'Your password',
  markSeen: 'Mark as seen',
  saving: 'Saving…',
  next: 'Next record →',
  seen: 'Seen ✓',
  unmatchedFooter: "Marking it as seen won't send it. Fix the employee in Attendance with the Not matched filter.",
  showRaw: 'Show raw record',
  checkingPassword: 'Checking password…',
  hideNow: 'Hide now',
  failed: "That didn't work. Try again.",
  rawShown: 'Raw record shown; this was logged.',
  hiddenIn: (seconds: number) => `Hidden again in ${plural(seconds, 's', 's')}.`,
} as const

/** Toast after Mark as seen. */
export function seenToast(row: { disposition: string; source_condition?: string | null }, left: number | null): string {
  if (row.source_condition === 'TERMINAL_NEWEST_RECORD_UNCONFIRMED' || row.source_condition === 'TERMINAL_NEWEST_RECORD_UNSETTLED') return 'Marked as seen. The machine’s latest record is still checked by itself.'
  if (row.disposition === 'IDENTITY_UNRESOLVED' && row.source_condition !== 'TERMINAL_NEWEST_RECORD_OUT_OF_ORDER') return 'Marked as seen. It stays not matched until the employee is fixed.'
  if (left === null) return 'Marked as seen. The recovery finishes by itself when none are left.'
  return `Marked as seen. ${formatCount(left)} left; the recovery finishes by itself when none are left.`
}

// --- Not in HR system yet -------------------------------------------------

export const HELD_TAB = {
  title: 'Not in HR system yet',
  intro: 'Check-ins that reached the dashboard but are not in the HR system (Oracle) yet, and why.',
  introMore: 'Most of them go through by themselves.',
  national: 'These figures cover all devices.',
  figuresFrom: (time: string) => `Figures from ${time}`,
  loadFailed: "Couldn't load these figures.",
  retry: 'Waiting to be sent again',
  retryText: "The HR system didn't take them yet. They are sent again by themselves.",
  sending: 'Being sent now',
  missing: 'Not lined up for sending yet',
  missingText: 'If this stays above 0 for a day, tell IT.',
  unmatched: 'Not matched to an employee',
  unmatchedLink: 'Send not-matched check-ins automatically',
  setAside: 'Set aside because something looks wrong',
  setAsideLink: 'See unreadable records',
} as const

/** The "Not lined up for sending yet" sentence (was withoutOutboxDetail). */
export function notLinedUpText(delivery?: { missing_outbox?: number; held_without_outbox?: number; review_without_outbox?: number }): string {
  const parts: string[] = [HELD_TAB.missingText]
  if (!delivery || (delivery.held_without_outbox === undefined && delivery.review_without_outbox === undefined)) return HELD_TAB.missingText
  if (delivery.held_without_outbox) parts.push(`${formatCount(delivery.held_without_outbox)} waiting to be matched to an employee`)
  if (delivery.review_without_outbox) parts.push(`${formatCount(delivery.review_without_outbox)} set aside because something looks wrong`)
  return parts.join(' · ')
}

export const setAsideText = (invalidTime: number) =>
  invalidTime ? `Including ${formatCount(invalidTime)} with an impossible time.` : 'Records that were not sent because something looks wrong.'

export const FIX_TIMES = {
  title: 'Fix impossible times',
  intro: 'Only records whose person is known can have their time fixed. The original record is kept.',
  off: 'Fixing impossible times is switched off for now. Ask IT to switch it on.',
  toFix: (count: number) => `${formatCount(count)} to fix`,
  zkt: 'ZKTeco machines',
  hik: 'Hikvision machines',
  show: 'Show the fixes',
  apply: (count: number) => `Fix ${plural(count, 'time')}`,
  timeLabel: (record: string) => `Corrected time for record ${record} (Pakistan time)`,
  note: 'Note (why you are doing this)',
  password: 'Your password',
  typePhrase: (phrase: string) => `Type ${phrase} to confirm`,
  none: 'No records with an impossible time are waiting to be fixed.',
  batch: 'Fixing times',
  progress: (done: number, failed: number, skipped: number, total: number) => `${formatCount(done)} done · ${formatCount(failed)} failed · ${formatCount(skipped)} skipped · of ${formatCount(total)}`,
  queued: 'The fixes are lined up. The original records are kept.',
  controlDone: { pause: 'Paused.', resume: 'Resumed.', cancel: 'Stopped.', retry: 'Trying the failed ones again.' } as Record<string, string>,
  notAllowed: "This record can't have its time fixed: the person isn't known.",
  allowed: 'The person is known; its time can be fixed.',
} as const

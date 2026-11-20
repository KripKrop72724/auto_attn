// Recover attendance page words (08-advanced ADV-2, new test 1): every code the
// backend can send for a recovery has plain words, unknown codes fall back
// safely, and the safety rules of the card (no Try again where it is pointless)
// are pinned. Code lists are copied from be/reconciliation.py,
// be/hikvision_reconciliation.py and be/source_exceptions.py.
import { describe, expect, it } from 'vitest'
import codes from './codes.generated.json'
import { isPlainText } from './glossary'
import {
  BLOCKER_TITLES,
  blockerTitle,
  bulkReason,
  clearHistoryBanner,
  jobProblem,
  knowsJobProblem,
  notLinedUpText,
  problemName,
  recoveryCard,
  recoveryEta,
  readCountsLine,
  statusSegments,
  verdict,
  waitReasonText,
} from './recover'

const domains = (codes as { domains: Record<string, { backend: string[] }> }).domains

const WAIT_REASONS = ['WAITING_FOR_SAFE_WINDOW', 'WAITING_FOR_SCAN_SLOT', 'WAITING_FOR_CAPACITY', 'WAITING_FOR_DEVICE',
  'WAITING_FOR_ZKT', 'WAITING_FOR_SNAPSHOT', 'WAITING_FOR_IDENTITY', 'WAITING_FOR_PROTOCOL', 'WAITING_FOR_ORACLE_CAPACITY',
  'HISTORY_BACKLOG_BACKPRESSURE', 'HISTORY_BACKLOG_CREDIT_EXHAUSTED', 'COMMAND_ACTIVE', 'LEASE_ACTIVE', 'TRANSIENT_STEP_RETRY']

const PROBLEM_CODES = ['SOURCE_QUARANTINE_REQUIRES_REVIEW', 'SOURCE_IDENTITY_EVIDENCE_REQUIRED', 'SOURCE_INTERPRETATION_REQUIRED',
  'SOURCE_NEWEST_PUNCH_UNRECEIVED', 'SOURCE_EXCEPTION_SCOPE_MISMATCH', 'ORACLE_TERMINAL_OUTCOME_REQUIRES_REVIEW',
  'SOURCE_COVERAGE_CANONICAL_EVIDENCE_MISSING', 'SOURCE_COVERAGE_CANONICAL_GAP', 'SOURCE_COVERAGE_CHAIN_DIVERGED',
  'SOURCE_COVERAGE_CHAIN_ENDPOINT_DIVERGED', 'SOURCE_COVERAGE_CHAIN_GAP', 'SOURCE_COVERAGE_GENERATION_INVALIDATED',
  'SOURCE_COVERAGE_GENERATION_SUPERSEDED', 'SOURCE_COVERAGE_INVALIDATED', 'SOURCE_COVERAGE_PROOF_LIMIT', 'SOURCE_COVERAGE_REVIEW',
  'SOURCE_GENERATION_LINEAGE_DIVERGED', 'SOURCE_GENERATION_KEY_DIVERGED', 'SOURCE_GENERATION_JOB_EVIDENCE_INVALID',
  'SOURCE_GENERATION_STALE', 'SOURCE_EPOCH_RECOVERY_LIMIT', 'SOURCE_DIVERGENCE_UNSTABLE', 'SELF_HEALING_RECOVERY_DISABLED']

const BLOCKERS = ['FEATURE_DISABLED', 'CONNECTOR_INACTIVE', 'DUPLICATE_SERIAL_QUARANTINE', 'NO_ZKT', 'FIRMWARE_UNSUPPORTED',
  'HISTORY_STREAM_UNCERTIFIED', 'RANGE_RESUME_UNCERTIFIED', 'SERIAL_UNKNOWN', 'IDENTITY_SNAPSHOT_UNSTABLE', 'WAITING_FOR_ZKT',
  'WAITING_FOR_DEVICE', 'COMMAND_ACTIVE', 'LEASE_ACTIVE', 'SOURCE_GENERATION_STALE', 'RECONCILIATION_ACTIVE',
  'RECONCILIATION_HELD', 'TERMINAL_BINDING_REQUIRED', 'HIKVISION_POLICY_REQUIRED']

const OPERATOR_STATES = [...domains.recoverStep.backend, 'FOLLOWING_TERMINAL_TAIL', 'TAIL_RECOVERY_ENDED']

const plain = (text: string) => expect(isPlainText(text), text).toBe(true)

describe('recovery card words', () => {
  it('has a known chip for every job status', () => {
    for (const status of domains.recoverJob.backend) {
      const words = recoveryCard({ status })
      expect(words.chip, status).not.toBe('Needs IT')
      plain(words.chip)
      plain(words.sentence)
    }
  })

  it('has a known chip for every operator state', () => {
    for (const operator_state of OPERATOR_STATES) {
      const status = operator_state === 'PAUSED' ? 'PAUSED' : operator_state === 'CANCELLED' ? 'CANCELLED'
        : operator_state.startsWith('COMPLETED') || operator_state === 'TAIL_RECOVERY_ENDED' ? 'COMPLETED'
          : operator_state === 'REVIEW_REQUIRED' ? 'NEEDS_ATTENTION' : 'RUNNING'
      const words = recoveryCard({ status, operator_state })
      expect(words.chip, operator_state).not.toBe('Needs IT')
      plain(words.chip)
      plain(words.sentence)
    }
  })

  it('shows a waiting recovery as Waiting to start, whatever the server message says', () => {
    const words = recoveryCard({ status: 'QUEUED', operator_state: 'CAPTURING_SOURCE' }, { queuePosition: 2 })
    expect(words.chip).toBe('Waiting to start')
    expect(words.stage).toBe('waiting')
    expect(words.sentence).toBe('Number 2 in line.')
    expect(recoveryCard({ status: 'QUEUED', wait_reason: 'WAITING_FOR_SCAN_SLOT' }, { running: 6 }).sentence)
      .toBe('Waiting for its turn: 6 recoveries are already running.')
  })

  it('never offers Try again where it cannot help', () => {
    for (const state of ['REVIEW_REQUIRED', 'IDENTITY_EVIDENCE_REQUIRED', 'INTERPRETATION_REQUIRED', 'NEWEST_PUNCH_UNRECEIVED', 'SCOPE_MISMATCH']) {
      const words = recoveryCard({ status: 'NEEDS_ATTENTION', source_exception_assurance: { state, open: 2 } })
      expect(words.action, state).not.toBe('retry')
      expect(words.noRetry, state).toBe(true)
    }
    const coverage = recoveryCard({ status: 'NEEDS_ATTENTION', error_code: 'SOURCE_COVERAGE_CHAIN_GAP' })
    expect(coverage.action).toBe('cancel')
    expect(coverage.actionLabel).toBe('Stop')
    expect(coverage.noRetry).toBe(true)
    expect(recoveryCard({ status: 'NEEDS_ATTENTION', source_exception_assurance: { state: 'REVIEW_REQUIRED', open: 3 } }).actionLabel).toBe('Look at 3 records')
  })

  it('reads a finished recovery as Done with its own note', () => {
    expect(recoveryCard({ status: 'COMPLETED', operator_state: 'COMPLETED_WITH_IDENTITY_HOLDS' }).chip).toBe('Done, some not matched')
    expect(recoveryCard({ status: 'COMPLETED', operator_state: 'COMPLETED' }).sentence).toBe('Every check-in on the machine reached the HR system.')
    expect(recoveryCard({ status: 'COMPLETED' }).stage).toBe('done')
  })

  it('gives every problem code plain words and unknown codes the safe fallback', () => {
    for (const code of PROBLEM_CODES) {
      expect(knowsJobProblem(code), code).toBe(true)
      plain(jobProblem(code, 2).sentence)
    }
    const unknown = jobProblem('SOMETHING_NEW_FROM_THE_SERVER')
    expect(unknown.sentence).toBe('Something unusual happened. IT can see the details below.')
    expect(unknown.action).toBe('details')
    expect(recoveryCard({ status: 'RUNNING', operator_state: 'BRAND_NEW_STATE' }).chip).toBe('Reading the machine')
  })

  it('has words for every wait reason', () => {
    for (const code of WAIT_REASONS) {
      const text = waitReasonText(code, 3)
      expect(text, code).toBeTruthy()
      plain(text as string)
    }
    expect(waitReasonText('UNKNOWN_WAIT')).toBeNull()
  })

  it('names the time left without a confidence word', () => {
    expect(recoveryEta({ status: 'RUNNING', eta: { high_seconds: 300 } })).toBe('About 5 min left')
    expect(recoveryEta({ status: 'PAUSED', eta: {} })).toBe('Paused')
    expect(recoveryEta({ status: 'RUNNING', eta: { unavailable_reason: 'ORACLE_VERIFYING' } })).toBe('Checking with the HR system')
    expect(recoveryEta({ status: 'RUNNING', eta: { unavailable_reason: 'WAITING_FOR_DEVICE' } })).toBe('Waiting for the link box')
    expect(recoveryEta({ status: 'NEEDS_ATTENTION', eta: { unavailable_reason: 'SOURCE_QUARANTINE_REQUIRES_REVIEW' } })).toBe('Waiting for you')
    expect(recoveryEta({ status: 'RUNNING', eta: { unavailable_reason: 'COLLECTING_THROUGHPUT' } })).toBe('Working out time left')
  })

  it('leaves zero parts out of the counts line', () => {
    expect(readCountsLine({ scanned: 11843, add_durable: 11843, blocked_identity: 0, quarantined: 1 }))
      .toBe('11,843 read · 11,843 saved · 1 not sent (unreadable)')
  })
})

describe('page words', () => {
  it('writes the status line with real plurals', () => {
    expect(statusSegments({ attention: 1, active: 2, queued_waiting: 0, paused: 1 }).map((segment) => segment.text))
      .toEqual(['1 recovery is waiting for you', '2 running', '0 waiting to start', '1 paused'])
    expect(statusSegments({ attention: 3, active: 0, queued_waiting: 0, paused: 0 })[0].text).toBe('3 recoveries are waiting for you')
  })

  it('has a plain title for every start blocker', () => {
    for (const code of BLOCKERS) {
      expect(BLOCKER_TITLES[code], code).toBeTruthy()
      plain(blockerTitle(code))
    }
    expect(blockerTitle('BRAND_NEW')).toBe('something on this device needs IT')
  })

  it('turns the preflight into one verdict line', () => {
    const base = { enabled: true, loading: false, error: false }
    expect(verdict({ ...base, preflight: null }).text).toBe('Checking this device…')
    expect(verdict({ ...base, preflight: { eligible: true, ready_now: true, hard_blockers: [], waitable_blockers: [] } }).text).toBe('Ready. Starts now.')
    expect(verdict({ ...base, preflight: { eligible: true, ready_now: false, hard_blockers: [], waitable_blockers: [{ code: 'WAITING_FOR_DEVICE' }] } }).text)
      .toBe('Will start by itself when the link box is back online.')
    const active = verdict({ ...base, preflight: { eligible: false, ready_now: false, hard_blockers: [{ code: 'RECONCILIATION_ACTIVE' }], waitable_blockers: [] } })
    expect(active.text).toBe("Can't start: a recovery is already running for this device.")
    expect(active.showIt).toBe(true)
    // Right after a start the answer is "Running now", never the red blocker.
    expect(verdict({ ...base, started: true, preflight: { eligible: false, ready_now: false, hard_blockers: [{ code: 'RECONCILIATION_ACTIVE' }], waitable_blockers: [] } }).kind).toBe('running')
    expect(verdict({ ...base, error: true, preflight: null }).text).toBe("Couldn't check this device.")
    expect(verdict({ ...base, enabled: false, preflight: null }).text).toBe('Recover attendance is switched off for now. Ask IT to switch it on.')
  })

  it('names every record problem', () => {
    for (const disposition of ['INVALID_TIME', 'MALFORMED', 'IDENTITY_UNRESOLVED']) {
      expect(problemName(disposition)).not.toBe('Record not sent')
    }
    for (const condition of ['TERMINAL_NEWEST_RECORD_UNSETTLED', 'TERMINAL_NEWEST_RECORD_UNCONFIRMED', 'TERMINAL_NEWEST_RECORD_CORRECTED', 'TERMINAL_NEWEST_RECORD_OUT_OF_ORDER']) {
      const name = problemName('IDENTITY_UNRESOLVED', condition)
      expect(name).not.toBe('Not matched to an employee')
      plain(name)
    }
  })

  it('turns bulk refusals and the clear-history result into plain sentences', () => {
    expect(bulkReason('Zone Lite 2.3.0 or newer is required.')).toBe('the link box needs a software update first')
    expect(bulkReason('Something new')).toBe('ask IT to look at this device')
    const banner = clearHistoryBanner({ cancelled: 1, deleted: 3, archived: 2, busy: 2 }, 1)
    expect(banner).toBe('History cleared: 1 stopped, 3 removed, 2 hidden. 1 still running on purpose (catching up on new check-ins, or an open attendance fix). 2 were busy. Press Clear history again to finish them.')
    plain(banner)
    plain(notLinedUpText({ missing_outbox: 4, held_without_outbox: 2, review_without_outbox: 1 }))
  })
})

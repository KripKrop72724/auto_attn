// The "every code has words" guard (02-words WORDS-2 / WORDS-3 test 5, owner
// decision D-22). codes.generated.json is written by
// `python scripts/export_word_codes.py` from the backend and link box sources.
//
// - A code the dashboard or backend writes ("backend") must have words: this
//   test fails and names it.
// - A code only the link box software sends ("firmware") may use its group's
//   fallback sentence; it is listed as a follow-up (console warning), not a failure.
// - Every dictionary value passes the plain-words rules.

import { describe, expect, it } from 'vitest'
import codes from './codes.generated.json'
import { ACTIVITY_TAB, LEVEL_FALLBACK, LOG_WORDS, logWords, describeLog } from './activity'
import { ATTENDANCE_TOOL_WORDS, AREA_WAIT_REASONS, DIRECT_REASONS, FORCE_REASONS, FORCE_RUN_ERROR, LOCK_REASONS, REASON_FALLBACKS, RECOVERY_ROW_REASONS, SKIP_REASONS } from './attendanceReasons'
import { OFFLINE_REASONS } from './deviceStatus'
import { ERROR_FALLBACK, ERROR_WORDS, NETWORK_ERROR } from './errors'
import { BUSY, findPlainWordProblems } from './glossary'
import { OFFLINE_RULE_SENTENCE, WHY_TITLES } from './health'
import { ACTIVITY_WORDS, CONNECTION_REASONS, HIK_POLL_WORDS, MACHINE_LINK_REASONS, MACHINE_LINK_REASON_FALLBACK } from './machine'
import { CAPABILITY_OFF, PEOPLE_ERRORS, WRITES_OFF, WRITES_OFF_FALLBACK, writesOff } from './people'
import { LINK_BOX_POWER_CYCLE, PROBLEM_INFO, REFUSAL_FALLBACK, REFUSAL_WORDS, RESOLUTION_WORDS, problemFallback, GROUP_LABELS } from './problems'
import { COMM_KEY_BLOCKS, COMM_KEY_ERRORS, COMM_KEY_ERROR_FALLBACK, REQUEST_FAILURES, REQUEST_FAILURE_FALLBACK, commKeyError, requestFailure, REQUEST_KEPT } from './requests'
import { SETUP_ERRORS, SETUP_MESSAGES, SETUP_SWITCHED_OFF } from './setup'
import { UPDATE_CODES, UPDATE_CODE_FALLBACK, plainUpdateCode } from './software'
import { STATE_WORDS, UNKNOWN_STATUS, plainStatus } from './states'
import type { Domain, Tone } from './types'

type GeneratedDomain = { description: string; backend: string[]; firmware: string[] }
const DOMAINS = (codes as { domains: Record<string, GeneratedDomain> }).domains

const status = (domain: Domain) => (code: string) => !plainStatus(domain, code).unknown
const inMap = (map: Record<string, unknown>) => (code: string) => code in map

/** How each generated domain is looked up. A new generator domain must be added here. */
const HAS_WORDS: Record<string, (code: string) => boolean> = {
  device: status('device'),
  tier: status('tier'),
  machineLink: status('machineLink'),
  machineLinkReason: (code) => code in MACHINE_LINK_REASONS || status('machineRaw')(code),
  machineRaw: status('machineRaw'),
  activity: inMap(ACTIVITY_WORDS),
  problem: inMap(PROBLEM_INFO),
  severity: status('severity'),
  refusal: inMap(REFUSAL_WORDS),
  delivery: status('delivery'),
  identity: status('identity'),
  requestType: status('requestType'),
  request: status('request'),
  requestFailure: (code) => !requestFailure(code).unknown,
  peopleRemoval: status('peopleRemoval'),
  writesOff: (code) => !writesOff(code).unknown,
  lease: status('lease'),
  commKey: status('commKey'),
  commKeyError: (code) => !commKeyError(code).unknown,
  softwareDevice: status('softwareDevice'),
  update: status('update'),
  updateEvent: (code) => status('updateEvent')(code) || status('update')(code),
  rollout: status('rollout'),
  updateCode: (code) => !plainUpdateCode(code).unknown,
  recoverJob: status('recoverJob'),
  recoverPhase: status('recoverPhase'),
  recoverStep: status('recoverStep'),
  forceReason: inMap(FORCE_REASONS),
  directReason: inMap(DIRECT_REASONS),
  error: inMap(ERROR_WORDS),
  log: (code) => logWords(code) !== null,
}

/** Domains whose words step has not landed yet. Must stay empty (02-words test 5). */
const PENDING_DOMAINS: string[] = []

// Appendix labels such as "Checking with the HR system" are 5 words; the plan's 1-3 word rule is for
// fixed UI labels, so chips allow 5 and never truncate (FND-5).
const CHIP_WORD_LIMIT = 5

const FIX_HINT = 'Add words for these codes in apps/add_frontend/src/words/ (then run `python scripts/export_word_codes.py`).'

describe('codes.generated.json', () => {
  it('has a sane number of codes, so a broken pattern cannot pass with an empty list', () => {
    const count = (name: string) => DOMAINS[name].backend.length + DOMAINS[name].firmware.length
    expect(count('log')).toBeGreaterThanOrEqual(180)
    expect(count('problem')).toBeGreaterThanOrEqual(47)
    expect(count('error')).toBeGreaterThanOrEqual(50)
    expect(count('updateCode')).toBeGreaterThanOrEqual(90)
  })

  it('maps every generated domain to a dictionary', () => {
    expect(Object.keys(DOMAINS).filter((name) => !HAS_WORDS[name])).toEqual([])
    expect(PENDING_DOMAINS).toEqual([])
  })

  for (const [name, domain] of Object.entries(DOMAINS)) {
    it(`${name}: every dashboard and backend code has words`, () => {
      const missing = domain.backend.filter((code) => !HAS_WORDS[name](code))
      expect(missing, `${name} (${domain.description}): ${FIX_HINT}`).toEqual([])
    })
  }

  it('lists firmware-only codes without words as a follow-up (D-22: group fallback, no failure)', () => {
    const followUp = Object.entries(DOMAINS).flatMap(([name, domain]) =>
      domain.firmware.filter((code) => !HAS_WORDS[name](code)).map((code) => `${name}.${code}`))
    if (followUp.length) console.warn(`Link box codes using their group's fallback words (add words in a follow-up): ${followUp.join(', ')}`)
    // Each still gets a safe sentence that never prints the code.
    for (const entry of followUp) {
      const [name, code] = entry.split('.')
      if (name === 'log') expect(describeLog({ code, level: 'ERROR' }).sentence).not.toContain(code)
    }
  })
})

// ---------------------------------------------------------- plain words rules

const values: Array<[string, string]> = []
const add = (where: string, text: string | undefined) => {
  if (text) values.push([where, text.replace(/\{[a-z]+\}/g, 'Sara')])
}

for (const [domain, words] of Object.entries(STATE_WORDS)) {
  for (const [code, word] of Object.entries(words)) {
    add(`${domain}.${code}.label`, word.label)
    add(`${domain}.${code}.explain`, word.explain)
    add(`${domain}.${code}.next`, word.next)
  }
}
add('UNKNOWN_STATUS', UNKNOWN_STATUS.label)
add('UNKNOWN_STATUS', UNKNOWN_STATUS.explain)
for (const [code, sentence] of Object.entries(MACHINE_LINK_REASONS)) add(`machineLinkReason.${code}`, sentence)
add('machineLinkReason.fallback', MACHINE_LINK_REASON_FALLBACK)
for (const [text, sentence] of Object.entries(CONNECTION_REASONS)) add(`connection.${text}`, sentence)
for (const [code, sentence] of Object.entries(ACTIVITY_WORDS)) add(`activity.${code}`, sentence)
for (const [code, sentence] of Object.entries(HIK_POLL_WORDS)) add(`hikPoll.${code}`, sentence)
for (const [code, info] of Object.entries(PROBLEM_INFO)) {
  add(`problem.${code}.title`, info.title)
  add(`problem.${code}.detail`, info.detail)
  add(`problem.${code}.next`, info.next)
  add(`problem.${code}.closesWhen`, info.closesWhen)
}
for (const group of Object.keys(GROUP_LABELS) as Array<keyof typeof GROUP_LABELS>) {
  const fallback = problemFallback(group)
  for (const text of Object.values(fallback)) add(`problemFallback.${group}`, text)
}
add('LINK_BOX_POWER_CYCLE', LINK_BOX_POWER_CYCLE)
for (const [code, sentence] of Object.entries(RESOLUTION_WORDS)) add(`resolution.${code}`, sentence)
for (const [code, sentence] of Object.entries(REFUSAL_WORDS)) add(`refusal.${code}`, sentence)
add('refusal.fallback', REFUSAL_FALLBACK)
for (const [code, sentence] of Object.entries(REQUEST_FAILURES)) add(`requestFailure.${code}`, sentence)
add('requestFailure.fallback', REQUEST_FAILURE_FALLBACK)
for (const [code, sentence] of Object.entries(COMM_KEY_ERRORS)) add(`commKeyError.${code}`, sentence)
add('commKeyError.fallback', COMM_KEY_ERROR_FALLBACK)
for (const [code, sentence] of Object.entries(COMM_KEY_BLOCKS)) add(`commKeyBlock.${code}`, sentence)
add('REQUEST_KEPT', REQUEST_KEPT)
for (const [code, sentence] of Object.entries(ERROR_WORDS)) add(`error.${code}`, sentence)
add('error.fallback', ERROR_FALLBACK)
add('error.network', NETWORK_ERROR)
for (const [name, map] of Object.entries({ FORCE_REASONS, DIRECT_REASONS, SKIP_REASONS, RECOVERY_ROW_REASONS, AREA_WAIT_REASONS, LOCK_REASONS, REASON_FALLBACKS })) {
  for (const [code, sentence] of Object.entries(map)) add(`${name}.${code}`, sentence)
}
add('FORCE_RUN_ERROR', FORCE_RUN_ERROR)
for (const [key, value] of Object.entries(ATTENDANCE_TOOL_WORDS)) add(`attendanceTool.${key}`, typeof value === 'function' ? value('Sara') : value)
for (const [code, words] of Object.entries(UPDATE_CODES)) {
  add(`updateCode.${code}`, words.sentence)
  add(`updateCode.${code}.next`, words.next)
}
add('updateCode.fallback', UPDATE_CODE_FALLBACK)
for (const code of [...DOMAINS.updateCode.backend, ...DOMAINS.updateCode.firmware]) add(`updateCode.${code}`, plainUpdateCode(code).sentence)
for (const [code, words] of Object.entries(LOG_WORDS)) {
  add(`log.${code}`, words.sentence)
  add(`log.${code}.next`, words.next)
  for (const [level, variant] of Object.entries(words.byLevel ?? {})) {
    add(`log.${code}.${level}`, variant?.sentence)
    add(`log.${code}.${level}.next`, variant?.next)
  }
}
for (const [level, words] of Object.entries(LEVEL_FALLBACK)) {
  add(`logFallback.${level}`, words.sentence)
  add(`logFallback.${level}.next`, words.next)
}
for (const [key, value] of Object.entries(ACTIVITY_TAB)) add(`activityTab.${key}`, typeof value === 'function' ? (value as (n: number | string) => string)(3) : value)
for (const [code, reason] of Object.entries(OFFLINE_REASONS)) {
  add(`offline.${code}.heading`, reason.heading)
  add(`offline.${code}.sentence`, reason.sentence)
}
for (const [state, title] of Object.entries(WHY_TITLES)) add(`why.${state}`, title)
add('OFFLINE_RULE_SENTENCE', OFFLINE_RULE_SENTENCE)
for (const [code, sentence] of Object.entries(WRITES_OFF)) add(`writesOff.${code}`, sentence)
add('writesOff.fallback', WRITES_OFF_FALLBACK)
for (const [key, sentence] of Object.entries(CAPABILITY_OFF)) add(`capabilityOff.${key}`, sentence)
for (const entry of PEOPLE_ERRORS) add(`peopleError.${entry.match}`, entry.sentence)
for (const entry of SETUP_MESSAGES) add(`setupMessage.${String(entry.match)}`, entry.sentence)
for (const [code, sentence] of Object.entries(SETUP_ERRORS)) add(`setupError.${code}`, sentence)
for (const [key, sentence] of Object.entries(SETUP_SWITCHED_OFF)) add(`setupOff.${key}`, sentence)
for (const [key, label] of Object.entries(BUSY)) add(`busy.${key}`, label)

describe('dictionary values', () => {
  it('collects a few thousand strings to check', () => {
    expect(values.length).toBeGreaterThan(900)
  })

  it('every label, sentence and fallback passes the plain-words rules', () => {
    const failures = values
      .map(([where, text]) => ({ where, text, problems: findPlainWordProblems(text) }))
      .filter((entry) => entry.problems.length)
      .map((entry) => `${entry.where}: "${entry.text}" → ${entry.problems.map((p) => p.match).join(', ')}`)
    expect(failures).toEqual([])
  })

  it('every sentence ends with a full stop, a question mark or an ellipsis; labels never do', () => {
    const sentenceKeys = values.filter(([where]) => /explain$|\.next$|sentence$|closesWhen$|detail$/.test(where))
    expect(sentenceKeys.filter(([, text]) => !/[.?…)"]$/.test(text)).map(([where, text]) => `${where}: ${text}`)).toEqual([])
  })

  it('tones are one of four', () => {
    const tones: Tone[] = ['good', 'neutral', 'warning', 'problem']
    for (const words of Object.values(STATE_WORDS)) {
      for (const word of Object.values(words)) expect(tones).toContain(word.tone)
    }
  })

  it('chip labels are short: at most 5 words in chip domains, at most 9 anywhere', () => {
    const chipDomains: Domain[] = ['device', 'tier', 'machineLink', 'severity', 'alertState', 'delivery', 'identity', 'clock', 'request', 'lease', 'commKey', 'softwareDevice', 'release', 'update', 'rollout', 'recoverJob', 'recoveryJob']
    const words = (text: string) => text.split(/\s+/).filter((part) => /\w/.test(part)).length
    const long = chipDomains.flatMap((domain) => Object.entries(STATE_WORDS[domain])
      .filter(([, word]) => words(word.label) > CHIP_WORD_LIMIT)
      .map(([code, word]) => `${domain}.${code}: "${word.label}"`))
    expect(long).toEqual([])
    const veryLong = Object.entries(STATE_WORDS).flatMap(([domain, map]) => Object.entries(map)
      .filter(([, word]) => words(word.label) > 9)
      .map(([code, word]) => `${domain}.${code}: "${word.label}"`))
    expect(veryLong).toEqual([])
  })

  it('fallbacks never contain an underscore or a code', () => {
    const fallbacks = [UNKNOWN_STATUS.label, UNKNOWN_STATUS.explain, ERROR_FALLBACK, REQUEST_FAILURE_FALLBACK, COMM_KEY_ERROR_FALLBACK, UPDATE_CODE_FALLBACK, MACHINE_LINK_REASON_FALLBACK, WRITES_OFF_FALLBACK, REFUSAL_FALLBACK, ...Object.values(REASON_FALLBACKS), ...Object.values(LEVEL_FALLBACK).map((f) => f.sentence)]
    for (const text of fallbacks) expect(text).not.toMatch(/_|[A-Z]{3,}/)
  })
})

describe('plainStatus', () => {
  it('returns the domain words, with the code kept for data-status-code', () => {
    expect(plainStatus('request', 'WAITING_FOR_ZKT')).toMatchObject({ label: 'Waiting for the machine', tone: 'warning', code: 'WAITING_FOR_ZKT', unknown: false })
    expect(plainStatus('delivery', 'PENDING').label).toBe('Waiting to send')
    expect(plainStatus('update', 'PENDING').label).toBe('Waiting its turn')
    expect(plainStatus('personChange', 'PENDING').label).toBe('Change in progress')
  })

  it('uses the dictionary tone, never a guess from words inside the code', () => {
    expect(plainStatus('delivery', 'ACKED_FIRMWARE').tone).toBe('neutral')
    expect(plainStatus('delivery', 'ACKED').tone).toBe('good')
    expect(plainStatus('delivery', 'FAILED_RETRYABLE').tone).toBe('warning')
    expect(plainStatus('update', 'ROLLED_BACK').tone).toBe('problem')
    expect(plainStatus('testSchedule', 'DEFERRED_OFFLINE').tone).toBe('neutral')
    expect(plainStatus('updateWait', 'NATIONWIDE_TARGET_OFFLINE').tone).toBe('neutral')
    expect(plainStatus('request', 'ACKNOWLEDGED')).toMatchObject({ label: 'The link box received it', tone: 'neutral' })
  })

  it('keeps today’s device tones by domain (moved from status.test.ts)', () => {
    expect(plainStatus('device', 'ONLINE_WITH_WARNINGS')).toMatchObject({ label: 'Working', tone: 'good' })
    expect(plainStatus('device', 'DEGRADED')).toMatchObject({ label: 'Needs attention', tone: 'warning' })
    expect(plainStatus('device', 'QUARANTINED_DUPLICATE_SERIAL')).toMatchObject({ label: 'Blocked: duplicate device', tone: 'problem' })
  })

  it('says "Other status" for an unknown code and never prints it', () => {
    const word = plainStatus('device', 'SOMETHING_NEW_FROM_THE_BACKEND')
    expect(word).toMatchObject({ label: 'Other status', tone: 'neutral', unknown: true, code: 'SOMETHING_NEW_FROM_THE_BACKEND' })
    expect(word.label + word.explain).not.toContain('SOMETHING')
    expect(plainStatus('device', null).unknown).toBe(true)
  })

  it('reads the Recover ETA labels the page sends today', () => {
    expect(plainStatus('recoverEta', 'Oracle verifying').label).toBe('Checking with the HR system')
  })
})

describe('code lookups', () => {
  it('update codes: waits read as waits, failures say what to do', () => {
    expect(plainUpdateCode('WAITING_FOR_RUNTIME_HEALTH')).toMatchObject({ tone: 'wait', where: 'main' })
    expect(plainUpdateCode('DOWNLOAD_INCOMPLETE')).toMatchObject({ tone: 'failure', sentence: 'The download stopped before it finished.' })
    expect(plainUpdateCode('BOOT_LOCAL_MARK_VALID_FAILED').tone).toBe('failure')
    expect(plainUpdateCode('JOURNAL_READER_PROOF_MISSING')).toMatchObject({ tone: 'hold', where: 'technical', unknown: false })
    expect(plainUpdateCode('SOMETHING_NEW').tone).toBe('wait')
    expect(plainUpdateCode('SOMETHING_NEW', 'FAILED')).toMatchObject({ tone: 'failure', unknown: true })
  })

  it('request failures fall back to a plain sentence', () => {
    expect(requestFailure('COMMAND_EXECUTOR_BUSY').sentence).toBe('The link box is busy; try again in a minute.')
    expect(requestFailure('COMM_KEY_PERSIST_FAILED').unknown).toBe(false)
    expect(requestFailure('NEW_THING')).toEqual({ sentence: REQUEST_FAILURE_FALLBACK, unknown: true })
  })
})

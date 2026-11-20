// Glossary and writing rules as code (02-words WORDS-1, plan Appendix A).
//
// BANNED_WORDS is the single list the page guards (words/testing.ts,
// src/test/plainWords.ts, tests/e2e/plainWords.ts) and the backend word tests
// read. Keep one entry per line: the backend test reads this file with a regex.

/** One word per thing, the word people already use (glossary, final). */
export const GLOSSARY: ReadonlyArray<{ today: string; plain: string }> = [
  { today: 'Fleet', plain: 'Devices' },
  { today: 'connector, Zone Lite, ESP, ESP32', plain: 'link box' },
  { today: 'terminal, ZKT, ZKTeco, Hikvision terminal', plain: 'machine (attendance machine)' },
  { today: 'device (machine and link box at one place)', plain: 'device, named by its place' },
  { today: 'Oracle, ORDS, Oracle delivery', plain: 'HR system (Oracle) on first use per screen, then HR system' },
  { today: 'punch, attendance event', plain: 'check-in (attendance record when counting all kinds)' },
  { today: 'ONLINE, ONLINE_WITH_WARNINGS', plain: 'Working (with a small note)' },
  { today: 'DEGRADED, FLAPPING', plain: 'Needs attention' },
  { today: 'machine not responding, no machine linked', plain: 'Offline, as on the public status page (D-1)' },
  { today: 'OFFLINE / ONBOARDING / spare / QUARANTINED_DUPLICATE_SERIAL', plain: 'Offline / Setting up / Spare (not in use) / Blocked: duplicate device' },
  { today: 'Alerts; Acknowledge / Resolve', plain: 'Problems; Mark as seen / Mark as fixed' },
  { today: 'Users (on the machine)', plain: 'People' },
  { today: "user number on the machine", plain: 'Employee ID (D-24)' },
  { today: "HR's own employee number", plain: "Employee ID in HR's records" },
  { today: 'enrollment lease', plain: 'temporary admin access' },
  { today: 'Reconciliation as a page or a person’s action', plain: 'Recover attendance' },
  { today: 'reconcile, light reconcile, truth, append-tail, audit (automatic)', plain: 'record check (quick check, full check, older-records check, newest-records check)' },
  { today: 'source exception / divergence / identity blocked / quarantined', plain: "unreadable record / records don't match / not matched to an employee / set aside for checking" },
  { today: 'Firmware, OTA, release, campaign, deployment', plain: 'software, update, software version, update rollout, update to one device' },
  { today: 'HIL', plain: 'test devices only' },
  { today: 'Live transport / realtime / Cached data', plain: 'Live / Reconnecting… / Offline · showing last saved data' },
  { today: 'PKT', plain: 'Pakistan time' },
  { today: 'ADD as the one doing something', plain: 'the dashboard' },
  { today: 'COMM Key', plain: 'machine password (COMM Key) on first use per screen, then machine password' },
  { today: 'NVS, SPIFFS, flash, storage, journal, outbox, durable queue', plain: 'link box memory' },
  { today: 'identity catalog, user snapshot, user table, roster', plain: 'list of people' },
  { today: 'boot, reboot', plain: 'restart (of the link box)' },
  { today: 'heartbeat, telemetry, last contact', plain: 'status report; last heard from {time}' },
  { today: 'severity CRITICAL / HIGH / WARNING, MEDIUM, INFO', plain: 'Urgent / Important / Low' },
  { today: 'step-up, administrator password confirmation', plain: 'Your password (helper: To confirm it’s you.)' },
  { today: 'contact person', plain: 'IT (D-26)' },
  { today: 'serial, MAC, IP, boot id, digest, hash, cursor, ordinal, epoch', plain: '(not translated) Technical details only' },
]

/** Case-insensitive whole words never shown outside Technical details. */
export const BANNED_WORDS: readonly string[] = [
  'esp',
  'esp32',
  'zone lite',
  'connector',
  'terminal',
  'zkt',
  'ords',
  'oracle',
  'punch',
  'punches',
  'reconcile',
  'reconciliation',
  'reconciling',
  'firmware',
  'ota',
  'hil',
  'lease',
  'quarantine',
  'quarantined',
  'telemetry',
  'heartbeat',
  'payload',
  'snapshot',
  'outbox',
  'journal',
  'cursor',
  'ordinal',
  'epoch',
  'digest',
  'hash',
  'manifest',
  'custody',
  'provenance',
  'idempotency',
  'idempotent',
  'fail-closed',
  'durable',
  'durably',
  'authoritative',
  'certified',
  'certification',
  'truth',
  'divergence',
  'tail',
  'anchor',
  'slot',
  'boot',
  'reboot',
  'held',
  'hold',
  'kept back',
  'nvs',
  'spiffs',
  'psram',
  'uid',
  'http',
  'errno',
  'worker',
  'transport',
  'realtime',
  'degraded',
  'flapping',
  'acknowledge',
  'acknowledged',
  'resolve',
  'resolved',
  'alert',
  'alerts',
  'fleet',
  'severity',
  'operator',
  'evidence',
  'stranded',
  'gating',
  'latched',
  'schema',
]

/** Brand names: allowed only inside `data-brand` or `data-technical`. */
export const BRAND_WORDS: readonly string[] = ['ZKTeco', 'Hikvision']

/** Capital words allowed in plain text. "ADD" is allowed only inside `data-brand`. */
export const ALLOWED_CAPS: readonly string[] = [
  'CNIC', 'PKT', 'ID', 'HR', 'OK', 'AM', 'PM', 'IT', 'PIN', 'KB', 'MB', 'USB', 'COMM',
  // Part of real place names ("SLIC Tower"), not a system word.
  'SLIC',
]

/** The one allowed way to name Oracle: "HR system (Oracle)". */
export const ORACLE_PHRASE = 'HR system (Oracle)'

/** Busy labels (rule 3): verb + "…". */
export const BUSY = {
  saving: 'Saving…',
  sending: 'Sending…',
  checking: 'Checking…',
  loading: 'Loading…',
  cancelling: 'Cancelling…',
  rereading: 'Re-reading…',
  refreshing: 'Refreshing…',
  starting: 'Starting…',
  stopping: 'Stopping…',
  confirming: 'Confirming…',
} as const

/** The one text for every reason and password box (glossary). Only the words; each box keeps its own checks. */
export const STEP_UP = {
  why: 'Why?',
  whyHelp: (min = 10) => `Saved with your name. At least ${min} letters.`,
  whyCount: (count: number, min = 10) => `${Math.min(count, min)} of ${min}`,
  password: 'Your password',
  passwordHelp: "To confirm it's you.",
} as const

export type PlainRuleName =
  | 'snake_upper'
  | 'snake_lower'
  | 'key_value'
  | 'json'
  | 'hex'
  | 'mac'
  | 'ip'
  | 'file_line'
  | 'build_string'
  | 'device_code'
  | 'plural_s'
  | 'exclamation'
  | 'caps'
  | 'banned_word'
  | 'brand'
  | 'add_brand'

/** Rule 8 patterns. Each regex is global so every match is reported. */
export const PLAIN_RULES: ReadonlyArray<{ name: PlainRuleName; pattern: RegExp; why: string }> = [
  { name: 'snake_upper', pattern: /\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g, why: 'a code in capitals (SNAKE_CASE)' },
  { name: 'snake_lower', pattern: /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g, why: 'a code in small letters (snake_case)' },
  { name: 'key_value', pattern: /\b[A-Za-z_]\w*=\S/g, why: 'key=value text' },
  { name: 'json', pattern: /[{[]\s*"[^"]*"\s*[:,\]]/g, why: 'JSON' },
  { name: 'mac', pattern: /\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b/gi, why: 'a MAC address' },
  { name: 'hex', pattern: /\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{8,}\b/gi, why: 'a hash or digest' },
  { name: 'ip', pattern: /\b\d{1,3}(?:\.\d{1,3}){3}\b/g, why: 'an IP address' },
  { name: 'file_line', pattern: /\b[\w-]+\.(?:c|h|py|tsx?|jsx?)(?::\d+)?\b/g, why: 'a file name or line number' },
  { name: 'build_string', pattern: /\bzone-lite-\d/gi, why: 'a software build string' },
  { name: 'device_code', pattern: /\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+){1,}\b/g, why: 'a device code outside data-device-code' },
  { name: 'plural_s', pattern: /\w\(s\)/g, why: '"(s)" instead of a real plural' },
  { name: 'exclamation', pattern: /!/g, why: 'an exclamation mark' },
]

export interface PlainWordProblem {
  rule: PlainRuleName
  match: string
  why: string
}

export interface PlainCheckOptions {
  /** Allow ZKTeco / Hikvision (only inside data-brand or data-technical). */
  allowBrand?: boolean
  /** Allow "ADD" (only inside data-brand). */
  allowAddBrand?: boolean
}

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const BANNED_PATTERN = new RegExp(
  `(?<![A-Za-z0-9])(?:${[...BANNED_WORDS].sort((a, b) => b.length - a.length).map(escapeRegExp).join('|')})(?![A-Za-z0-9])`,
  'gi',
)
const BRAND_PATTERN = new RegExp(`\\b(?:${BRAND_WORDS.map(escapeRegExp).join('|')})\\b`, 'gi')
const CAPS_PATTERN = /\b[A-Z]{2,}[0-9]*\b/g
const ALLOWED_CAPS_SET = new Set(ALLOWED_CAPS)

/**
 * Every rule-8 problem in a piece of visible text. An empty list means the text
 * is plain. Used by the page guards and by the dictionary tests.
 */
export function findPlainWordProblems(text: string, options: PlainCheckOptions = {}): PlainWordProblem[] {
  if (!text) return []
  const problems: PlainWordProblem[] = []
  // "HR system (Oracle)" is the one allowed use of the word Oracle.
  const value = text.split(ORACLE_PHRASE).join('HR system')
  for (const rule of PLAIN_RULES) {
    for (const match of value.matchAll(rule.pattern)) problems.push({ rule: rule.name, match: match[0], why: rule.why })
  }
  for (const match of value.matchAll(BANNED_PATTERN)) {
    problems.push({ rule: 'banned_word', match: match[0], why: `a system word ("${match[0]}")` })
  }
  if (!options.allowBrand) {
    for (const match of value.matchAll(BRAND_PATTERN)) {
      problems.push({ rule: 'brand', match: match[0], why: 'a brand name outside data-brand' })
    }
  }
  const snake = new Set(problems.filter((p) => p.rule === 'snake_upper' || p.rule === 'device_code').map((p) => p.match))
  for (const match of value.matchAll(CAPS_PATTERN)) {
    const word = match[0]
    if (ALLOWED_CAPS_SET.has(word)) continue
    if ([...snake].some((code) => code.includes(word))) continue
    if (word === 'ADD') {
      if (!options.allowAddBrand) problems.push({ rule: 'add_brand', match: word, why: '"ADD" outside data-brand (say "the dashboard")' })
      continue
    }
    problems.push({ rule: 'caps', match: word, why: `a capital word that is not allowed ("${word}")` })
  }
  return problems
}

/** True when the text passes every rule. */
export const isPlainText = (text: string, options?: PlainCheckOptions) => findPlainWordProblems(text, options).length === 0

const NUMBER = new Intl.NumberFormat('en-PK')

/** Thousands separators, en-PK ("158,560"). */
export const formatCount = (value: number) => NUMBER.format(value)

const IRREGULAR: Record<string, string> = {
  person: 'people',
  is: 'are',
  was: 'were',
  has: 'have',
  this: 'these',
  entry: 'entries',
  copy: 'copies',
}

/**
 * "1 check-in" / "3 check-ins" / "1 person" / "2 people". Never "(s)" (rule 10).
 * Pass `many` for words a simple "s" can't make plural.
 */
export function plural(count: number, word: string, many?: string): string {
  const other = many ?? IRREGULAR[word] ?? (/(s|x|ch|sh)$/.test(word) ? `${word}es` : /[^aeiou]y$/.test(word) ? `${word.slice(0, -1)}ies` : `${word}s`)
  return `${formatCount(count)} ${count === 1 ? word : other}`
}

/** Just the word, no number: pluralWord(2, 'device') → "devices". */
export function pluralWord(count: number, word: string, many?: string): string {
  return plural(count, word, many).replace(/^[\d,]+ /, '')
}

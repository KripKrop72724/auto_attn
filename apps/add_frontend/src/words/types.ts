// Shared shapes for the plain-words dictionaries (02-words, "Where the words live").
//
// Every dictionary is keyed by a stable code the backend or the link box sends.
// The code itself never appears as text on screen: components put it on
// `data-status-code` or inside a "Technical details" element (`data-technical`).

/** The four colours a status may take. Tone comes only from a dictionary, never from substrings. */
export type Tone = 'good' | 'neutral' | 'warning' | 'problem'

/** Words for one code in one domain. */
export interface StateWord {
  /** 1-4 words, sentence case. */
  label: string
  tone: Tone
  /** One plain sentence shown on tap or hover. */
  explain: string
  /** "What to do" line, only when a person must act. */
  next?: string
  /** Sort key for "Needs attention first" (lower first). */
  rank?: number
}

/** A dictionary lookup result: the words plus the code they came from. */
export interface PlainStatus extends StateWord {
  code: string
  domain: Domain
  /** True when the dictionary has no words for this code (safe fallback used). */
  unknown: boolean
}

/** Every domain of `STATE_WORDS` (02-words Appendices A, C, D, E, F, G, H). */
export type Domain =
  // A: devices and the machine connection
  | 'device'
  | 'tier'
  | 'machineLink'
  | 'machineRaw'
  | 'currency'
  | 'binding'
  | 'latch'
  | 'coverage'
  | 'coverageKey'
  // C: problems
  | 'severity'
  | 'alertState'
  | 'alertQueue'
  | 'cleanupClass'
  // D: attendance
  | 'delivery'
  | 'deliveryAssurance'
  | 'confirmationPath'
  | 'identity'
  | 'clock'
  | 'captureSource'
  // E: requests, people, machine password
  | 'requestType'
  | 'request'
  | 'personChange'
  | 'peopleRemoval'
  | 'peopleRemovalItem'
  | 'lease'
  | 'commKey'
  | 'commKeyMode'
  // F: software
  | 'softwareDevice'
  | 'release'
  | 'update'
  | 'otaState'
  | 'rollout'
  | 'bulkStart'
  | 'updateWait'
  | 'updateEvent'
  | 'stability'
  | 'undoDiagnosis'
  | 'testSchedule'
  | 'repairUpdate'
  // G: Recover attendance
  | 'recoverJob'
  | 'recoverStep'
  | 'recoverPhase'
  | 'recoverEvent'
  | 'recoverEta'
  | 'recoverScope'
  | 'recoverList'
  | 'recoverBulk'
  | 'recoverPreflight'
  | 'recoveryAction'
  | 'recoveryLane'
  | 'recoveryJob'
  | 'recoveryItem'
  // H: attendance fix tools
  | 'forceItem'
  | 'areaRun'
  // Set up a device (08-advanced W3)
  | 'setup'
  | 'boxType'

/** The status chip's non-colour pattern (kept from today's `data-pattern`, App.test.tsx). */
export type StatusPattern = 'confirmed' | 'waiting' | 'blocked' | 'notice'

export const TONE_PATTERN: Record<Tone, StatusPattern> = {
  good: 'confirmed',
  neutral: 'notice',
  warning: 'waiting',
  problem: 'blocked',
}

export const toneToPattern = (tone: Tone): StatusPattern => TONE_PATTERN[tone]

/** Activity groups (Appendix B; the same six labels as the Problems type filter). */
export type ActivityGroup = 'machine' | 'link_box' | 'hr_system' | 'people' | 'software' | 'memory'

/** Activity kinds: two only (brief §2). */
export type ActivityKind = 'routine' | 'problem'

/** "Act": does a person need to do something? */
export type ActivityAct = 'no' | 'if_repeats' | 'yes' | 'list'

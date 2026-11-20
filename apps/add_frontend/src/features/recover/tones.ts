import type { Tone as ChipTone } from '../../ui'
import type { Tone } from '../../words/types'

/** Words tone → kit chip tone. A problem reads red with an alert icon (callers pass icon="alert"). */
export const chipTone = (tone: Tone): ChipTone =>
  tone === 'good' ? 'working' : tone === 'warning' ? 'attention' : tone === 'problem' ? 'offline' : 'info'

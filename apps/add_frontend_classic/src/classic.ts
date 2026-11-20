// The old screens live under /classic/ for one week after the revamp release.
// Remove this whole app with the steps in docs/add-classic-screens.md.

/** Router basename and URL prefix of the old screens. Matches `base` in vite.config.ts. */
export const CLASSIC_BASE = '/classic'

/**
 * The day the old screens are removed: one week after the revamp release.
 * Set it to the real date when the release goes live.
 */
export const CLASSIC_REMOVAL_DATE = '2026-11-28'

/** Where "Back to the new screens" goes. */
export const NEW_SCREENS_PATH = '/'

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']

/** "2026-11-28" -> "28 November 2026", with no time zone involved. */
export function removalDateLabel(isoDate: string = CLASSIC_REMOVAL_DATE) {
  const [year, month, day] = isoDate.split('-').map(Number)
  return `${day} ${MONTHS[month - 1]} ${year}`
}

/** Prefixes an app path (for example "/fleet") with /classic for plain links and history calls. */
export function withBase(path: string) {
  return `${CLASSIC_BASE}${path.startsWith('/') ? path : `/${path}`}`
}

/** The State Life logo from this app's own public folder. */
export const LOGO_SRC = `${import.meta.env.BASE_URL}state-life-logo.png`

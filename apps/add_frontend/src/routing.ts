// Dashboard pages and their addresses. Old addresses (bookmarks, links in
// messages, the old menu) redirect to the new ones with their query string and
// `?division=` kept. /status is the public page and never comes here.
import type { DashboardRoute, FirmwareSection, RecoverSection, UpdatesSection } from './types'
import type { DivisionChoice } from './lib/prefs'

export const DASHBOARD_ROUTES: readonly DashboardRoute[] = [
  'home', 'devices', 'attendance', 'problems', 'people', 'recover', 'updates', 'setup', 'settings',
]
const routes = new Set<string>(DASHBOARD_ROUTES)
export const DEFAULT_ROUTE: DashboardRoute = 'home'

/** The page for a path; anything unknown is Home. */
export function dashboardRoute(pathname: string): DashboardRoute {
  const segment = pathname.split('/').filter(Boolean)[0]
  return segment && routes.has(segment) ? segment as DashboardRoute : DEFAULT_ROUTE
}

/** The device id in `/devices/:id` or `/people/:deviceId`, or ''. */
export function routeDeviceId(pathname: string, route: 'devices' | 'people') {
  const parts = pathname.split('/').filter(Boolean)
  if (parts[0] !== route || !parts[1]) return ''
  try {
    return decodeURIComponent(parts[1])
  } catch {
    return ''
  }
}

/** The address of a page, with the Division choice carried (`?division=life`). */
export function routePath(route: DashboardRoute, deviceId?: string, options: { division?: DivisionChoice } = {}) {
  const path = deviceId && (route === 'devices' || route === 'people')
    ? `/${route}/${encodeURIComponent(deviceId)}`
    : `/${route}`
  return options.division && options.division !== 'all' ? `${path}?division=${options.division}` : path
}

const updatesSections = new Set<UpdatesSection>(['overview', 'updates', 'versions'])
const oldUpdatesSections: Record<string, UpdatesSection> = { campaigns: 'updates', releases: 'versions' }

/** The Software updates tab (`?tab=overview | updates | versions`); old tab names are read too. */
export function updatesSection(search: string): UpdatesSection {
  const value = new URLSearchParams(search).get('tab') || ''
  if (updatesSections.has(value as UpdatesSection)) return value as UpdatesSection
  return oldUpdatesSections[value] || 'overview'
}

/** Software updates tabs ⇄ the tabs of today's firmware page, until that page is rebuilt. */
export const firmwareSectionForUpdates = (section: UpdatesSection): FirmwareSection =>
  section === 'updates' ? 'campaigns' : section === 'versions' ? 'releases' : 'overview'
export const updatesSectionForFirmware = (section: FirmwareSection): UpdatesSection =>
  section === 'campaigns' ? 'updates' : section === 'releases' ? 'versions' : 'overview'

const recoverSections = new Set<RecoverSection>(['recoveries', 'unreadable', 'held'])
const oldRecoverSections: Record<string, RecoverSection> = {
  jobs: 'recoveries',
  'source-exceptions': 'unreadable',
  exceptions: 'unreadable',
  recovery: 'held',
}

/** The Recover attendance tab (`?tab=recoveries | unreadable | held`); old tab names are read too. */
export function recoverSection(search: string): RecoverSection {
  const value = new URLSearchParams(search).get('tab') || ''
  if (recoverSections.has(value as RecoverSection)) return value as RecoverSection
  return oldRecoverSections[value] || 'recoveries'
}

/** @deprecated Read `updatesSection`; kept for the old firmware page's tab type. */
export function firmwareSection(search: string): FirmwareSection {
  const value = new URLSearchParams(search).get('tab')
  if (value === 'prepare') return 'prepare'
  return firmwareSectionForUpdates(updatesSection(search))
}

const legacyHashes: Record<string, DashboardRoute> = {
  fleet: 'home',
  users: 'people',
  attendance: 'attendance',
  reconciliation: 'recover',
  firmware: 'updates',
  alerts: 'problems',
}

const join = (path: string, params: URLSearchParams, hash: string) => {
  const query = params.toString()
  return `${path}${query ? `?${query}` : ''}${hash}`
}

/**
 * Where an old or unknown address now lives, or null when it is already right.
 * Pure: the app calls it once per location and navigates with `replace`.
 */
export function legacyRedirect(pathname: string, search = '', hash = ''): string | null {
  const parts = pathname.split('/').filter(Boolean)
  const first = parts[0] || ''
  const second = parts[1] || ''
  const params = new URLSearchParams(search)
  const cleanHash = hash && hash !== '#' ? hash : ''

  // Very old links used a hash: /#users, /#firmware.
  const hashRoute = legacyHashes[cleanHash.replace(/^#/, '')]
  if (hashRoute && (!first || !routes.has(first))) {
    const division = params.get('division')
    const kept = new URLSearchParams()
    if (division) kept.set('division', division)
    return join(`/${hashRoute}`, kept, '')
  }

  const tab = params.get('tab') || ''
  const recover = () => {
    if (tab === 'employee-repair') {
      // The old blocked-punch review lives on Attendance (Reconciliation.tsx used to do this itself).
      const next = new URLSearchParams()
      next.set('view', 'needs-review')
      const deviceId = params.get('device_id')
      if (deviceId) next.set('device_id', deviceId)
      const division = params.get('division')
      if (division) next.set('division', division)
      return join('/attendance', next, cleanHash)
    }
    if (tab && oldRecoverSections[tab]) params.set('tab', oldRecoverSections[tab])
    return join('/recover', params, cleanHash)
  }
  const updates = () => {
    if (tab === 'prepare') {
      params.delete('tab')
      return join('/setup', params, cleanHash)
    }
    if (tab && oldUpdatesSections[tab]) params.set('tab', oldUpdatesSections[tab])
    return join('/updates', params, cleanHash)
  }

  switch (first) {
    case '':
      return join('/home', params, cleanHash)
    case 'fleet':
      return second ? join(`/devices/${second}`, params, cleanHash) : join('/home', params, cleanHash)
    case 'users':
      return join(second ? `/people/${second}` : '/people', params, cleanHash)
    case 'alerts':
      return join('/problems', params, cleanHash)
    case 'reconciliation':
      return recover()
    case 'firmware':
      if (!tab) params.set('tab', 'overview')
      return updates()
    case 'recover':
      return tab === 'employee-repair' || (tab && oldRecoverSections[tab]) || parts.length > 1 ? recover() : null
    case 'updates':
      return tab === 'prepare' || (tab && oldUpdatesSections[tab]) || parts.length > 1 ? updates() : null
    case 'devices':
    case 'people':
      return parts.length > 2 ? join(`/${first}/${second}`, params, cleanHash) : null
    default:
      if (!routes.has(first)) {
        // Unknown page: go Home, keeping only the Division choice.
        const kept = new URLSearchParams()
        const division = params.get('division')
        if (division) kept.set('division', division)
        return join('/home', kept, '')
      }
      return parts.length > 1 ? join(`/${first}`, params, cleanHash) : null
  }
}

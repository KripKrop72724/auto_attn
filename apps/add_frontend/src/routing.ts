import type { DashboardRoute, FirmwareSection } from './types'

const routes = new Set<DashboardRoute>([
  'fleet',
  'users',
  'attendance',
  'reconciliation',
  'firmware',
  'alerts',
])
const firmwareSections = new Set<FirmwareSection>(['overview', 'releases', 'campaigns'])

export function dashboardRoute(pathname: string): DashboardRoute {
  const segment = pathname.split('/').filter(Boolean)[0] as DashboardRoute | undefined
  return segment && routes.has(segment) ? segment : 'fleet'
}

export function routeDeviceId(pathname: string, route: 'fleet' | 'users') {
  const parts = pathname.split('/').filter(Boolean)
  return parts[0] === route && parts[1] ? decodeURIComponent(parts[1]) : ''
}

export function firmwareSection(search: string): FirmwareSection {
  const value = new URLSearchParams(search).get('tab') as FirmwareSection | null
  return value && firmwareSections.has(value) ? value : 'overview'
}

export function routePath(route: DashboardRoute, deviceId?: string) {
  if (deviceId && (route === 'fleet' || route === 'users')) {
    return `/${route}/${encodeURIComponent(deviceId)}`
  }
  return `/${route}`
}

import { describe, expect, it } from 'vitest'
import {
  dashboardRoute, firmwareSection, legacyRedirect, recoverSection, routeDeviceId, routePath, updatesSection,
} from './routing'

describe('dashboard routing', () => {
  it('maps supported paths and defaults unknown locations to home', () => {
    expect(dashboardRoute('/home')).toBe('home')
    expect(dashboardRoute('/devices/connector-one')).toBe('devices')
    expect(dashboardRoute('/attendance')).toBe('attendance')
    expect(dashboardRoute('/problems')).toBe('problems')
    expect(dashboardRoute('/people/connector-one')).toBe('people')
    expect(dashboardRoute('/recover')).toBe('recover')
    expect(dashboardRoute('/updates')).toBe('updates')
    expect(dashboardRoute('/setup')).toBe('setup')
    expect(dashboardRoute('/settings')).toBe('settings')
    expect(dashboardRoute('/not-a-dashboard-route')).toBe('home')
    expect(dashboardRoute('/fleet')).toBe('home')
    expect(dashboardRoute('/')).toBe('home')
  })

  it('round-trips connector identifiers without exposing operational filters', () => {
    const connectorId = 'ZONE-SWAT/terminal one'
    for (const route of ['devices', 'people'] as const) {
      const path = routePath(route, connectorId)
      expect(routeDeviceId(path, route)).toBe(connectorId)
      expect(path).not.toMatch(/cnic|password|reason|confirmation/i)
    }
    expect(routeDeviceId('/devices/%E0%A4%A', 'devices')).toBe('')
    expect(routeDeviceId('/people', 'people')).toBe('')
  })

  it('carries the Division choice on page links', () => {
    expect(routePath('devices', 'connector-one', { division: 'life' })).toBe('/devices/connector-one?division=life')
    expect(routePath('problems', undefined, { division: 'health' })).toBe('/problems?division=health')
    expect(routePath('home', undefined, { division: 'all' })).toBe('/home')
    expect(routePath('attendance', 'ignored')).toBe('/attendance')
  })

  it('accepts only allowlisted Software updates and Recover attendance tabs, old names included', () => {
    expect(updatesSection('?tab=updates')).toBe('updates')
    expect(updatesSection('?tab=versions&password=must-not-persist')).toBe('versions')
    expect(updatesSection('?tab=campaigns')).toBe('updates')
    expect(updatesSection('?tab=releases')).toBe('versions')
    expect(updatesSection('?tab=prepare')).toBe('overview')
    expect(updatesSection('?tab=unsafe')).toBe('overview')
    expect(recoverSection('?tab=unreadable')).toBe('unreadable')
    expect(recoverSection('?tab=source-exceptions')).toBe('unreadable')
    expect(recoverSection('?tab=recovery')).toBe('held')
    expect(recoverSection('?tab=jobs')).toBe('recoveries')
    expect(recoverSection('')).toBe('recoveries')
    expect(firmwareSection('?tab=updates')).toBe('campaigns')
    expect(firmwareSection('?tab=prepare')).toBe('prepare')
  })
})

describe('old addresses redirect', () => {
  it.each([
    ['/', '', '', '/home'],
    ['/', '?division=life', '', '/home?division=life'],
    ['/fleet', '', '', '/home'],
    ['/fleet', '?division=health', '', '/home?division=health'],
    ['/fleet/connector-one', '', '', '/devices/connector-one'],
    ['/fleet/connector-one', '?division=life', '', '/devices/connector-one?division=life'],
    ['/users', '', '', '/people'],
    ['/users/connector-one', '?user_id=1007', '', '/people/connector-one?user_id=1007'],
    ['/alerts', '?division=life', '', '/problems?division=life'],
    ['/reconciliation', '', '', '/recover'],
    ['/reconciliation', '?tab=jobs', '', '/recover?tab=recoveries'],
    ['/reconciliation', '?tab=source-exceptions&device_id=connector-one', '', '/recover?tab=unreadable&device_id=connector-one'],
    ['/reconciliation', '?tab=source-exceptions&job_id=job-1&device_id=connector-one', '', '/recover?tab=unreadable&job_id=job-1&device_id=connector-one'],
    ['/reconciliation', '?tab=exceptions', '', '/recover?tab=unreadable'],
    ['/reconciliation', '?tab=recovery', '', '/recover?tab=held'],
    ['/reconciliation', '?tab=employee-repair&device_id=connector-one', '', '/attendance?view=needs-review&device_id=connector-one'],
    ['/firmware', '', '', '/updates?tab=overview'],
    ['/firmware', '?tab=overview', '', '/updates?tab=overview'],
    ['/firmware', '?tab=campaigns', '', '/updates?tab=updates'],
    ['/firmware', '?tab=releases&division=life', '', '/updates?tab=versions&division=life'],
    ['/firmware', '?tab=prepare', '', '/setup'],
    ['/updates', '?tab=prepare', '', '/setup'],
    ['/updates', '?tab=campaigns', '', '/updates?tab=updates'],
    ['/recover', '?tab=source-exceptions', '', '/recover?tab=unreadable'],
    ['/', '', '#users', '/people'],
    ['/', '?division=life', '#firmware', '/updates?division=life'],
    ['/', '', '#fleet', '/home'],
    ['/', '', '#alerts', '/problems'],
    ['/', '', '#reconciliation', '/recover'],
    ['/', '', '#attendance', '/attendance'],
    ['/not-a-page', '?cnic=3520212345671&division=life', '', '/home?division=life'],
    ['/fleet/connector-one', '', '#logs', '/devices/connector-one#logs'],
  ])('%s%s%s → %s', (pathname, search, hash, target) => {
    expect(legacyRedirect(pathname, search, hash)).toBe(target)
  })

  it('leaves the new addresses alone', () => {
    for (const path of ['/home', '/devices', '/devices/connector-one', '/attendance', '/problems', '/people', '/people/connector-one', '/recover', '/updates', '/setup', '/settings']) {
      expect(legacyRedirect(path, '?division=life', '')).toBeNull()
    }
    expect(legacyRedirect('/attendance', '?view=checkins&tool=release', '')).toBeNull()
    expect(legacyRedirect('/recover', '?tab=unreadable&device_id=x', '')).toBeNull()
    expect(legacyRedirect('/updates', '?tab=versions', '')).toBeNull()
  })

  it('never keeps a private value from an unknown page', () => {
    const target = legacyRedirect('/old', '?password=secret&reason=x', '#users')
    expect(target).toBe('/people')
    expect(target).not.toMatch(/password|reason|cnic|confirmation/i)
  })
})

import { describe, expect, it } from 'vitest'
import { dashboardRoute, firmwareSection, routeDeviceId, routePath } from './routing'

describe('dashboard routing', () => {
  it('maps supported paths and defaults unknown locations to fleet', () => {
    expect(dashboardRoute('/attendance')).toBe('attendance')
    expect(dashboardRoute('/firmware')).toBe('firmware')
    expect(dashboardRoute('/not-a-dashboard-route')).toBe('fleet')
  })

  it('round-trips connector identifiers without exposing operational filters', () => {
    const connectorId = 'ZONE-SWAT/terminal one'
    const path = routePath('fleet', connectorId)
    expect(routeDeviceId(path, 'fleet')).toBe(connectorId)
    expect(path).not.toContain('cnic')
    expect(path).not.toContain('password')
    expect(path).not.toContain('reason')
  })

  it('accepts only allowlisted firmware tabs', () => {
    expect(firmwareSection('?tab=campaigns')).toBe('campaigns')
    expect(firmwareSection('?tab=releases&password=must-not-persist')).toBe('releases')
    expect(firmwareSection('?tab=unsafe')).toBe('overview')
  })
})

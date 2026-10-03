import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { FirmwareDiagnostics } from '../types'
import { FirmwareHealth } from './FirmwareHealth'

afterEach(cleanup)
const healthy: FirmwareDiagnostics = {
  schema_version: 1,
  boot_id: 'current',
  storage: { durability: 'HEALTHY', persistence_verified: true, recovery_complete: true },
  workers: [], queues: [],
}
describe('firmware preservation evidence', () => {
  it('separates an active probe error from historical failures and a healthy claim', () => {
    const diagnostics = { ...healthy, storage: { ...healthy.storage!, persistence_probe_error: 5,
      persistence_probe_operation: 'persistence_sync', persistence_probe_failures: 3,
      persistence_probe_total_failures: 12 } }
    const { rerender } = render(<FirmwareHealth bootId="current" diagnostics={diagnostics} observedAt={new Date().toISOString()} />)
    expect(screen.getByRole('heading', { name: 'Local storage needs attention' })).toBeTruthy()
    expect(screen.getByText('persistence_sync · 5')).toBeTruthy()
    rerender(<FirmwareHealth bootId="current" diagnostics={{ ...diagnostics, storage: {
      ...diagnostics.storage, persistence_probe_error: 0, persistence_probe_failures: 0,
    } }} observedAt={new Date().toISOString()} />)
    expect(screen.getByRole('heading', { name: 'Local storage verified' })).toBeTruthy()
    expect(screen.getByText('12')).toBeTruthy()
    expect(screen.queryByText('Active persistence probe error')).toBeNull()
  })
  it('does not turn absent legacy diagnostics into healthy zero values', () => {
    render(<FirmwareHealth />)
    expect(screen.getByRole('heading', { name: 'Local durability not reported' })).toBeTruthy()
    expect(screen.queryByText('Local storage verified')).toBeNull()
  })
  it('requires both persistence and recovery evidence', () => {
    render(<FirmwareHealth bootId="current" diagnostics={{ ...healthy, storage: { ...healthy.storage!, recovery_complete: false } }} observedAt={new Date().toISOString()} />)
    expect(screen.getByRole('heading', { name: 'Local recovery checks pending' })).toBeTruthy()
  })
  it('does not present stale healthy reports as current health', () => {
    render(<FirmwareHealth bootId="current" diagnostics={healthy} observedAt={new Date(Date.now() - 60_000).toISOString()} />)
    expect(screen.getByRole('heading', { name: 'Durability telemetry is stale' })).toBeTruthy()
    expect(screen.queryByText('Local storage verified')).toBeNull()
  })
  it('keeps unknown queue counts distinct from an empty queue', () => {
    render(<FirmwareHealth bootId="current" diagnostics={{ ...healthy, queues: [{ name: 'live', count_known: false, records: 0 }] }} observedAt={new Date().toISOString()} />)
    expect(screen.getByRole('heading', { name: 'Local storage verified' })).toBeTruthy()
    expect(screen.getByText(/Pending count not reported/)).toBeTruthy()
    expect(screen.queryByText(/0 pending/)).toBeNull()
  })
  it.each([undefined, 'old-boot'])('withholds health for missing or mismatched boot evidence: %s', boot => {
    render(<FirmwareHealth bootId="current" diagnostics={{ ...healthy, boot_id: boot }} observedAt={new Date().toISOString()} />)
    expect(screen.queryByText('Local storage verified')).toBeNull()
    expect(screen.getByText(/Boot identity is unverified/)).toBeTruthy()
  })
})

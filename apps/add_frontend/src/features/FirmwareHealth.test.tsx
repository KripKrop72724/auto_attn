import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { FirmwareDiagnostics } from '../types'
import { FirmwareHealth } from './FirmwareHealth'

afterEach(cleanup)
const healthy: FirmwareDiagnostics = {
  schema_version: 1,
  storage: { durability: 'HEALTHY', persistence_verified: true, recovery_complete: true },
  workers: [], queues: [],
}
describe('firmware preservation evidence', () => {
  it('does not turn absent legacy diagnostics into healthy zero values', () => {
    render(<FirmwareHealth />)
    expect(screen.getByRole('heading', { name: 'Local durability not reported' })).toBeTruthy()
    expect(screen.queryByText('Local storage verified')).toBeNull()
  })
  it('requires both persistence and recovery evidence', () => {
    render(<FirmwareHealth diagnostics={{ ...healthy, storage: { ...healthy.storage!, recovery_complete: false } }} observedAt={new Date().toISOString()} />)
    expect(screen.getByRole('heading', { name: 'Local recovery checks pending' })).toBeTruthy()
  })
  it('does not present stale healthy reports as current health', () => {
    render(<FirmwareHealth diagnostics={healthy} observedAt={new Date(Date.now() - 60_000).toISOString()} />)
    expect(screen.getByRole('heading', { name: 'Durability telemetry is stale' })).toBeTruthy()
    expect(screen.queryByText('Local storage verified')).toBeNull()
  })
  it('keeps unknown queue counts distinct from an empty queue', () => {
    render(<FirmwareHealth diagnostics={{ ...healthy, queues: [{ name: 'live', count_known: false, records: 0 }] }} observedAt={new Date().toISOString()} />)
    expect(screen.getByRole('heading', { name: 'Local storage verified' })).toBeTruthy()
    expect(screen.getByText(/Pending count not reported/)).toBeTruthy()
    expect(screen.queryByText(/0 pending/)).toBeNull()
  })
})

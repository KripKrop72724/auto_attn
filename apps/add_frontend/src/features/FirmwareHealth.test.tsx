import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { FirmwareDiagnostics } from '../types'
import { FirmwareHealth } from './FirmwareHealth'

afterEach(cleanup)
const healthy: FirmwareDiagnostics = {
  schema_version: 1,
  boot_id: 'current',
  delivery_authority: 'ADD',
  storage: { durability: 'HEALTHY', persistence_verified: true, recovery_complete: true },
  workers: [], queues: [],
}
const journal: NonNullable<FirmwareDiagnostics['journal_runtime']> = {
  observed: true, phase: 'READY', reader_ready: true, writer_ready: false,
  delivery_authority: 'ADD',
  start_attempts: 9, storage_starts: 1, delivery_starts: 1, capture_starts: 0,
  proof_attempts: 3, failures: 7, sampled_uptime_ms: 40000, compatibility: 'READER_OK',
}
describe('firmware preservation evidence', () => {
  it.each(['UNKNOWN', 'LEGACY', undefined] as const)('does not infer ADD ownership from writer-ready: %s', authority => {
    render(<FirmwareHealth bootId="current" diagnostics={{ ...healthy, sampled_uptime_ms: 41000,
      journal_runtime: { ...journal, writer_ready: true, delivery_authority: authority } }} observedAt={new Date().toISOString()} />)
    expect(screen.queryByText('Local writer permitted')).toBeNull()
  })
  it.each(['UNKNOWN', undefined] as const)('shows uncertain delivery ownership explicitly: %s', authority => {
    render(<FirmwareHealth bootId="current" diagnostics={{ ...healthy, delivery_authority: authority,
      sampled_uptime_ms: 41000, journal_runtime: { ...journal, writer_ready: true } }} observedAt={new Date().toISOString()} />)
    expect(screen.getByText('Current ownership unverified')).toBeTruthy()
    expect(screen.queryByText('Legacy delivery paths')).toBeNull()
    expect(screen.queryByText('Local writer permitted')).toBeNull()
  })
  it.each([
    ['QUIESCING', 'Finishing storage work before restart'],
    ['AUTHORITY_HOLD', 'Delivery ownership needs recovery'],
    ['BRIDGE_VALIDATION', 'Verifying the bridge reader before boot confirmation'],
  ])('shows %s without granting capture permission', (phase, label) => {
    render(<FirmwareHealth bootId="current" diagnostics={{ ...healthy, sampled_uptime_ms: 41000,
      journal_runtime: { ...journal, phase, writer_ready: true } }} observedAt={new Date().toISOString()} />)
    expect(screen.getByText(label)).toBeTruthy()
    expect(screen.queryByText('Local writer permitted')).toBeNull()
  })
  it('shows actual journal starts independently from attempts and writer permission', () => {
    render(<FirmwareHealth bootId="current" diagnostics={{ ...healthy, sampled_uptime_ms: 41000, journal_runtime: journal }} observedAt={new Date().toISOString()} />)
    expect(screen.getByText('Startup checks passed')).toBeTruthy()
    expect(screen.getByText('Ready for recovery and receipt delivery')).toBeTruthy()
    expect(screen.getByText('1 storage · 1 delivery · 0 capture')).toBeTruthy()
    expect(screen.getByText('Writer permission not confirmed')).toBeTruthy()
  })
  it.each(['stale-parent', 'stale-runtime', 'future-runtime', 'missing-sample', 'wrong-boot', 'invalid-counters'])('withholds a journal writer verdict for %s', failure => {
    const diagnostics = { ...healthy, sampled_uptime_ms: 41000, journal_runtime: { ...journal, writer_ready: true } }
    if (failure === 'stale-runtime') diagnostics.sampled_uptime_ms = 90000
    if (failure === 'future-runtime') diagnostics.journal_runtime.sampled_uptime_ms = 42000
    if (failure === 'missing-sample') diagnostics.journal_runtime.sampled_uptime_ms = undefined
    if (failure === 'wrong-boot') diagnostics.boot_id = 'old'
    if (failure === 'invalid-counters') diagnostics.journal_runtime.storage_starts = -1
    render(<FirmwareHealth bootId="current" diagnostics={diagnostics} observedAt={new Date(Date.now() - (failure === 'stale-parent' ? 46000 : 0)).toISOString()} />)
    expect(screen.getByText('Current journal state unverified')).toBeTruthy()
    expect(screen.queryByText('Local writer permitted')).toBeNull()
    expect(screen.queryByText('Ready for recovery and receipt delivery')).toBeNull()
  })
  it('handles the 32-bit runtime clock wrap and never promotes a stalled worker', () => {
    const diagnostics = { ...healthy, sampled_uptime_ms: 0x100000000 + 1000,
      journal_runtime: { ...journal, sampled_uptime_ms: 0xffffffff - 999, writer_ready: true } }
    const { rerender } = render(<FirmwareHealth bootId="current" diagnostics={diagnostics} observedAt={new Date().toISOString()} />)
    expect(screen.getByText('Local writer permitted')).toBeTruthy()
    rerender(<FirmwareHealth bootId="current" diagnostics={{ ...diagnostics, journal_runtime: { ...diagnostics.journal_runtime, phase: 'STALLED' } }} observedAt={new Date().toISOString()} />)
    expect(screen.getByText('Worker progress is stalled')).toBeTruthy()
    expect(screen.queryByText('Local writer permitted')).toBeNull()
  })
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

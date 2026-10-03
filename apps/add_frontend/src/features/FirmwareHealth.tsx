import type { FirmwareDiagnostics } from '../types'
import { useEffect, useState } from 'react'

const bytes = (value?: number | null) => value == null ? 'Not reported' : `${(value / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 })} KiB`
const labels: Record<string, string> = {
  add_delivery: 'ADD delivery', ords_delivery: 'Oracle delivery',
  live: 'Live attendance', bulk: 'Historical attendance', receipts: 'Delivery receipts',
  blocked: 'Identity exceptions', evidence: 'Preserved evidence', ords: 'Oracle pending',
}

export function FirmwareHealth({ diagnostics, observedAt, bootId, imageDigest }: {
  diagnostics?: FirmwareDiagnostics | null
  observedAt?: string | null
  bootId?: string | null
  imageDigest?: string | null
}) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  const observed = observedAt ? Date.parse(observedAt) : NaN
  const sameBoot = Boolean(diagnostics?.boot_id && bootId && diagnostics.boot_id === bootId)
  const fresh = sameBoot && Number.isFinite(observed) && now - observed >= -1000 && now - observed <= 45_000
  const storage = diagnostics?.storage
  const verified = fresh && storage?.durability === 'HEALTHY' && storage.persistence_verified && storage.recovery_complete
  const heading = !diagnostics ? 'Local durability not reported'
    : !sameBoot ? 'Durability boot identity is unverified'
      : !fresh ? 'Durability telemetry is stale'
      : verified ? 'Local storage verified'
        : storage?.durability === 'DEGRADED' || storage?.durability === 'FULL' ? 'Local storage needs attention'
          : 'Local recovery checks pending'
  return <article className="detail-card wide" aria-label="Firmware preservation health">
    <p className="eyebrow">ATTENDANCE PRESERVATION</p>
    <h3>{heading}</h3>
    <p>Connection status, local preservation and Oracle delivery are checked separately.</p>
    {!diagnostics ? <p>This firmware has not reported preservation diagnostics.</p> : <>
      {!fresh && <p>Values below are the last report and do not confirm current health.</p>}
      <dl>
        <div><dt>Telemetry received</dt><dd>{observedAt ? new Date(observedAt).toLocaleString() : 'Not reported'}{Number.isFinite(observed) ? ` · ${Math.max(0, Math.floor((now - observed) / 1000))} seconds ago` : ''}</dd></div>
        <div><dt>Telemetry sampled</dt><dd>{diagnostics.sampled_at || 'Not reported'}</dd></div>
        <div><dt>Boot identity</dt><dd>{diagnostics.boot_id || bootId || 'Not reported'}{!sameBoot ? ' · Boot identity is unverified' : ''}</dd></div>
        <div><dt>Application digest</dt><dd style={{ overflowWrap: 'anywhere' }}>{imageDigest || 'Not reported'}</dd></div>
        <div><dt>Oracle delivery owner</dt><dd>{diagnostics.delivery_authority === 'ADD' ? 'ADD' : 'Legacy delivery paths'}</dd></div>
        <div><dt>Storage used / total</dt><dd>{bytes(storage?.used_bytes)} / {bytes(storage?.total_bytes)}</dd></div>
        <div><dt>Reserved admission space</dt><dd>{bytes(storage?.admission_reserve_bytes)}</dd></div>
        <div><dt>Write failures</dt><dd>{storage?.write_failures ?? 'Not reported'}</dd></div>
        <div><dt>Read failures</dt><dd>{storage?.read_failures ?? 'Not reported'}</dd></div>
        <div><dt>Persistence probe failures</dt><dd>{storage?.persistence_probe_failures ?? 'Not reported'}</dd></div>
        <div><dt>Active reconciliation mode</dt><dd>{diagnostics.reconciliation_mode?.replaceAll('_', ' ') || 'Not reported'}</dd></div>
        <div><dt>Committed source cursor</dt><dd>{diagnostics.committed_source_cursor ?? 'Not reported'}</dd></div>
        {storage?.error_operation && <div><dt>Last storage error</dt><dd>{storage.error_operation} · {storage.error_code ?? 'No code reported'}</dd></div>}
      </dl>
      {diagnostics.workers.map(worker => <p key={worker.name}>
        {labels[worker.name] || worker.name}: {worker.state === 'UNKNOWN' ? 'Not reported' : worker.state.replaceAll('_', ' ').toLowerCase()}
        {worker.operation ? ` · ${worker.operation}` : ''}
        {worker.restart_count != null ? ` · ${worker.restart_count} successful restarts` : ''}
        {worker.restart_attempts != null ? ` · ${worker.restart_attempts} restart attempts` : ''}
      </p>)}
      {!diagnostics.workers.length && <p>Delivery workers: Not reported</p>}
      {diagnostics.queues.map(queue => <p key={queue.name}>
        {labels[queue.name] || queue.name}: {queue.count_known && queue.records != null ? `${queue.records.toLocaleString()} pending` : 'Pending count not reported'} · {bytes(queue.bytes)}
      </p>)}
    </>}
  </article>
}

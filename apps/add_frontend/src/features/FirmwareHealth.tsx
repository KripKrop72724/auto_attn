import type { FirmwareDiagnostics } from '../types'
import { useEffect, useState } from 'react'
import { captureLatencyLabel } from './captureLatency'

const bytes = (value?: number | null) => value == null ? 'Not reported' : `${(value / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 })} KiB`
const labels: Record<string, string> = {
  add_delivery: 'ADD delivery', ords_delivery: 'Oracle delivery',
  live: 'Live attendance', bulk: 'Historical attendance', receipts: 'Delivery receipts',
  blocked: 'Identity exceptions', evidence: 'Preserved evidence', ords: 'Oracle pending',
  capture: 'Terminal capture', storage_owner: 'Journal storage', journal: 'Preserved journal',
  journal_add_delivery: 'Journal ADD delivery', legacy_add_delivery: 'Retained ADD delivery',
  legacy_ords_delivery: 'Retained Oracle delivery', legacy_migration: 'Legacy custody transfer',
  add_live: 'Retained live attendance', add_bulk: 'Retained historical attendance',
  ords_pending: 'Retained Oracle pending', identity_blocked: 'Retained identity exceptions',
  ords_quarantine: 'Oracle preserved evidence', add_quarantine: 'ADD preserved evidence',
  add_quarantine_backup: 'ADD preserved evidence backup',
}
const legacyOperations: Record<string, string> = {
  legacy_read: 'reading preserved records', legacy_append: 'preserving a record',
  legacy_retire: 'checkpointing preserved custody',
}
const countReasons: Record<string, string> = {
  VERIFIED_EMPTY: 'Empty queue verified', NONEMPTY_OR_UNVERIFIED: 'Pending inventory not yet verified',
  PENDING_APPEND: 'Capture is awaiting storage', STALE_OWNER: 'Current storage evidence unavailable',
  UNVERIFIED_MIGRATION: 'Legacy custody verification pending',
}
const journalPhases: Record<string, string> = {
  NOT_STARTED: 'Not started', DISABLED: 'Inactive for this image', SECURITY_HOLD: 'Security checks required',
  BINDING_HOLD: 'Terminal binding needs review', STORAGE_WAIT: 'Waiting for storage checks',
  OWNER_START: 'Starting storage worker', RECOVERING: 'Recovering preserved records',
  TRANSPORT_START: 'Starting delivery worker', CHECKING_READER: 'Checking rollback reader',
  READER_HOLD: 'Reader compatibility needs review', CAPTURE_START: 'Starting capture',
  WRITER_DISABLED: 'Reader active; new capture is disabled', READY: 'Startup checks passed',
  STALLED: 'Worker progress is stalled', UNKNOWN: 'Unknown',
  QUIESCING: 'Finishing storage work before restart', AUTHORITY_HOLD: 'Delivery ownership needs recovery',
  BRIDGE_VALIDATION: 'Verifying the bridge reader before boot confirmation',
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
  const ota = diagnostics?.ota_runtime
  const currentBootReported = fresh && Boolean(ota?.running_version &&
    ['factory', 'ota_0', 'ota_1'].includes(ota.running_partition || '') &&
    /^[0-9a-f]{64}$/.test(ota.image_sha256 || ''))
  const storage = diagnostics?.storage
  const journal = diagnostics?.journal_runtime
  const journalStorage = diagnostics?.journal_storage
  const capture = diagnostics?.workers.find(worker => worker.name === 'capture')
  const captureSample = capture?.last_activity_uptime_ms
  const captureAge = typeof diagnostics?.sampled_uptime_ms === 'number' && typeof captureSample === 'number' &&
    Number.isSafeInteger(diagnostics.sampled_uptime_ms) && diagnostics.sampled_uptime_ms >= 0 && Number.isSafeInteger(captureSample) && captureSample >= 0
    ? diagnostics.sampled_uptime_ms - captureSample : Infinity
  const captureFresh = fresh && captureAge >= 0 && captureAge + Math.max(0, now - observed) <= 45_000
  const count = (value: unknown) => typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 0xffffffff
  const journalValid = journal?.observed === true && Object.hasOwn(journalPhases, journal.phase) &&
    typeof journal.reader_ready === 'boolean' && typeof journal.writer_ready === 'boolean' &&
    [journal.start_attempts, journal.storage_starts, journal.delivery_starts, journal.capture_starts,
      journal.proof_attempts, journal.failures].every(count)
  const parentUptime = diagnostics?.sampled_uptime_ms
  const journalLag = journalValid && count(journal.sampled_uptime_ms) && typeof parentUptime === 'number' &&
    Number.isSafeInteger(parentUptime) && parentUptime >= 0
    ? (parentUptime % 0x100000000 - journal.sampled_uptime_ms! + 0x100000000) % 0x100000000 : Infinity
  const journalFresh = fresh && journalLag + Math.max(0, now - observed) <= 45_000
  const journalReader = journalFresh && journal?.reader_ready &&
    ['CHECKING_READER', 'READER_HOLD', 'CAPTURE_START', 'WRITER_DISABLED', 'BRIDGE_VALIDATION', 'READY'].includes(journal.phase)
  const ownerSample = journalStorage?.sampled_uptime_ms
  const ownerAge = typeof parentUptime === 'number' && typeof ownerSample === 'number' &&
    Number.isSafeInteger(ownerSample) && ownerSample >= 0 ? parentUptime - ownerSample : Infinity
  const ownerFresh = fresh && journalStorage?.observed && journalStorage.fresh && ownerAge >= -5000 &&
    Math.max(0, ownerAge) + Math.max(0, now - observed) <= 45_000
  const journalStorageVerified = ownerFresh && journalStorage?.ready && journalStorage.durability === 'HEALTHY' &&
    !journalStorage.checkpoint_recovery_pending && (!journalStorage.last_append_result || journalStorage.last_append_result === 'OK')
  const legacyNeedsAttention = Boolean(storage?.legacy_read_faults || storage?.legacy_append_faults ||
    storage?.legacy_retire_faults || storage?.legacy_error_code)
  const verified = fresh && storage?.durability === 'HEALTHY' && storage.persistence_verified && storage.recovery_complete && !storage.persistence_probe_error && !legacyNeedsAttention &&
    (diagnostics?.runtime_profile !== 'ZKT_JOURNAL_V1' || journalStorageVerified)
  const journalNeedsAttention = ownerFresh && journalStorage &&
    (['DEGRADED', 'FULL'].includes(journalStorage.durability) || !journalStorage.ready ||
      journalStorage.checkpoint_recovery_pending || (journalStorage.last_append_result && journalStorage.last_append_result !== 'OK'))
  const heading = !diagnostics ? 'Local durability not reported'
    : !sameBoot ? 'Durability boot identity is unverified'
      : !fresh ? 'Durability telemetry is stale'
      : verified ? 'Local storage verified'
        : storage?.durability === 'DEGRADED' || storage?.durability === 'FULL' || storage?.persistence_probe_error || legacyNeedsAttention || journalNeedsAttention ? 'Local storage needs attention'
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
        <div><dt>Current boot evidence</dt><dd>{currentBootReported ? 'Fresh report from this boot' : 'Unverified'}</dd></div>
        <div><dt>Reported running image</dt><dd>{ota?.running_version || 'Not reported'}{ota?.running_partition ? ` · ${ota.running_partition}` : ''}</dd></div>
        <div><dt>Reported running application digest</dt><dd style={{ overflowWrap: 'anywhere' }}>{ota?.image_sha256 || 'Not reported'}</dd></div>
        <div><dt>Last OTA capability digest</dt><dd style={{ overflowWrap: 'anywhere' }}>{imageDigest || 'Not reported'}</dd></div>
        <div><dt>Reported OTA state</dt><dd>{ota?.state?.replaceAll('_', ' ') || 'Not reported'}</dd></div>
        {!!ota?.last_error && <div><dt>Reported OTA error</dt><dd>{ota.last_error === 'BOOT_ROLLBACK_PREDECESSOR_UNQUALIFIED'
          ? 'Previous image is not qualified to read the preserved data; connector rollback is held.'
          : ota.last_error.replaceAll('_', ' ')}</dd></div>}
        <div><dt>Local boot checks</dt><dd>{ota?.boot_health_checks
          ? `${ota.boot_health_checks} · ${ota.boot_health_last_ready ? 'Last check ready' : 'Last check not ready'}` : 'No checks reported'}</dd></div>
        <div><dt>Oracle delivery owner</dt><dd>{fresh && diagnostics.delivery_authority === 'ADD' ? 'ADD'
          : fresh && diagnostics.delivery_authority === 'LEGACY_DUAL' ? 'Legacy delivery paths' : 'Current ownership unverified'}</dd></div>
        <div><dt>Storage used / total</dt><dd>{bytes(storage?.used_bytes)} / {bytes(storage?.total_bytes)}</dd></div>
        <div><dt>Reserved admission space</dt><dd>{bytes(storage?.admission_reserve_bytes)}</dd></div>
        <div><dt>Write failures</dt><dd>{storage?.write_failures ?? 'Not reported'}</dd></div>
        <div><dt>Read failures</dt><dd>{storage?.read_failures ?? 'Not reported'}</dd></div>
        <div><dt>Active legacy read faults</dt><dd>{storage?.legacy_read_faults ?? 'Not reported'}</dd></div>
        <div><dt>Legacy writes awaiting recovery proof</dt><dd>{storage?.legacy_append_faults ?? 'Not reported'}</dd></div>
        <div><dt>Legacy retirement faults awaiting recovery proof</dt><dd>{storage?.legacy_retire_faults ?? 'Not reported'}</dd></div>
        <div><dt>Legacy read faults recovered since boot</dt><dd>{storage?.legacy_read_recoveries ?? 'Not reported'}</dd></div>
        {!!storage?.legacy_error_code && <div><dt>Active legacy storage error</dt><dd>{labels[storage.legacy_error_queue || ''] || 'Queue not reported'} · {legacyOperations[storage.legacy_error_operation || ''] || 'Operation not reported'} · {storage.legacy_error_code}</dd></div>}
        <div><dt>Persistence probe failures</dt><dd>{storage?.persistence_probe_failures ?? 'Not reported'}</dd></div>
        <div><dt>Probe failures since boot</dt><dd>{storage?.persistence_probe_total_failures ?? 'Not reported'}</dd></div>
        {!!storage?.persistence_probe_error && <div><dt>Active persistence probe error</dt><dd>{storage.persistence_probe_operation || 'Operation not reported'} · {storage.persistence_probe_error}</dd></div>}
        <div><dt>Active reconciliation mode</dt><dd>{diagnostics.reconciliation_mode?.replaceAll('_', ' ') || 'Not reported'}</dd></div>
        <div><dt>Committed source cursor</dt><dd>{diagnostics.committed_source_cursor ?? 'Not reported'}</dd></div>
        {capture && <>
          <div><dt>Complete packet preservation time</dt><dd>{captureFresh ? captureLatencyLabel(capture.packet_commit_latency_ms) : 'Current capture timing unverified'}</dd></div>
          <div><dt>Fragment preservation time</dt><dd>{captureFresh ? captureLatencyLabel(capture.fragment_commit_latency_ms) : 'Current capture timing unverified'}</dd></div>
        </>}
        {storage?.error_operation && <div><dt>Last storage error</dt><dd>{storage.error_operation} · {storage.error_code ?? 'No code reported'}</dd></div>}
        {journalStorage && <>
          <div><dt>Journal preservation</dt><dd>{ownerFresh ? journalStorage.durability.toLowerCase() : 'Current journal storage unverified'}</dd></div>
          <div><dt>Journal checkpoint recovery</dt><dd>{ownerFresh ? journalStorage.checkpoint_recovery_pending ? 'Recovery pending' : 'No checkpoint recovery pending' : 'Not confirmed'}</dd></div>
          <div><dt>Last capture append</dt><dd>{journalStorage.last_append_result || 'No result reported'}</dd></div>
          <div><dt>Storage mailbox peak / capacity</dt><dd>{journalStorage.mailbox_high_watermark} / {journalStorage.mailbox_capacity}</dd></div>
          <div><dt>Capture appends awaiting storage</dt><dd>{ownerFresh ? journalStorage.pending_appends : 'Not confirmed'}</dd></div>
          {journalStorage.last_failure_operation && <div><dt>Historical journal failure</dt><dd>{journalStorage.last_failure_operation} · filesystem {journalStorage.last_filesystem_error ?? 'not reported'} · NVS {journalStorage.last_nvs_error ?? 'not reported'}</dd></div>}
        </>}
        {journal && <>
          <div><dt>Journal startup</dt><dd>{journalFresh ? journalPhases[journal.phase] : 'Current journal state unverified'}</dd></div>
          <div><dt>Journal reader</dt><dd>{journalReader ? 'Ready for recovery and receipt delivery' : 'Readiness not confirmed'}</dd></div>
          <div><dt>New journal capture</dt><dd>{journalFresh && journal.delivery_authority === 'ADD' && diagnostics.delivery_authority === 'ADD' &&
            journal.writer_ready && journalReader && journal.phase === 'READY' ? 'Local writer permitted' : 'Writer permission not confirmed'}</dd></div>
          <div><dt>Journal start attempts</dt><dd>{journalValid ? journal.start_attempts : 'Not reported'}</dd></div>
          <div><dt>Journal workers started</dt><dd>{journalValid ? `${journal.storage_starts} storage · ${journal.delivery_starts} delivery · ${journal.capture_starts} capture` : 'Not reported'}</dd></div>
          <div><dt>Reader checks</dt><dd>{journalValid ? journal.proof_attempts : 'Not reported'}</dd></div>
          <div><dt>Last reader compatibility result</dt><dd style={{ overflowWrap: 'anywhere' }}>{journal.compatibility || 'Not reported'}</dd></div>
        </>}
      </dl>
      {capture?.packet_commit_latency_ms && <p>Timing covers successful preservation in this capture session. Failed or incomplete packets remain separate; this does not certify attendance delivery latency.</p>}
      {diagnostics.workers.map(worker => <p key={worker.name}>
        {labels[worker.name] || worker.name}: {worker.state === 'UNKNOWN' ? 'Not reported' : worker.state.replaceAll('_', ' ').toLowerCase()}
        {worker.operation ? ` · ${worker.operation}` : ''}
        {worker.restart_count != null ? ` · ${worker.restart_count} successful restarts` : ''}
        {worker.restart_attempts != null ? ` · ${worker.restart_attempts} restart attempts` : ''}
        {worker.pending_requests != null ? ` · ${worker.pending_requests} pending requests` : ''}
        {worker.failures != null ? ` · ${worker.failures} failures since boot` : ''}
        {worker.timeouts != null ? ` · ${worker.timeouts} timeouts since boot` : ''}
        {worker.refusals != null ? ` · ${worker.refusals} admission refusals since boot` : ''}
      </p>)}
      {!diagnostics.workers.length && <p>Delivery workers: Not reported</p>}
      {diagnostics.queues.map(queue => <p key={queue.name}>
        {labels[queue.name] || queue.name}: {queue.count_known && queue.records != null ? `${queue.records.toLocaleString()} pending` : 'Pending count not reported'} · {bytes(queue.bytes)}
        {queue.count_reason && countReasons[queue.count_reason] ? ` · ${countReasons[queue.count_reason]}` : ''}
      </p>)}
    </>}
  </article>
}

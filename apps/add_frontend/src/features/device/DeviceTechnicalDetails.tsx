// Summary › Technical details (04-device §2 block 4): identifiers, machine,
// software, health evidence, link box memory, record checks and the machine
// connection history. Closed unless "Show technical details" is on.
import type { ReactNode } from 'react'
import { ago, dayTime } from '../../lib/time'
import { TechnicalDetails } from '../../ui'
import { plainActivity, plainConnectionRow } from '../../words/machine'
import { SUMMARY_WORDS, softwareVersion, type DeviceDetail } from '../../words/device'
import { HealthEvidence } from '../DeviceHealthPanel'
import { FirmwareHealth } from '../FirmwareHealth'
import { deviceCode, isHikvision } from './deviceNaming'
import { useConnectivity } from './useDeviceData'

const when = (value: string | null | undefined) => (value ? `${dayTime(value)} · ${ago(value)}` : 'Not recorded')
const epochIso = (value: number | null | undefined) => (value ? new Date(value * 1000).toISOString() : null)

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="dv-tech-section" aria-labelledby={`dv-tech-${id}`}>
      <h4 id={`dv-tech-${id}`}>{title}</h4>
      {children}
    </section>
  )
}

function Rows({ rows }: { rows: Array<[string, ReactNode] | null | false | undefined> }) {
  return <dl className="dv-tech-rows">{rows.filter(Boolean).map((row) => { const [label, value] = row as [string, ReactNode]; return <div key={label}><dt>{label}</dt><dd>{value}</dd></div> })}</dl>
}

const LIGHT_STATES: Record<string, string> = { SCANNING: 'Running', COMPLETE: 'Done', BLOCKED: 'Stuck', WAITING: 'Waiting', RETRYING: 'Trying again' }
const BACKFILL: Record<string, string> = { NOT_STARTED: 'Not started', RUNNING: 'In progress', IN_PROGRESS: 'In progress', COMPLETE: 'Done', COMPLETED: 'Done', BLOCKED: 'Stuck', FAILED: 'Stuck' }

function RecordChecks({ device }: { device: DeviceDetail }) {
  if (isHikvision(device)) {
    const hik = device.hikvision
    const light = hik?.light_reconcile
    return (
      <>
        <p>{hik?.capture_mode === 'poll' ? `Checks the machine every ${hik.poll_interval_seconds ?? '?'} seconds` : 'Waiting for the first report'}</p>
        <Rows rows={[
          ['Last good check', hik?.last_successful_poll_epoch ? when(epochIso(hik.last_successful_poll_epoch)) : 'Not reported yet'],
          ['Records waiting in the link box', hik?.source_queue_depth ?? 'Not reported yet'],
          ['Last saved record number', hik?.durable_poll_cursor ?? 'Not reported yet'],
          ['Last check took', hik?.last_poll_interval_ms ? `${(hik.last_poll_interval_ms / 1000).toFixed(1)} seconds` : 'Not reported yet'],
          hik?.poll_stage ? ['Last check step', `${hik.poll_stage} · ${hik.poll_http_status ? `reply ${hik.poll_http_status}` : 'no reply'}`] : null,
          hik?.consecutive_poll_failures != null ? ['Failed checks in a row', hik.consecutive_poll_failures] : null,
          ['Error number', hik?.poll_error ?? 0],
        ]} />
        {light && <dl className="dv-tech-rows" aria-label="Quick check of older records">
          <div><dt>Quick check of older records</dt><dd>{LIGHT_STATES[light.state] ?? light.state}</dd></div>
          <div><dt>Records checked this round</dt><dd>{light.scanned}</dd></div>
          <div><dt>Last full round</dt><dd>{light.last_completed_epoch ? when(epochIso(light.last_completed_epoch)) : 'Not finished yet'}</dd></div>
          <div><dt>How often</dt><dd>Older records are checked slowly in the background, again every 6 hours.</dd></div>
        </dl>}
        <p>New check-ins keep arriving while older records are checked. The full history is in Recover attendance.</p>
      </>
    )
  }
  const zkt = device.zkt
  const capabilities = zkt?.capabilities || {}
  const certified = Boolean(capabilities.source_coverage_certified)
  return (
    <>
      <p>{zkt?.attendance_count == null ? 'Counting records…' : `${zkt.attendance_count.toLocaleString('en-PK')} records saved on the machine`}
        {' · '}{certified ? 'Every record confirmed; only new records are checked now' : zkt?.last_reconcile_at ? `Last full record check ${ago(zkt.last_reconcile_at)}` : 'No full record check yet'}</p>
      {certified ? <Rows rows={[
        ['Record history', 'Confirmed'],
        ['Last confirmed record number', String(device.firmware_diagnostics?.committed_source_cursor ?? capabilities.source_coverage_cursor ?? 'Not reported yet')],
      ]} /> : <Rows rows={[
        ['Old records collected', BACKFILL[String(capabilities.history_backfill_state || 'NOT_STARTED').toUpperCase()] ?? String(capabilities.history_backfill_state)],
        ['Oldest record on the machine', String(capabilities.history_coverage_start_month || 'Not known yet')],
        ['Checking month', String(capabilities.history_cursor_month || '—')],
        ['Months that could not be checked', Number(capabilities.history_failed_windows || 0)],
      ]} />}
    </>
  )
}

function ConnectionHistory({ connectorId }: { connectorId: string }) {
  const query = useConnectivity(connectorId, true)
  if (query.isPending) return <p>{SUMMARY_WORDS.connectionSubtitle}…</p>
  if (query.isError) return <p>{SUMMARY_WORDS.connectionError} <button type="button" className="text-button" onClick={() => void query.refetch()}>Try again</button></p>
  const rows = query.data || []
  if (!rows.length) return <p>{SUMMARY_WORDS.connectionEmpty}</p>
  return (
    <ol className="dv-connection-list">
      {rows.map((row) => {
        const words = plainConnectionRow(row)
        return (
          <li key={row.id}>
            <time dateTime={row.observed_at}>{dayTime(row.observed_at)}</time>
            <span>{words.sentence}</span>
            <small>{row.from_state || 'first'} → {row.to_state}{row.reason ? ` · ${row.reason}` : ''}</small>
          </li>
        )
      })}
    </ol>
  )
}

export function DeviceTechnicalDetails({ device }: { device: DeviceDetail }) {
  const zkt = device.zkt
  const bound = zkt?.confirmed_serial || zkt?.expected_serial
  const copy = [
    `Device code: ${deviceCode(device)}`,
    `Connection id: ${device.connector_id}`,
    `Link box ID (MAC): ${device.hardware_id}`,
    device.boot_id ? `Restart id: ${device.boot_id}` : '',
    zkt?.serial ? `Machine serial number: ${zkt.serial}` : '',
    zkt?.ip_address ? `Machine network address: ${zkt.ip_address}` : '',
    device.firmware_version ? `Link box software: ${device.firmware_version}` : '',
  ].filter(Boolean).join('\n')
  return (
    <TechnicalDetails className="dv-technical" copyText={copy}>
      <Section id="identifiers" title={SUMMARY_WORDS.identifiers}>
        <Rows rows={[
          ['Device code', deviceCode(device)],
          ['Connection id', device.connector_id],
          ['Link box ID (MAC)', device.hardware_id],
          ['Restart id', device.boot_id || 'Not reported'],
          ['Times set up', device.onboarding_generation],
          ['Last set up', when(device.last_onboarded_at)],
          ['Place code', `${device.zone_id} · ${device.zone_name}`],
        ]} />
      </Section>
      <Section id="machine" title={SUMMARY_WORDS.machineSection}>
        <Rows rows={[
          ['Doing now', plainActivity(device)],
          ['Machine serial number', `${zkt?.serial || '—'}${bound && zkt?.serial && bound !== zkt.serial ? ` (set up for ${bound})` : ''}`],
          ['Machine network address', zkt?.ip_address || '—'],
          ['Machine model', zkt?.model || '—'],
          ['Changes to people allowed', zkt?.certification_state === 'CERTIFIED' ? 'Yes' : 'Not yet'],
          ['People list read', zkt?.snapshot_complete ? 'Complete' : 'Not complete yet'],
          ['People list version', zkt?.identity_snapshot_revision ?? '—'],
          ['Machine clock reading', zkt?.device_time ? `${dayTime(zkt.device_time)} · checked ${ago(zkt.device_time_sampled_at)}` : 'Not checked yet'],
          ['Machine clock difference', zkt?.drift_seconds == null ? 'Not checked yet' : `${Math.round(zkt.drift_seconds)} s off · checked ${ago(zkt.device_time_sampled_at)}`],
          zkt?.writes_disabled_reason ? ['Why changes are paused', zkt.writes_disabled_reason] : null,
        ]} />
      </Section>
      <Section id="software" title={SUMMARY_WORDS.softwareSection}>
        <Rows rows={[
          ['Link box software', device.firmware_version || 'Not reported'],
          ['Version', softwareVersion(device.firmware_version) || '—'],
          ['Running copy', device.ota_running_partition || '—'],
          ['Software fingerprint', device.ota_image_sha256 || '—'],
          ['Update state', device.ota_state || '—'],
        ]} />
      </Section>
      {!device.is_spare && <Section id="health" title={SUMMARY_WORDS.healthSection}>
        <HealthEvidence device={device} health={device.health} />
      </Section>}
      <Section id="memory" title={SUMMARY_WORDS.memorySection}>
        <FirmwareHealth diagnostics={device.firmware_diagnostics} observedAt={device.firmware_diagnostics_at} bootId={device.boot_id} imageDigest={device.ota_image_sha256} />
      </Section>
      <Section id="records" title={SUMMARY_WORDS.recordChecks}>
        <RecordChecks device={device} />
      </Section>
      <Section id="connection" title={SUMMARY_WORDS.connectionHistory}>
        <p>{SUMMARY_WORDS.connectionSubtitle}</p>
        <ConnectionHistory connectorId={device.connector_id} />
      </Section>
    </TechnicalDetails>
  )
}

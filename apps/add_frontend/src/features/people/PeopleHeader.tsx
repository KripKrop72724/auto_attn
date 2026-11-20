// The device block on /people/:deviceId (07-people P2) and the one notice at a
// time under it (P3).
import { useState } from 'react'
import type { Command } from '../../types'
import { Callout, DivisionTag, StatusChip, TechnicalDetails, toneForPattern } from '../../ui'
import { Icon } from '../../Icon'
import { ago, dayTime } from '../../lib/time'
import { deviceStatus } from '../../words/deviceStatus'
import { toneToPattern } from '../../words/types'
import { CAPABILITY_OFF, HEADER, NOTICE, writesOff } from '../../words/people'
import { MachineCheckCard, MachineCheckPending } from './MachineCheck'
import { baseWritable, divisionOf, friendlyName, machineBinding, type PeopleDevice } from './model'

export function PeopleDeviceHeader({ device, otherDivision, canCreate, onAdd, onChangeDevice, onRefresh, refreshing, changing }: {
  device: PeopleDevice
  /** The global switch shows the other division: say so (a direct link still opens this device). */
  otherDivision: boolean
  canCreate: boolean
  onAdd: () => void
  onChangeDevice: () => void
  onRefresh: () => Promise<void>
  refreshing: boolean
  /** A change is being sent: refreshing waits for it. */
  changing: boolean
}) {
  const status = deviceStatus(device)
  const division = divisionOf(device)
  const zkt = device.zkt
  const readAt = zkt?.identity_snapshot_observed_at || zkt?.identity_snapshot_received_at
  const brand = device.firmware_family === 'hikvision' ? 'Hikvision' : 'ZKTeco'
  const confirmedSerial = zkt?.terminal_binding_state === 'CONFIRMED' && zkt.serial ? zkt.serial : ''
  return (
    <section className="people-device-header" aria-labelledby="people-device-title">
      <div className="people-device-header-text">
        <h2 id="people-device-title">{friendlyName(device)}</h2>
        <p className="people-device-header-facts">
          <DivisionTag division={division} />
          <StatusChip tone={toneForPattern(toneToPattern(status.tone))} label={status.chip} code={status.code} size="sm" />
          <span>{HEADER.people(zkt?.user_count)}</span>
          {status.group !== 'working' && device.last_seen_at && <span>{HEADER.lastHeard(ago(device.last_seen_at))}</span>}
        </p>
        <p className="people-muted">
          {zkt && !zkt.snapshot_complete ? HEADER.listReading : readAt ? HEADER.listRead(ago(readAt)) : null}
        </p>
        {otherDivision && <p className="people-muted">{HEADER.otherDivision(division === 'life' ? 'Life' : 'Health')}</p>}
      </div>
      <div className="people-device-header-actions">
        <button type="button" className="button secondary" onClick={onChangeDevice}><Icon name="server" /> {HEADER.changeDevice}</button>
        <span className="people-refresh">
          <button type="button" className="button secondary" disabled={!device.connected || refreshing || changing} aria-describedby={!device.connected ? 'people-refresh-reason' : undefined} onClick={() => void onRefresh()}>
            <Icon name="refresh" /> {refreshing ? HEADER.refreshBusy : HEADER.refresh}
          </button>
          {!device.connected && <small id="people-refresh-reason">{HEADER.refreshOffline}</small>}
        </span>
        {canCreate && <button type="button" className="button primary" onClick={onAdd}><Icon name="userPlus" /> {HEADER.addPerson}</button>}
      </div>
      <TechnicalDetails className="people-device-technical">
        <span data-brand>{brand}</span>
        {[zkt?.model, zkt?.serial && `serial ${zkt.serial}`, zkt?.ip_address, device.display_name, device.hardware_id].filter(Boolean).map((part) => ` · ${part}`)}
        {confirmedSerial && <><br />{HEADER.machineConfirmed(confirmedSerial)}</>}
      </TechnicalDetails>
    </section>
  )
}

export function PeopleNotice({ device, command, onConfirmMachine, deviceHref }: {
  device: PeopleDevice
  command: Command | null
  onConfirmMachine: () => void
  deviceHref: string
}) {
  const binding = machineBinding(device)
  const zkt = device.zkt
  const place = friendlyName(device)
  if (binding.needsAction) return <MachineCheckCard device={device} place={place} onConfirm={onConfirmMachine} />
  if (binding.pending) return <MachineCheckPending device={device} command={command} onRetry={onConfirmMachine} />
  if (!zkt) return null
  const capabilities = zkt.capabilities || {}
  if (!baseWritable(device)) {
    const code = zkt.writes_disabled_reason || ''
    const reason = writesOff(code)
    return (
      <section className="people-notice" aria-label={NOTICE.viewOnlyChip} role="status">
        <Callout
          tone="warning"
          title={<><StatusChip tone="attention" label={NOTICE.viewOnlyChip} labelPrefix={null} size="sm" code={zkt.certification_state} /> {NOTICE.viewOnly}</>}
          action={code === 'SERIAL_MISMATCH' ? <a className="button secondary" href={deviceHref}>{HEADER.openDevice}</a> : undefined}
        >
          <p>{reason.sentence}</p>
          {reason.unknown && code && <TechnicalDetails>{code}</TechnicalDetails>}
        </Callout>
      </section>
    )
  }
  if (zkt.certification_state === 'PROFILE_EXCEPTION') {
    const expires = capabilities.write_exception_expires_at
    const text = capabilities.write_exception_permanent
      ? NOTICE.exceptionForever
      : `${NOTICE.exceptionUntil(expires ? dayTime(String(expires)) : '—')}${capabilities.write_exception_pending_firmware ? NOTICE.exceptionPendingSoftware : ''}`
    return (
      <section className="people-notice" aria-label={NOTICE.exceptionLabel}>
        <Callout tone="info" title={NOTICE.exceptionTitle} icon="shield">
          <p>{text}{NOTICE.exceptionNoRole}</p>
        </Callout>
      </section>
    )
  }
  const missing = [
    capabilities.create_user !== true && NOTICE.capabilityAdd,
    capabilities.user_write !== true && NOTICE.capabilityChange,
    capabilities.delete_user !== true && NOTICE.capabilityRemove,
  ].filter(Boolean) as string[]
  if (missing.length) {
    return (
      <section className="people-notice" aria-label={NOTICE.capabilityTitle}>
        <Callout tone="info" title={NOTICE.capabilityTitle}>
          <ul className="people-notice-list">{missing.map((item) => <li key={item}>{item}</li>)}</ul>
          {capabilities.create_user !== true && <p>{CAPABILITY_OFF.create}</p>}
        </Callout>
      </section>
    )
  }
  return null
}

export function StatusFailedLine({ onRetry }: { onRetry: () => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  return (
    <p className="people-slim-line" role="status">
      <Icon name="alert" size="sm" /> {HEADER.statusFailed}{' '}
      <button type="button" className="text-button" disabled={busy} onClick={async () => { setBusy(true); try { await onRetry() } finally { setBusy(false) } }}>{busy ? 'Trying…' : 'Try again'}</button>
    </p>
  )
}

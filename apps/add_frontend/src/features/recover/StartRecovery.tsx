// Start a recovery (08-advanced 1.2): a friendly device picker, one verdict
// line instead of the readiness badge and eight-row checklist ("Why?" keeps
// the checks), and three radio cards for what to read on Hikvision machines.
// Starting stays one click with no reason, phrase or password.
import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Button, Disclosure, DivisionTag, SegmentedControl, Sheet, StatusChip, TechnicalDetails, useMediaQuery } from '../../ui'
import { ago } from '../../lib/time'
import { deviceStatus } from '../../words/deviceStatus'
import { formatCount } from '../../words/glossary'
import {
  SCOPE_WORDS,
  activeUsersHelp,
  blockerTitle,
  capitalised,
  recentTailHelp,
  scopeUnavailable,
  verdict as verdictFor,
  type Verdict,
} from '../../words/recover'
import type { DivisionChoice } from '../../lib/division'
import type { Device, HikvisionHistoryScope, ReconciliationPreflight } from '../../types'
import type { Namer } from './names'
import { chipTone } from './tones'

const COARSE = '(pointer: coarse)'

function DevicePicker({ devices, namer, division, selectedId, onSelect, onClose }: {
  devices: Device[]
  namer: Namer
  division: DivisionChoice
  selectedId: string
  onSelect: (id: string) => void
  onClose: () => void
}) {
  const coarse = useMediaQuery(COARSE)
  const [tab, setTab] = useState<DivisionChoice>(division)
  const [query, setQuery] = useState('')
  const [spares, setSpares] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return devices
      .map((device) => ({ device, place: namer(device) }))
      .filter(({ device, place }) => (spares || !device.is_spare)
        && (tab === 'all' || place.division === tab)
        && `${place.name} ${place.city} ${device.display_name} ${device.zone_id} ${device.zone_name} ${device.zkt?.serial || ''} ${device.hardware_id}`.toLowerCase().includes(needle))
      .sort((left, right) => Number(Boolean(left.device.is_spare)) - Number(Boolean(right.device.is_spare)) || (left.place.division === right.place.division ? 0 : left.place.division === 'life' ? -1 : 1) || left.place.name.localeCompare(right.place.name, undefined, { numeric: true }))
  }, [devices, namer, query, spares, tab])
  const move = (event: KeyboardEvent<HTMLElement>, index: number) => {
    const options = Array.from(listRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') || [])
    let next = index
    if (event.key === 'ArrowDown') next = Math.min(options.length - 1, index + 1)
    else if (event.key === 'ArrowUp') next = Math.max(0, index - 1)
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = options.length - 1
    else return
    event.preventDefault()
    options[next]?.focus()
  }
  return (
    <Sheet title="Choose a device to recover" onClose={onClose} initialFocus={coarse ? 'close' : 'first-field'} className="recover-sheet recover-picker">
      <div className="recover-picker-tools">
        <SegmentedControl options={[{ id: 'all', label: 'All' }, { id: 'life', label: 'Life' }, { id: 'health', label: 'Health' }]} value={tab} onChange={(value) => setTab(value as DivisionChoice)} label="Division" fullWidth />
        <label className="search-field recover-search">
          <span className="sr-only">Search devices</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowDown') {
                event.preventDefault()
                listRef.current?.querySelector<HTMLButtonElement>('[role="option"]')?.focus()
              }
            }}
            placeholder="Search by place, device or serial number"
          />
        </label>
      </div>
      <div ref={listRef} className="recover-picker-list" role="listbox" aria-label="Devices">
        {rows.map(({ device, place }, index) => {
          const status = deviceStatus(device)
          return (
            <button
              key={device.connector_id}
              type="button"
              role="option"
              aria-selected={device.connector_id === selectedId}
             
              onKeyDown={(event) => move(event, index)}
              onClick={() => { onSelect(device.connector_id); onClose() }}
            >
              <span className="recover-picker-name">
                <strong>{place.name}</strong>
                <small>{place.city} <DivisionTag division={place.division} /></small>
              </span>
              <span className="recover-picker-status">
                <StatusChip size="sm" tone={chipTone(status.tone)} label={status.chip} />
                <small>{device.zkt?.attendance_count != null ? `${formatCount(device.zkt.attendance_count)} check-ins on the machine · ` : ''}seen {ago(device.last_seen_at)}</small>
              </span>
            </button>
          )
        })}
        {!rows.length && <p className="recover-muted recover-empty-line">No devices match your search.</p>}
      </div>
      <label className="recover-switch">
        <input type="checkbox" checked={spares} onChange={(event) => setSpares(event.target.checked)} />
        <span>Show spare devices</span>
      </label>
    </Sheet>
  )
}

const checkIcon = (ok: boolean | null) => (ok === null ? '◷' : ok ? '✓' : '!')

/** "Why?": the eight checks in plain words (W1.3). */
function WhyChecks({ device, detail, preflight }: { device: Device; detail: Device | null; preflight: ReconciliationPreflight }) {
  const hik = device.firmware_family === 'hikvision' || preflight.source_protocol === 'hikvision-isapi-v1'
  const resume = hik ? preflight.terminal?.serial_search_enabled : preflight.terminal?.range_resume_verified
  const lease = detail?.active_lease
  const checks: Array<{ label: string; value: string; ok: boolean | null }> = [
    { label: 'Link box', value: device.connected ? 'Online' : 'Offline: the recovery will wait', ok: device.connected },
    { label: 'Attendance machine', value: preflight.terminal ? (preflight.terminal.connection_state === 'ONLINE' ? 'Responding' : 'Not responding') : 'Not known yet', ok: preflight.terminal ? preflight.terminal.connection_state === 'ONLINE' : null },
    { label: hik ? 'Can search its records' : 'Can carry on after a break', value: resume ? 'Yes' : 'Not confirmed', ok: Boolean(resume) },
    { label: hik ? 'Matching people' : 'People list', value: hik ? 'By name and CNIC; check-ins without a match wait' : device.zkt?.snapshot_complete ? 'Up to date' : 'Incomplete: some check-ins may not be matched', ok: hik || Boolean(device.zkt?.snapshot_complete) },
    { label: 'Other work on this device', value: detail?.active_command ? 'Busy' : 'None', ok: detail?.active_command ? null : true },
    { label: 'Temporary admin access', value: lease ? `On, ends ${ago(lease.expires_at)}` : 'None', ok: lease ? null : true },
    { label: 'Last full recovery', value: preflight.coverage ? `Done ${preflight.coverage.captured_at ? ago(preflight.coverage.captured_at) : ''} · ${formatCount(preflight.coverage.source_committed_cursor)} records`.replace('Done  ·', 'Done ·') : 'Never done', ok: preflight.coverage ? preflight.coverage.chain_continuous !== false : null },
    { label: 'Sent to the HR system', value: preflight.coverage?.oracle_certified_at ? 'Confirmed' : 'Not confirmed yet', ok: preflight.coverage?.oracle_certified_at ? true : null },
  ]
  return (
    <Disclosure summary="Why?" className="recover-why">
      <ul className="recover-checks">
        {checks.map((check) => (
          <li key={check.label} data-ok={check.ok === null ? 'wait' : check.ok ? 'yes' : 'no'}>
            <span aria-hidden="true">{checkIcon(check.ok)}</span>
            <span><strong>{check.label}</strong><small>{check.value}</small></span>
          </li>
        ))}
      </ul>
      {preflight.hard_blockers.length > 0 && (
        <div className="recover-blockers">
          <p><strong>Can't start because</strong></p>
          <ul>{preflight.hard_blockers.map((item) => <li key={item.code}>{capitalised(blockerTitle(item.code))}</li>)}</ul>
        </div>
      )}
      {preflight.waitable_blockers.length > 0 && (
        <div className="recover-blockers">
          <p><strong>Will start once</strong></p>
          <ul>{preflight.waitable_blockers.map((item) => <li key={item.code}>{blockerTitle(item.code, true)}</li>)}</ul>
        </div>
      )}
      <TechnicalDetails>
        <dl className="recover-tech">
          <div><dt>Device code</dt><dd>{device.zone_id} · connector {device.connector_id} · device {device.device_id}</dd></div>
          <div><dt>Machine</dt><dd>{preflight.terminal?.connection_state || 'No terminal evidence'} · {preflight.terminal?.serial || 'serial pending'}</dd></div>
          {preflight.coverage && <div><dt>Source coverage</dt><dd>{preflight.coverage.capture_state} · cursor {preflight.coverage.source_committed_cursor.toLocaleString()} · {preflight.coverage.oracle_state}</dd></div>}
          {[...preflight.hard_blockers, ...preflight.waitable_blockers].map((item) => <div key={`code-${item.code}`}><dt>{item.code}</dt><dd>{item.message}</dd></div>)}
          {preflight.active_user_history?.reason && <div><dt>Active users</dt><dd>{preflight.active_user_history.reason}</dd></div>}
          {preflight.recent_tail?.reason && <div><dt>Recent tail</dt><dd>{preflight.recent_tail.reason}</dd></div>}
          {preflight.recent_tail?.eligible && <div><dt>Recent tail</dt><dd>{`Resumes at terminal serial ${preflight.recent_tail.begin_serial} (re-reads up to serial ${preflight.recent_tail.anchor_serial}).`}{preflight.recent_tail.earlier_breaks?.length ? ` Poll records also break after serial ${preflight.recent_tail.earlier_breaks.join(', ')}.` : ''}</dd></div>}
        </dl>
      </TechnicalDetails>
    </Disclosure>
  )
}

export interface StartRecoveryProps {
  devices: Device[]
  namer: Namer
  division: DivisionChoice
  enabled: boolean
  selectedId: string
  onSelect: (id: string) => void
  preflight: ReconciliationPreflight | null
  preflightLoading: boolean
  preflightError: boolean
  onRetryPreflight: () => void
  deviceDetail: Device | null
  scope: HikvisionHistoryScope
  onScope: (scope: HikvisionHistoryScope) => void
  starting: boolean
  started: boolean
  onStart: () => void
  onShowIt: () => void
}

export function StartRecovery(props: StartRecoveryProps) {
  const { devices, namer, division, enabled, selectedId, onSelect, preflight, preflightLoading, preflightError, onRetryPreflight, deviceDetail, scope, onScope, starting, started, onStart, onShowIt } = props
  const [pickerOpen, setPickerOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const selected = devices.find((device) => device.connector_id === selectedId) || null
  const place = selected ? namer(selected) : null
  const isHikvision = selected?.firmware_family === 'hikvision'
  const activeUsersOnly = isHikvision && scope === 'ACTIVE_USERS'
  const recentTail = isHikvision && scope === 'RECENT_TAIL'
  const scopeBlocked = activeUsersOnly && preflight?.active_user_history?.eligible === false
    ? scopeUnavailable('ACTIVE_USERS', preflight.active_user_history.reason)
    : recentTail && preflight && preflight.recent_tail?.eligible !== true
      ? scopeUnavailable('RECENT_TAIL', preflight.recent_tail?.reason)
      : null
  const canStart = Boolean(selected && enabled && preflight?.eligible && !scopeBlocked)
  const line: Verdict | null = selected ? verdictFor({ enabled, loading: preflightLoading, error: preflightError, started, preflight }) : null
  const scopeHelp = scope === 'ACTIVE_USERS'
    ? activeUsersHelp(preflight?.active_user_history?.eligible ? preflight.active_user_history.user_count : null)
    : scope === 'RECENT_TAIL'
      ? (preflight?.recent_tail?.eligible ? recentTailHelp(preflight.recent_tail.begin_serial, preflight.recent_tail.earlier_breaks) : SCOPE_WORDS.RECENT_TAIL.help)
      : SCOPE_WORDS.ALL_RECORDS.help
  const showButton = enabled && line?.kind !== 'blocked' && line?.kind !== 'off' && line?.kind !== 'running'

  return (
    <section className="recover-start panel" aria-labelledby="recover-start-title">
      <h2 id="recover-start-title">{place ? `Ready to recover ${place.name}?` : 'Start a recovery'}</h2>
      {!enabled && <p className="recover-off">{line?.text || 'Recover attendance is switched off for now. Ask IT to switch it on.'}</p>}
      <div className="recover-start-device">
        <div className="recover-start-current">
          {place ? (
            <>
              <strong>{place.name}</strong>
              <small>{place.city} <DivisionTag division={place.division} /></small>
            </>
          ) : <strong>Choose a device to recover</strong>}
        </div>
        {selected && <StatusChip size="sm" tone={chipTone(deviceStatus(selected).tone)} label={deviceStatus(selected).chip} />}
        <Button ref={triggerRef} icon="search" aria-haspopup="dialog" aria-expanded={pickerOpen} onClick={() => setPickerOpen(true)}>
          {selected ? 'Change device' : 'Choose device'}
        </Button>
      </div>
      {selected && isHikvision && (
        <fieldset className="recover-scope">
          <legend>What to read</legend>
          <div className="recover-scope-cards" role="radiogroup" aria-label="What to read">
            {(['ALL_RECORDS', 'ACTIVE_USERS', 'RECENT_TAIL'] as const).map((value) => (
              <label key={value} className="recover-scope-card" data-checked={scope === value ? '' : undefined}>
                <input type="radio" name="recover-scope" value={value} checked={scope === value} onChange={() => onScope(value)} />
                <span>{SCOPE_WORDS[value].label}</span>
              </label>
            ))}
          </div>
          <p className="recover-scope-help">{scopeBlocked || scopeHelp}</p>
        </fieldset>
      )}
      {selected && line && enabled && (
        <div className="recover-verdict" data-kind={line.kind} data-tone={line.tone} role="status">
          <span className="recover-verdict-icon" aria-hidden="true">{line.kind === 'ready' || line.kind === 'running' ? '✓' : line.kind === 'waits' ? '◷' : line.kind === 'blocked' ? '✕' : line.kind === 'error' ? '!' : '…'}</span>
          <span className="recover-verdict-text">{line.text}</span>
          {line.showIt && <Button variant="link" onClick={onShowIt}>Show it</Button>}
          {line.kind === 'error' && <Button variant="link" onClick={onRetryPreflight}>Try again</Button>}
        </div>
      )}
      {selected && showButton && (
        <Button variant="primary" className="recover-start-button" busy={starting} busyLabel="Starting…" disabled={!canStart || starting} onClick={onStart}>
          Start recovery
        </Button>
      )}
      {selected && preflight && <WhyChecks device={deviceDetail || selected} detail={deviceDetail} preflight={preflight} />}
      {pickerOpen && (
        <DevicePicker
          devices={devices}
          namer={namer}
          division={division}
          selectedId={selectedId}
          onSelect={onSelect}
          onClose={() => { setPickerOpen(false); window.setTimeout(() => triggerRef.current?.focus(), 0) }}
        />
      )}
    </section>
  )
}

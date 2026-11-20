// Past setups (08-advanced 3.4, ADV-21; today "Provisioning history"): rows by
// place with the Life / Health tag, search and Mine · Everyone, and each setup's
// summary in a sheet. Hidden when there are none.
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import { api, queryString } from '../../api'
import { deviceName } from '../../lib/deviceNames'
import { oneOf, text, useUrlState } from '../../lib/urlState'
import { dateTime, dayTime } from '../../lib/time'
import { Button, DivisionTag, ErrorState, FilterBar, SegmentedControl, Sheet, Skeleton, Spinner, StatusChip, TechnicalDetails, toneForPattern } from '../../ui'
import { toneToPattern } from '../../words/types'
import { PAST_SETUPS, SETUP_ENDINGS, boxType, setupState } from '../../words/setup'

export interface SetupSession {
  session_id: string
  operator: string
  hardware_mac: string | null
  hardware_classification: string | null
  zone_id: string | null
  zone_name: string | null
  device_id: string | null
  state: string
  progress: number
  result: Record<string, unknown>
  bundle?: { version: string; firmware_family?: string; bundle_id?: string } | null
  created_at: string
  updated_at: string
  events?: Array<{ state: string; created_at: string; details?: Record<string, unknown> }>
}

interface SessionList {
  rows: SetupSession[]
  next_cursor?: number | null
  filtered_total?: number
}

const schema = { q: text(), mine: oneOf(['1', '0'] as const, '1') }

export function setupPlace(session: Pick<SetupSession, 'device_id' | 'zone_id' | 'zone_name'>) {
  const code = session.device_id || session.zone_id
  if (!code) return null
  const parsed = deviceName(code)
  return { name: parsed.unit ? `${parsed.location} · ${parsed.unit}` : parsed.location, division: parsed.zone }
}

const chipFor = (state: string) => {
  const word = setupState(state)
  return { label: word.unknown ? 'Other status' : word.label, tone: toneForPattern(toneToPattern(word.tone)) }
}

export function PastSetups({ username, refreshKey, onOpenDevice }: { username: string; refreshKey: number; onOpenDevice: (connectorId: string) => void }) {
  const [filters, setFilters] = useUrlState(schema)
  const [rows, setRows] = useState<SetupSession[] | null>(null)
  const [total, setTotal] = useState(0)
  const [cursor, setCursor] = useState<number | null>(null)
  const [error, setError] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [opening, setOpening] = useState<string | null>(null)
  const [open, setOpen] = useState<SetupSession | null>(null)
  const [openError, setOpenError] = useState('')
  const returnRef = useRef<HTMLButtonElement | null>(null)
  const everAny = useRef(false)

  const load = useCallback(async ({ append = false, from }: { append?: boolean; from?: number } = {}) => {
    if (append) setLoadingMore(true)
    try {
      const response = await api<SessionList>(`/api/v1/provisioning/sessions${queryString({
        mine_only: filters.mine === '1' ? 'true' : undefined, q: filters.q.trim() || undefined, cursor: from, limit: 25,
      })}`)
      if (response.rows.length) everAny.current = true
      setRows((current) => (append ? [...new Map([...(current ?? []), ...response.rows].map((row) => [row.session_id, row])).values()] : response.rows))
      setTotal(response.filtered_total ?? response.rows.length)
      setCursor(response.next_cursor ?? null)
      setError(false)
    } catch {
      setError(true)
    } finally {
      setLoadingMore(false)
    }
  }, [filters.mine, filters.q])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 250)
    return () => window.clearTimeout(timer)
  }, [load, refreshKey])

  const openSetup = async (row: SetupSession, trigger: HTMLButtonElement) => {
    returnRef.current = trigger
    setOpening(row.session_id)
    setOpenError('')
    try {
      setOpen(await api<SetupSession>(`/api/v1/provisioning/sessions/${row.session_id}`))
    } catch {
      setOpenError(row.session_id)
    } finally {
      setOpening(null)
    }
  }

  const filtered = Boolean(filters.q.trim()) || filters.mine !== '1'
  // The section is hidden when nobody has set anything up yet.
  if (rows !== null && !rows.length && !filtered && !everAny.current && !error) return null

  return (
    <section className="setup-history" aria-labelledby="setup-history-title">
      <header className="setup-history-header">
        <h2 id="setup-history-title">{PAST_SETUPS.title}</h2>
        {rows && <p>{PAST_SETUPS.subtitle(total)}</p>}
      </header>
      <FilterBar
        search={{ value: filters.q, onChange: (q) => setFilters({ q }), label: 'Search past setups', placeholder: PAST_SETUPS.search }}
        actions={
          <SegmentedControl
            label="Whose setups"
            value={filters.mine}
            onChange={(mine) => setFilters({ mine })}
            options={[{ id: '1', label: PAST_SETUPS.mine }, { id: '0', label: PAST_SETUPS.everyone }]}
          />
        }
      />
      {error && !rows ? <ErrorState compact title={PAST_SETUPS.loadFailed} onRetry={() => load()} /> : rows === null ? <Skeleton variant="row" count={3} label="Loading past setups" /> : (
        <>
          {!rows.length && <p className="setup-history-empty">{PAST_SETUPS.noMatch}</p>}
          <ul className="setup-history-list">
            {rows.map((row) => {
              const place = setupPlace(row)
              const chip = chipFor(row.state)
              return (
                <li key={row.session_id}>
                  <button type="button" className="setup-history-row" aria-busy={opening === row.session_id} disabled={opening !== null} onClick={(event) => void openSetup(row, event.currentTarget)}>
                    <span className="setup-history-place">
                      <strong>{place?.name ?? PAST_SETUPS.noPlace}</strong>
                      {place && <DivisionTag division={place.division} />}
                    </span>
                    <span className="setup-history-meta">
                      {dayTime(row.created_at)} · {row.operator === username ? PAST_SETUPS.you : <span data-user-text>{row.operator}</span>}
                    </span>
                    <span className="setup-history-state">
                      {opening === row.session_id ? <Spinner size="sm" label="Opening" /> : null}
                      <StatusChip tone={chip.tone} label={chip.label} code={row.state} size="sm" />
                    </span>
                  </button>
                  {openError === row.session_id && <p className="setup-history-error" role="alert">{PAST_SETUPS.openFailed}</p>}
                </li>
              )
            })}
          </ul>
          {cursor !== null && (
            <div className="load-more">
              <Button busy={loadingMore} busyLabel="Loading…" onClick={() => load({ append: true, from: cursor })}>{PAST_SETUPS.showOlder}</Button>
              <small>{PAST_SETUPS.showing(rows.length, total)}</small>
            </div>
          )}
        </>
      )}
      {open && <SetupSummary session={open} username={username} returnFocusRef={returnRef} onClose={() => setOpen(null)} onOpenDevice={onOpenDevice} />}
    </section>
  )
}

function SetupSummary({ session, username, returnFocusRef, onClose, onOpenDevice }: {
  session: SetupSession
  username: string
  returnFocusRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
  onOpenDevice: (connectorId: string) => void
}) {
  const place = setupPlace(session)
  const ending = SETUP_ENDINGS[session.state]
  const word = setupState(session.state)
  const chip = chipFor(session.state)
  const connectorId = typeof session.result.connector_id === 'string' ? session.result.connector_id : ''
  const box = session.hardware_classification ? boxType(session.hardware_classification) : null
  return (
    <Sheet
      title={ending?.heading ?? (word.unknown ? 'Setup' : word.label)}
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      className="setup-summary-sheet"
      headerExtra={
        <div className="setup-summary-sub">
          {place ? <><strong>{place.name}</strong> <DivisionTag division={place.division} /></> : <span>{PAST_SETUPS.noPlace}</span>}
          <StatusChip tone={chip.tone} label={chip.label} code={session.state} size="sm" />
        </div>
      }
      footer={session.state === 'VERIFIED_ONLINE' && connectorId ? (
        <Button variant="primary" onClick={() => { onClose(); onOpenDevice(connectorId) }}>Open device</Button>
      ) : undefined}
    >
      <p>{ending?.sentence ?? word.explain}</p>
      <dl className="setup-summary-facts">
        <div><dt>Started</dt><dd>{dayTime(session.created_at)}</dd></div>
        <div><dt>Last change</dt><dd>{dayTime(session.updated_at)}</dd></div>
        <div><dt>Set up by</dt><dd>{session.operator === username ? PAST_SETUPS.you : <span data-user-text>{session.operator}</span>}</dd></div>
        {box && !box.unknown && <div><dt>Box</dt><dd>{box.label}</dd></div>}
        {session.bundle?.version && <div><dt>Software</dt><dd>version {session.bundle.version}</dd></div>}
      </dl>
      <TechnicalDetails>
        <dl className="setup-summary-facts">
          <div><dt>Box ID (MAC)</dt><dd>{session.hardware_mac || 'not read yet'}</dd></div>
          <div><dt>Setup ID</dt><dd>{session.session_id}</dd></div>
          <div><dt>State</dt><dd>{session.state} · {session.progress}%</dd></div>
          <div><dt>Device code</dt><dd>{session.device_id || '—'} · {session.zone_id || '—'} · {session.zone_name || '—'}</dd></div>
          {session.bundle?.bundle_id && <div><dt>Software bundle</dt><dd>{session.bundle.bundle_id}</dd></div>}
          {typeof session.result.error_code === 'string' && <div><dt>Error code</dt><dd>{session.result.error_code}</dd></div>}
          {(session.events ?? []).map((event, index) => (
            <div key={`${event.created_at}-${index}`}><dt>{dateTime(event.created_at)}</dt><dd>{event.state}</dd></div>
          ))}
        </dl>
        <p><a href={`/api/v1/provisioning/sessions/${session.session_id}/receipt`} download>Download for IT (JSON)</a></p>
      </TechnicalDetails>
    </Sheet>
  )
}

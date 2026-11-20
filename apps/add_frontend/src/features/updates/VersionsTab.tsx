// Software updates › Versions (08-advanced 2.5, today "Signed releases"):
// compact rows with the version, brand and where it runs; fingerprints and
// signing keys under Technical details; Withdraw moves into the ⋯ menu and
// keeps its note and password (D-2..D-8 wait for the owner).
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, queryString } from '../../api'
import { oneOf, text, useUrlState } from '../../lib/urlState'
import { dateTime, day } from '../../lib/time'
import type { Device, FirmwareRelease } from '../../types'
import { ActionMenu, Badge, Button, Callout, Dialog, EmptyState, ErrorState, FilterBar, Skeleton, StatusChip, TechnicalDetails } from '../../ui'
import type { Toast } from '../../ui/Toast'
import { FAMILY_NAMES, SOFTWARE_PAGE, newestVersion, updateActionError, updateToast } from '../../words/software'
import { hilScopeLabel } from '../hilTargets'
import { devicesOnVersion, fileSize, isAbort, releaseFamily, type ReleaseResponse } from './model'
import type { Catalog } from './UpdatesPage'

const SHOW = ['current', 'all', 'test', 'withdrawn'] as const
const BRANDS = ['any', 'zkt', 'hikvision'] as const
const schema = { show: oneOf(SHOW, 'current'), brand: oneOf(BRANDS, 'any'), q: text() }
const SHOW_LABEL: Record<(typeof SHOW)[number], string> = { current: 'In use', all: 'All versions', test: 'Test devices only', withdrawn: 'Withdrawn' }

const params = (show: (typeof SHOW)[number]) =>
  show === 'current' ? { scope: 'current' } : show === 'test' ? { state: 'HIL_ONLY' } : show === 'withdrawn' ? { state: 'REVOKED' } : {}

export function VersionsTab({ devices, catalog, refreshKey, toast, onChanged }: {
  devices: Device[]
  catalog: Catalog | null
  refreshKey: number
  toast: Toast
  onChanged: () => Promise<void>
}) {
  const [filters, setFilters] = useUrlState(schema)
  const [rows, setRows] = useState<FirmwareRelease[] | null>(null)
  const [cursor, setCursor] = useState<number | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [withdraw, setWithdraw] = useState<FirmwareRelease | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const load = useCallback(async ({ append = false, from }: { append?: boolean; from?: number } = {}) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setLoading(true)
    try {
      const response = await api<ReleaseResponse>(`/api/v1/firmware/releases${queryString({
        q: filters.q.trim() || undefined, family: filters.brand === 'any' ? undefined : filters.brand, cursor: from, limit: 24, ...params(filters.show),
      })}`, { signal: controller.signal })
      setRows((current) => (append ? [...new Map([...(current ?? []), ...response.rows].map((row) => [row.release_id, row])).values()] : response.rows))
      setCursor(response.next_cursor)
      setTotal(response.filtered_total)
      setError(false)
    } catch (reason) {
      if (!isAbort(reason)) setError(true)
    } finally {
      if (abortRef.current === controller) setLoading(false)
    }
  }, [filters])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 250)
    return () => window.clearTimeout(timer)
  }, [load, refreshKey])
  useEffect(() => () => abortRef.current?.abort(), [])

  const filtered = Boolean(filters.q.trim()) || filters.brand !== 'any'
  const clear = () => setFilters({ q: '', brand: 'any', show: 'current' })

  return (
    <section className="updates-versions" aria-labelledby="updates-versions-title">
      <header className="updates-list-header">
        <div>
          <h2 id="updates-versions-title">{SOFTWARE_PAGE.versionsTitle}</h2>
          <p>{SOFTWARE_PAGE.versionsIntro}</p>
        </div>
      </header>
      <FilterBar
        search={{ value: filters.q, onChange: (q) => setFilters({ q }), label: 'Search versions', placeholder: 'Search by version, e.g. 2.7.8' }}
        chipsLabel="Show versions"
        chips={SHOW.map((id) => ({ id, label: SHOW_LABEL[id] }))}
        chipValue={filters.show}
        onChipChange={(id) => setFilters({ show: id as (typeof SHOW)[number] })}
        active={filters.brand !== 'any' ? [{ id: 'brand', label: FAMILY_NAMES[filters.brand], onRemove: () => setFilters({ brand: 'any' }) }] : []}
        onClearAll={filtered || filters.show !== 'current' ? clear : undefined}
        actions={
          <label className="updates-brand">
            <span className="sr-only">Machine brand</span>
            <select value={filters.brand} onChange={(event) => setFilters({ brand: event.target.value as (typeof BRANDS)[number] })}>
              <option value="any">Both brands</option>
              <option value="zkt">{FAMILY_NAMES.zkt}</option>
              <option value="hikvision">{FAMILY_NAMES.hikvision}</option>
            </select>
          </label>
        }
      />
      {error && rows?.length ? <Callout tone="warning" role="status" action={<Button size="sm" onClick={() => load()}>Try again</Button>}>Couldn't refresh the versions. Showing the list from before.</Callout> : null}
      {error && !rows?.length ? (
        <ErrorState title={SOFTWARE_PAGE.catalogFailed} onRetry={() => load()} />
      ) : rows === null ? (
        <Skeleton variant="row" count={4} label="Loading versions" />
      ) : (
        <ul className={`updates-version-list${loading ? ' is-refreshing' : ''}`}>
          {rows.map((release) => <VersionRow key={release.release_id} release={release} devices={devices} catalog={catalog} onWithdraw={() => setWithdraw(release)} />)}
          {!rows.length && (
            filtered ? <EmptyState title={SOFTWARE_PAGE.noVersionsMatch} action={<Button onClick={clear}>Clear all</Button>} />
              : filters.show === 'current' ? <EmptyState title={SOFTWARE_PAGE.noVersionsInUse} action={<Button onClick={() => setFilters({ show: 'all' })}>Show all versions</Button>} />
                : <EmptyState title={SOFTWARE_PAGE.noVersions}>{SOFTWARE_PAGE.noVersionsHelp}</EmptyState>
          )}
        </ul>
      )}
      {cursor !== null && rows && (
        <div className="load-more">
          <Button busy={loading} busyLabel="Loading…" onClick={() => load({ append: true, from: cursor })}>Show more versions</Button>
          <small>Showing {rows.length} of {total}</small>
        </div>
      )}
      {withdraw && (
        <WithdrawDialog
          release={withdraw}
          onClose={() => setWithdraw(null)}
          onDone={async () => {
            toast.notice(updateToast.withdrawn(withdraw.version))
            setWithdraw(null)
            await Promise.all([load(), onChanged()])
          }}
        />
      )}
    </section>
  )
}

function VersionRow({ release, devices, catalog, onWithdraw }: { release: FirmwareRelease; devices: Device[]; catalog: Catalog | null; onWithdraw: () => void }) {
  const family = releaseFamily(release)
  const on = devicesOnVersion(release, devices)
  const newest = release.state === 'AVAILABLE' && newestVersion(catalog?.rows, family) === release.version
  const passed = release.hil_schedule?.counts.PASSED
  const testing = release.hil_schedule ? release.hil_schedule.trial_target_count ?? release.hil_schedule.denominator : release.hil_targets?.length
  return (
    <li className="updates-version-row" data-state={release.state}>
      <div className="updates-version-main">
        <strong className="updates-version-number">{release.version}</strong>
        <span className="updates-version-brand" data-brand>{FAMILY_NAMES[family]}</span>
        {release.state === 'AVAILABLE' && <StatusChip tone="working" label="For all devices" size="sm" labelPrefix={null} code={release.state} />}
        {release.state === 'HIL_ONLY' && <StatusChip tone="info" label="Test devices only" size="sm" labelPrefix={null} code={release.state} />}
        {release.state === 'REVOKED' && (
          <StatusChip tone="neutral" label={release.revoked_at ? `Withdrawn ${day(release.revoked_at)}` : 'Withdrawn'} size="sm" labelPrefix={null} code={release.state} />
        )}
        {newest && <Badge tone="info">Newest</Badge>}
      </div>
      <p className="updates-version-meta">
        On {on.total === 1 ? '1 device' : `${on.total} devices`}{on.total ? ` (${on.life} Life · ${on.health} Health)` : ''} · Released {day(release.published_at)}
        {release.state === 'HIL_ONLY' && typeof testing === 'number' && ` · Being tested on ${testing} device${testing === 1 ? '' : 's'}${typeof passed === 'number' ? ` · ${passed} passed` : ''}`}
        {release.state === 'REVOKED' && release.revoked_by && <> · by <span data-user-text>{release.revoked_by}</span></>}
      </p>
      {release.state !== 'REVOKED' && (
        <ActionMenu label={`More actions for version ${release.version}`} items={[{ id: 'withdraw', label: 'Withdraw version…', danger: true, onSelect: onWithdraw }]} />
      )}
      <TechnicalDetails>
        <dl className="updates-facts">
          <div><dt>Version ID</dt><dd>{release.release_id}</dd></div>
          <div><dt>Code version</dt><dd>{release.git_sha}</dd></div>
          <div><dt>File fingerprint (SHA-256)</dt><dd>{release.image_sha256}</dd></div>
          <div><dt>App fingerprint (SHA-256)</dt><dd>{release.application_sha256 || 'Unavailable'}</dd></div>
          <div><dt>File size</dt><dd>{fileSize(release.image_size)} ({release.image_size.toLocaleString('en-PK')} bytes)</dd></div>
          <div><dt>Storage layout</dt><dd>{release.partition_layout}</dd></div>
          <div><dt>Signing key</dt><dd>{release.signing_key_id}</dd></div>
          <div><dt>Published</dt><dd>{dateTime(release.published_at)}</dd></div>
          {release.revoked_at && <div><dt>Withdrawn</dt><dd>{dateTime(release.revoked_at)} by {release.revoked_by || 'an administrator'}</dd></div>}
          {release.state === 'HIL_ONLY' && <div><dt>Test plan</dt><dd>{hilScopeLabel(release)}</dd></div>}
        </dl>
      </TechnicalDetails>
    </li>
  )
}

// Same request as before: note (10+ letters) and password, POST …/revoke.
function WithdrawDialog({ release, onClose, onDone }: { release: FirmwareRelease; onClose: () => void; onDone: () => Promise<void> }) {
  const [note, setNote] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const close = () => {
    setNote('')
    setPassword('')
    setError('')
    onClose()
  }
  const submit = async () => {
    if (note.trim().length < 10 || !password || busy) return
    setBusy(true)
    setError('')
    try {
      await api(`/api/v1/firmware/releases/${release.release_id}/revoke`, { method: 'POST', body: JSON.stringify({ reason: note.trim(), password }) })
      setPassword('')
      setNote('')
      await onDone()
    } catch (reason) {
      setPassword('')
      setError(updateActionError(reason instanceof Error ? reason.message : '', "Couldn't withdraw this version. Try again."))
    } finally {
      setBusy(false)
    }
  }
  const length = note.trim().length
  return (
    <Dialog
      titleId="withdraw-version-title"
      title={`Withdraw version ${release.version}`}
      description="No device will be offered this version again. Running updates that use it will pause."
      onClose={close}
    >
      <div className="dialog-body">
        <label>
          Why are you withdrawing it? (at least 10 letters, saved in the log)
          <textarea value={note} onChange={(event) => setNote(event.target.value)} maxLength={200} rows={3} />
          <small className="updates-counter" aria-live="polite">{length < 10 ? `${10 - length} more letter${10 - length === 1 ? '' : 's'} needed` : `${length} of 200`}</small>
        </label>
        <label>
          Your password
          <input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        {error && <Callout tone="danger" role="alert">{error}</Callout>}
        <div className="dialog-actions">
          <Button onClick={close}>Keep this version</Button>
          <Button variant="danger" busy={busy} busyLabel="Withdrawing…" disabled={length < 10 || !password} onClick={() => submit()}>Withdraw version</Button>
        </div>
      </div>
    </Dialog>
  )
}

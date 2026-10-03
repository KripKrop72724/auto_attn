import { useEffect, useState } from 'react'
import { api } from '../api'
import { humanizeStatus } from '../status'

type Work = {
  id: number; state: string; reason_code: string; owner: string;
  updated_at: string; next_attempt_at: string | null;
}
export type CustodySnapshot = {
  connector_id: string; enabled: boolean; sampled_at: string;
  oracle_completion: 'NOT_ASSERTED'; missing_processing_obligation: boolean;
  counts: { state: string; owner: string; count: number }[];
  rows: Work[]; next_cursor: number | null;
}
const states: Record<string, string> = {
  PENDING: 'Awaiting inspection', WAIT_FRAGMENTS: 'Waiting for packet fragments',
  WAIT_PROFILE: 'Waiting for profile qualification', WAIT_SOURCE: 'Waiting for source evidence',
  HELD_EXCEPTION: 'Preserved for review', RETRY_SYSTEM: 'System retry pending',
  SOURCE_ASSOCIATED: 'Source linked',
}
const owners: Record<string, string> = {
  ADD_PROTOCOL: 'Protocol review', ADD_EVIDENCE_REVIEW: 'Evidence review',
  ADD_OPERATIONS: 'ADD operations', ADD_RECONCILIATION: 'Reconciliation',
}
const label = (state: string) => states[state] || humanizeStatus(state)
const owner = (value: string) => owners[value] || humanizeStatus(value)
const date = (value: string) => new Date(value).toLocaleString('en-GB', { timeZone: 'Asia/Karachi' })
const timestamp = (value: unknown): value is string => typeof value === 'string' && Number.isFinite(Date.parse(value))
function valid(value: CustodySnapshot, connector: string) {
  return value?.connector_id === connector && typeof value.enabled === 'boolean' && timestamp(value.sampled_at)
    && Date.parse(value.sampled_at) <= Date.now() + 1000
    && value.oracle_completion === 'NOT_ASSERTED' && typeof value.missing_processing_obligation === 'boolean'
    && Array.isArray(value.counts) && value.counts.length <= 100 && value.counts.every(row =>
      typeof row.state === 'string' && typeof row.owner === 'string' && Number.isSafeInteger(row.count) && row.count >= 0)
    && Array.isArray(value.rows) && value.rows.length <= 10 && value.rows.every(row =>
      Number.isSafeInteger(row.id) && typeof row.state === 'string' && typeof row.reason_code === 'string'
      && typeof row.owner === 'string' && timestamp(row.updated_at)
      && (row.next_attempt_at === null || timestamp(row.next_attempt_at)))
}

export function ZktCustodyStatus({ connectorId, revision }: { connectorId: string; revision: number }) {
  const [snapshot, setSnapshot] = useState<CustodySnapshot | null>(null)
  const [failureFor, setFailureFor] = useState<string | null>(null)
  const [refresh, setRefresh] = useState(0)
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const poll = window.setInterval(() => setRefresh(value => value + 1), 30_000)
    const clock = window.setInterval(() => setNow(Date.now()), 1000)
    return () => { window.clearInterval(poll); window.clearInterval(clock) }
  }, [])
  useEffect(() => {
    let active = true
    const controller = new AbortController()
    const deadline = window.setTimeout(() => controller.abort(), 10_000)
    void api<CustodySnapshot>(`/api/v1/devices/${encodeURIComponent(connectorId)}/zkt-custody?limit=10`, { signal: controller.signal })
      .then(value => {
        if (!active || controller.signal.aborted) return
        if (!valid(value, connectorId)) throw new Error('Custody snapshot is unverified')
        setSnapshot(prior => prior?.connector_id === connectorId && Date.parse(prior.sampled_at) > Date.parse(value.sampled_at) ? prior : value)
        setFailureFor(null)
      })
      .catch(() => { if (active) setFailureFor(connectorId) })
      .finally(() => window.clearTimeout(deadline))
    return () => { active = false; window.clearTimeout(deadline); controller.abort() }
  }, [connectorId, revision, refresh])
  const data = snapshot?.connector_id === connectorId ? snapshot : null
  const failed = failureFor === connectorId
  const age = data ? now - Date.parse(data.sampled_at) : NaN
  const fresh = !failed && age >= -1000 && age <= 45_000
  const heading = failed ? 'Custody status unavailable' : !data ? 'Loading custody status'
    : !fresh ? 'Custody evidence is stale' : !data.enabled ? 'Journal custody is not enabled'
      : data.missing_processing_obligation ? 'Preserved records need processing repair' : 'ADD custody processing'
  return <article className="detail-card wide" aria-label="ZKT ADD custody">
    <div className="detail-title"><div><p className="eyebrow">ADD CUSTODY</p><h3>{heading}</h3></div>
      <button className="text-button" onClick={() => setRefresh(value => value + 1)}>Refresh custody</button></div>
    <p>Custody confirms preservation in ADD. Identity resolution, source coverage and Oracle completion require their own evidence.</p>
    {!data ? <p>Processing counts are unknown until ADD returns a verified snapshot.</p> : <>
      {!fresh && <p>Values below are the last report and do not confirm current processing status.</p>}
      <dl>
        <div><dt>Evidence sampled (Pakistan)</dt><dd>{date(data.sampled_at)} · {Math.max(0, Math.floor(age / 1000))} seconds ago</dd></div>
        <div><dt>Journal receipt path</dt><dd>{data.enabled ? 'Enabled' : 'Not enabled'}</dd></div>
        <div><dt>Missing processing obligations</dt><dd>{data.missing_processing_obligation ? 'Detected — needs ADD operations review' : 'None detected in this snapshot'}</dd></div>
        <div><dt>Oracle completion</dt><dd>Not established by custody</dd></div>
      </dl>
      <h4>Processing groups</h4>
      <p>A group can contain several observations. These counts are not punch totals.</p>
      <dl>{data.counts.map(row => <div key={`${row.state}:${row.owner}`}>
        <dt>{label(row.state)} · {owner(row.owner)}</dt><dd>{row.count.toLocaleString()}</dd>
      </div>)}</dl>
      {!data.counts.length && <p>No journal processing groups recorded.</p>}
      {!!data.rows.length && <><h4>Latest {data.rows.length} processing {data.rows.length === 1 ? 'group' : 'groups'}</h4>
        {data.rows.map(row => <details className="custody-work-group" key={row.id}>
          <summary>{label(row.state)} · {humanizeStatus(row.reason_code)}</summary>
          <dl><div><dt>Responsible team</dt><dd>{owner(row.owner)}</dd></div>
            <div><dt>Last updated (Pakistan)</dt><dd>{date(row.updated_at)}</dd></div>
            <div><dt>Next inspection</dt><dd>{row.next_attempt_at ? date(row.next_attempt_at) : row.state === 'SOURCE_ASSOCIATED' ? 'Source association complete' : 'Waiting for relevant evidence or review'}</dd></div></dl>
        </details>)}</>}
    </>}
  </article>
}

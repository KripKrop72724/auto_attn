// Recover attendance for this machine (04-device Tools 2): the preflight says
// whether it can start; Start stays one click (owner decision) and sends
// exactly what Recover attendance sends today.
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api'
import { idempotency } from '../../lib/idempotency'
import { clock } from '../../lib/time'
import { Button, ErrorState, Skeleton, TechnicalDetails } from '../../ui'
import { RECOVER_BLOCKERS, RECOVER_WAITS, TOOLS_WORDS, type DeviceDetail } from '../../words/device'
import { recoverJobStatus } from '../../words/recover'
import { isHikvision } from './deviceNaming'
import { usePreflight } from './useDeviceData'

interface StartedJob { job_id: string; status: string; created_at?: string | null }

export function RecoverAttendanceAction({ device, custodyRevision }: { device: DeviceDetail; custodyRevision: number }) {
  const preflight = usePreflight(device.connector_id, true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [started, setStarted] = useState<{ job: StartedJob; at: string } | null>(null)
  const job = useQuery({
    queryKey: ['reconciliation', started?.job.job_id],
    queryFn: ({ signal }) => api<StartedJob>(`/api/v1/reconciliations/${encodeURIComponent(started?.job.job_id || '')}`, { signal }),
    enabled: Boolean(started?.job.job_id),
    initialData: started?.job,
    retry: false,
  })
  const { refetch } = job
  useEffect(() => {
    if (started) void refetch()
  }, [custodyRevision, refetch, started])

  const start = async () => {
    setBusy(true)
    setError('')
    try {
      const answer = await api<StartedJob>(`/api/v1/devices/${encodeURIComponent(device.connector_id)}/reconciliations/full-history`, {
        method: 'POST',
        body: JSON.stringify({ idempotency_key: idempotency('full-history'), scope: 'ALL_RECORDS' }),
      })
      setStarted({ job: answer, at: new Date().toISOString() })
      void preflight.refetch()
    } catch {
      setError(TOOLS_WORDS.startFailed)
    } finally {
      setBusy(false)
    }
  }

  const follow = `/recover?tab=recoveries&device=${encodeURIComponent(device.zone_id || device.connector_id)}`
  let body: React.ReactNode
  if (started) {
    body = (
      <>
        <p role="status">{TOOLS_WORDS.startedAt(clock(started.at))} {job.data?.status ? `${recoverJobStatus(job.data.status).label}.` : ''}</p>
        <p><a href={follow}>{TOOLS_WORDS.follow}</a></p>
      </>
    )
  } else if (preflight.isPending) {
    body = <Skeleton width="10rem" label={TOOLS_WORDS.checking} />
  } else if (preflight.isError) {
    body = <ErrorState compact title={TOOLS_WORDS.preflightError} onRetry={() => preflight.refetch()} />
  } else {
    const data = preflight.data
    const hard = data?.hard_blockers || []
    const waitable = data?.waitable_blockers || []
    const running = hard.find((blocker) => blocker.code === 'RECONCILIATION_ACTIVE' || blocker.code === 'RECONCILIATION_HELD')
    const ready = Boolean(data?.ready_now) && !hard.length
    const first = hard[0] || waitable[0]
    body = (
      <>
        {ready && <p>{TOOLS_WORDS.ready}</p>}
        {running && <p>{TOOLS_WORDS.running} <a href={follow}>{TOOLS_WORDS.openIt}</a></p>}
        {!ready && !running && hard[0] && <p>{RECOVER_BLOCKERS[hard[0].code] ?? TOOLS_WORDS.cantStart}</p>}
        {!ready && !running && !hard[0] && waitable[0] && <p>{TOOLS_WORDS.waitFor(RECOVER_WAITS[waitable[0].code] ?? 'the device is ready')}</p>}
        <div className="dv-tool-actions">
          <Button variant="primary" busy={busy} busyLabel={TOOLS_WORDS.starting} disabled={!ready} onClick={() => void start()}>{TOOLS_WORDS.start}</Button>
        </div>
        {first && <TechnicalDetails><p>{first.code}: {first.message}</p></TechnicalDetails>}
      </>
    )
  }
  return (
    <div className="dv-action">
      <p>{TOOLS_WORDS.recoverText}</p>
      {body}
      {error && <p className="field-error" role="alert">{error}</p>}
      {isHikvision(device) && <p className="dv-muted">{TOOLS_WORDS.hikChoices}</p>}
    </div>
  )
}

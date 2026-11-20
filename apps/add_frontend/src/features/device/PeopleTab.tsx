// People on this machine (04-device §5): how many, when the list was last
// read, whether changes are allowed, and the way to manage them.
import { ago, clock } from '../../lib/time'
import { formatNumber } from '../../lib/format'
import { Button, Skeleton } from '../../ui'
import { PEOPLE_WORDS, TOOLS_WORDS, type DeviceDetail } from '../../words/device'
import { writesOff } from '../../words/people'
import { isHikvision } from './deviceNaming'
import { RefreshPeopleAction } from './RefreshPeopleAction'

export function PeopleTab({ device, revision, onManageUsers, onRefreshed }: {
  device: DeviceDetail
  revision: number
  onManageUsers: () => void
  onRefreshed: () => Promise<void> | void
}) {
  const zkt = device.zkt
  const hikvision = isHikvision(device)
  const count = zkt?.user_count
  const lastRead = zkt?.identity_snapshot_observed_at
  const allowed = zkt?.certification_state === 'CERTIFIED' && zkt.snapshot_complete
  const reason = zkt?.writes_disabled_reason ? writesOff(zkt.writes_disabled_reason) : null
  const lease = device.active_lease && ['GRANTING', 'ACTIVE', 'REVOKING', 'OVERDUE'].includes(String(device.active_lease.state).toUpperCase()) ? device.active_lease : null
  return (
    <section className="dv-card dv-people" aria-labelledby="dv-people-title">
      <div className="dv-people-facts">
        <h3 id="dv-people-title">{PEOPLE_WORDS.title}</h3>
        <p className="dv-people-count">
          {count == null ? (lastRead ? <Skeleton width="3rem" label={PEOPLE_WORDS.counting} /> : PEOPLE_WORDS.notRead) : formatNumber(count)}
        </p>
        {lastRead && <p className="dv-muted">{PEOPLE_WORDS.lastRead(ago(lastRead))}{zkt?.identity_snapshot_stable === false ? ` ${PEOPLE_WORDS.stillChecking}` : ''}</p>}
        {!hikvision && zkt && <p>{allowed ? PEOPLE_WORDS.allowed : PEOPLE_WORDS.paused(reason && !reason.unknown ? reason.sentence : PEOPLE_WORDS.pausedFallback)}</p>}
        <p className="dv-muted">{hikvision ? PEOPLE_WORDS.manageHik : PEOPLE_WORDS.manageZkt}</p>
        {lease && <p>{PEOPLE_WORDS.adminUntil(lease.expires_at ? clock(lease.expires_at) : '…')}</p>}
      </div>
      <div className="dv-people-actions">
        <Button variant="primary" icon="users" onClick={onManageUsers}>{PEOPLE_WORDS.open}</Button>
        <h4 className="dv-action-title">{TOOLS_WORDS.refreshTitle}</h4>
        <RefreshPeopleAction device={device} revision={revision} onRefreshed={onRefreshed} showText={false} />
      </div>
    </section>
  )
}

import { lazy } from 'react'
import { useAppData } from '../AppData'

const UsersView = lazy(() => import('../features/UsersWorkspace').then((module) => ({ default: module.UsersView })))

// People (`/people`, `/people/:deviceId`).
export default function PeopleRoute() {
  const app = useAppData()
  return <UsersView devices={app.devices} selectedDeviceId={app.selectedDeviceId} onSelectDevice={app.selectUserDevice} revision={app.revisions.users + app.revisions.identity + app.revisions.command} toast={app.toast} refreshFleet={app.refreshFleet} />
}

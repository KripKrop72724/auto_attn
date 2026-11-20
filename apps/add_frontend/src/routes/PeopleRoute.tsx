import { lazy } from 'react'
import { useAppData } from '../AppData'

const UsersView = lazy(() => import('../features/UsersWorkspace').then((module) => ({ default: module.UsersView })))

// People (`/people`, `/people/:deviceId`). The page gets the device list's
// loaded / error flags so it never says "no device matches" before the answer.
export default function PeopleRoute() {
  const app = useAppData()
  return <UsersView devices={app.devices} devicesLoaded={app.devicesLoaded} devicesError={app.devicesError} selectedDeviceId={app.selectedDeviceId} onSelectDevice={app.selectUserDevice} revision={app.revisions.users + app.revisions.identity + app.revisions.command} toast={app.toast} refreshFleet={app.refreshFleet} />
}

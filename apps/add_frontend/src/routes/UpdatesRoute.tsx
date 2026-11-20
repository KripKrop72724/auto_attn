import { lazy } from 'react'
import { useLocation } from 'react-router-dom'
import { useAppData } from '../AppData'
import { updatesSection } from '../routing'

const FirmwareView = lazy(() => import('../features/Firmware').then((module) => ({ default: module.FirmwareView })))

// Software updates (`/updates?tab=overview | updates | versions`). The page
// moves between its own tabs; old tab names are read by routing.ts.
export default function UpdatesRoute() {
  const app = useAppData()
  const location = useLocation()
  return (
    <FirmwareView
      devices={app.devices}
      devicesLoaded={app.devicesLoaded}
      devicesError={app.devicesError}
      revision={app.revisions.firmware}
      toast={app.toast}
      section={updatesSection(location.search)}
      onRefreshDevices={app.refreshFleet}
    />
  )
}

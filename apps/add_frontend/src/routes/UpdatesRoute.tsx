import { lazy } from 'react'
import { useLocation } from 'react-router-dom'
import { useAppData } from '../AppData'
import { firmwareSectionForUpdates, updatesSection, updatesSectionForFirmware } from '../routing'

const FirmwareView = lazy(() => import('../features/Firmware').then((module) => ({ default: module.FirmwareView })))

// Software updates (`/updates?tab=overview | updates | versions`). Today's
// firmware page keeps its own tab names; this module translates them.
export default function UpdatesRoute() {
  const app = useAppData()
  const location = useLocation()
  const section = firmwareSectionForUpdates(updatesSection(location.search))
  return (
    <FirmwareView
      devices={app.devices}
      revision={app.revisions.firmware}
      toast={app.toast}
      section={section}
      onSection={(next) => app.navigate(next === 'prepare' ? '/setup' : `/updates?tab=${updatesSectionForFirmware(next)}`)}
    />
  )
}

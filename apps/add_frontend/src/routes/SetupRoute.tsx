import { lazy } from 'react'
import { useAppData } from '../AppData'
import { updatesSectionForFirmware } from '../routing'

const FirmwareProvisioning = lazy(() => import('../features/FirmwareProvisioning'))

// Set up a device (`/setup`). Its other tabs lead to Software updates.
export default function SetupRoute() {
  const app = useAppData()
  return (
    <FirmwareProvisioning
      revision={app.revisions.provisioning}
      toast={app.toast}
      username={app.username}
      onSection={(next) => app.navigate(next === 'prepare' ? '/setup' : `/updates?tab=${updatesSectionForFirmware(next)}`)}
    />
  )
}

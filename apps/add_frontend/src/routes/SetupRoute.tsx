import { lazy } from 'react'
import { useAppData } from '../AppData'

const SetupPage = lazy(() => import('../features/setup/SetupPage'))

// Set up a device (`/setup`; old `/firmware?tab=prepare` links redirect here).
export default function SetupRoute() {
  const app = useAppData()
  return (
    <SetupPage
      revision={app.revisions.provisioning}
      toast={app.toast}
      username={app.username}
      onOpenDevice={(connectorId) => app.navigate(`/devices/${encodeURIComponent(connectorId)}`)}
    />
  )
}

// The device view's entry point. App.tsx lazy-loads `DeviceDrawer` from this
// file and passes the same props as before, so the frame needs no change; the
// view itself lives in features/device (04-device DEV-5 … DEV-11).
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from '../data'
import { DeviceView, type DeviceViewProps } from './device/DeviceView'

export type DeviceDrawerProps = DeviceViewProps

export function DeviceDrawer(props: DeviceDrawerProps) {
  // main.tsx already provides this client; the provider here keeps the view
  // self-contained for tests and for any caller outside the dashboard frame.
  return <QueryClientProvider client={queryClient}><DeviceView {...props} /></QueryClientProvider>
}

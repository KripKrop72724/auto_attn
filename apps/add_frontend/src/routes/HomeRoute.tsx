import { lazy } from 'react'

const HomeView = lazy(() => import('../features/Home').then((module) => ({ default: module.HomeView })))

// Home (`/home`, the default page; 03-home-devices §1). The page reads the app
// data itself, so this module only mounts it.
export default function HomeRoute() {
  return <HomeView />
}

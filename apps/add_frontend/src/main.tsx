import React, { lazy, Suspense } from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { queryClient } from './data'
import { isPublicStatusPath } from './publicStatus'
import './styles.css'

// The public status page needs no sign-in, so it never mounts the dashboard.
const PublicStatus = lazy(() => import('./features/PublicStatus'))

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {isPublicStatusPath(window.location.pathname)
      ? <Suspense fallback={null}><PublicStatus /></Suspense>
      : <QueryClientProvider client={queryClient}><App /></QueryClientProvider>}
  </React.StrictMode>,
)

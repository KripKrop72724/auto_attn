import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { ClassicBanner } from './ClassicBanner'
import { queryClient } from './data'
import './styles.css'
import './classic.css'

// The old screens. The public /status page is served only by the new app.
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <div className="classic-frame">
      <ClassicBanner />
      <QueryClientProvider client={queryClient}><App /></QueryClientProvider>
    </div>
  </React.StrictMode>,
)

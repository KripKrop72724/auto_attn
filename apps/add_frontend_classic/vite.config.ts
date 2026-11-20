import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The old screens, frozen for one week after the revamp release.
// Served by the same nginx under /classic/; /api and /events stay on the same
// origin, so the sign-in cookie is shared with the new screens.
export default defineConfig({
  base: '/classic/',
  plugins: [react()],
  build: {
    chunkSizeWarningLimit: 350,
    rolldownOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('react-router') || id.includes('@remix-run')) return 'router'
          if (id.includes('@tanstack')) return 'query'
          if (id.includes('react-hook-form') || id.includes('/zod/')) return 'forms'
          if (id.includes('/react/') || id.includes('/react-dom/') || id.includes('/scheduler/')) return 'react-core'
          return 'vendor'
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8096',
      '/events': 'http://localhost:8096',
      '/health': 'http://localhost:8096',
    },
  },
})

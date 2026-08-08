import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
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
  test: {
    environment: 'jsdom',
    globals: false,
    exclude: ['tests/e2e/**', 'node_modules/**', 'dist/**'],
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8096',
      '/events': 'http://localhost:8096',
      '/health': 'http://localhost:8096',
    },
  },
})

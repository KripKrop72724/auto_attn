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
    // These suites exercise lazy route loading and share process-wide browser
    // stubs. Serial files keep CI timing deterministic as the control surface grows.
    fileParallelism: false,
    exclude: ['tests/e2e/**', 'node_modules/**', 'dist/**'],
  },
  // `vite preview` (the e2e server) sends the production security headers
  // from security-headers.conf, so a Content Security Policy break fails the
  // browser tests instead of the live site (FND-1). The dev server stays open.
  preview: {
    headers: {
      'X-Content-Type-Options': 'nosniff',
      'X-Frame-Options': 'DENY',
      'Referrer-Policy': 'same-origin',
      'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
      'Content-Security-Policy': "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
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

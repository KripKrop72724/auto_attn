import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
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

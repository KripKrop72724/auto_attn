import { defineConfig, devices } from '@playwright/test'

const sweeps = /(guards|plain-words|visual-tour)\.spec\.ts$/

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'pnpm run preview:test',
    url: 'http://127.0.0.1:4173/',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
  // The page sweeps and the screenshot tour run on one phone and one desktop
  // size only (DLV-3); every other project runs the page specs.
  projects: [
    // WebKit on a CPU-only runner draws slowly; several WebKit browsers at once
    // stall each other for seconds and time out. Run its tests one at a time,
    // first, so they overlap the Chromium projects instead of ending the run
    // with every worker on WebKit.
    { name: 'webkit-1280', workers: 1, testIgnore: sweeps, use: { ...devices['Desktop Safari'], viewport: { width: 1280, height: 900 } } },
    { name: 'mobile-320', testIgnore: sweeps, use: { ...devices['Desktop Chrome'], viewport: { width: 320, height: 720 } } },
    { name: 'mobile-390', testIgnore: sweeps, use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } } },
    // A real phone: touch, mobile viewport rules (FND-1). Runs the frame specs and the sweeps.
    { name: 'phone-390-touch', testMatch: /(frame|guards|plain-words|visual-tour)\.spec\.ts$/, use: { ...devices['Pixel 5'], viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
    { name: 'tablet-768', testIgnore: sweeps, use: { ...devices['Desktop Chrome'], viewport: { width: 768, height: 1024 } } },
    { name: 'desktop-1280', testIgnore: sweeps, use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 900 } } },
    { name: 'desktop-1440', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } } },
    { name: 'wide-1600', testIgnore: sweeps, use: { ...devices['Desktop Chrome'], viewport: { width: 1600, height: 1000 } } },
    { name: 'firefox-1280', testIgnore: sweeps, use: { ...devices['Desktop Firefox'], viewport: { width: 1280, height: 900 } } },
  ],
})

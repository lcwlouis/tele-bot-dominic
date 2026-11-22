import { defineConfig } from '@playwright/test';

/*
  Screenshot baselines of the built app, served by `vite preview` under Dom's real CSP.
  Baselines are made on Linux in the Playwright container (see README), the same image CI runs,
  so fonts render identically. Snapshot names carry no platform suffix for that reason.
*/
export default defineConfig({
  testDir: 'tests',
  snapshotPathTemplate: '{testDir}/screenshots/{arg}{ext}',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  expect: {
    toHaveScreenshot: { maxDiffPixelRatio: 0.002, animations: 'disabled', caret: 'hide' },
  },
  use: {
    baseURL: 'http://127.0.0.1:4173',
    browserName: 'chromium',
    reducedMotion: 'reduce',
    timezoneId: 'UTC',
    locale: 'en-GB',
  },
  webServer: {
    command: 'npm run preview -- --host 127.0.0.1',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
  },
});

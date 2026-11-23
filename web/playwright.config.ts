import { defineConfig } from "@playwright/test";

import { PREVIEW_PORT as PORT, PREVIEW_TOKEN as TOKEN } from "./screens/preview";

// Screens run against tests/web_preview.py: the real dashboard app (auth, CSP) with a fictional Dom.

export default defineConfig({
  testDir: "./screens",
  snapshotPathTemplate: "{testDir}/baseline/{arg}{ext}",
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    extraHTTPHeaders: { Authorization: "Bearer " + TOKEN },
    browserName: "chromium",
  },
  expect: { toHaveScreenshot: { animations: "disabled", caret: "hide", maxDiffPixelRatio: 0.01 } },
  webServer: {
    command: `uv run python tests/web_preview.py --port ${PORT}`,
    cwd: "..",
    url: `http://127.0.0.1:${PORT}/app`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});

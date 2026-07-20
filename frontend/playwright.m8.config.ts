import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e-release",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 600_000,
  reporter: "line",
  outputDir:
    process.env.M8_PLAYWRIGHT_OUTPUT_DIR ?? "test-results/m8-release-list-only",
  use: {
    baseURL: "http://127.0.0.1:2026",
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  projects: [
    {
      name: "desktop-chromium",
      use: { ...devices["Desktop Chrome"], browserName: "chromium" },
    },
  ],
});

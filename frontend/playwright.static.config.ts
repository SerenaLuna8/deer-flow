import { defineConfig, devices } from "@playwright/test";

const baseURL =
  process.env.PLAYWRIGHT_STATIC_BASE_URL ?? "http://localhost:3100";

export default defineConfig({
  testDir: "./tests/e2e-static",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "html",
  timeout: 30_000,
  use: {
    baseURL,
    locale: "en-US",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "static-chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command:
      "./node_modules/.bin/next build --webpack && ./node_modules/.bin/next start -p 3100",
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      SKIP_ENV_VALIDATION: "1",
      BUILD_MODE: "static",
      DEER_FLOW_AUTH_DISABLED: "1",
    },
  },
});

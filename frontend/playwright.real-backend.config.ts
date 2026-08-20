import { defineConfig, devices } from "@playwright/test";

const frontendPort = process.env.E2E_FRONTEND_PORT ?? "3000";
const gatewayPort = process.env.E2E_GATEWAY_PORT ?? "8011";
const frontendUrl = `http://localhost:${frontendPort}`;
const gatewayUrl = `http://localhost:${gatewayPort}`;
const gatewayInternalUrl = `http://127.0.0.1:${gatewayPort}`;

/**
 * Core full-stack Replay: the real Next.js frontend renders data from a real
 * Gateway/Worker boundary whose LLM is deterministic `ReplayChatModel` (no API
 * key). It stays separate from the mocked project-route browser check.
 *
 * Two webServers are started: the replay gateway and the frontend pointed at
 * it. Auth-disabled mode is enabled on both servers so the no-cookie e2e
 * contract is covered; the scenario registers one throwaway test account.
 */
export default defineConfig({
  testDir: "./tests/e2e-real-backend",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "html",
  timeout: 90_000,

  use: {
    baseURL: frontendUrl,
    trace: "on-first-retry",
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  webServer: [
    {
      command: `uv run python scripts/run_replay_gateway.py --port ${gatewayPort} --cors ${frontendUrl}`,
      cwd: "../backend",
      url: `${gatewayUrl}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        DEER_FLOW_AUTH_DISABLED: "1",
        // The Current Version acceptance regenerates one completed turn in the
        // same Thread, so each deterministic recorded turn is consumed twice.
        DEERFLOW_REPLAY_REPEAT_COUNT: "2",
      },
    },
    {
      command: "pnpm build && pnpm start",
      url: frontendUrl,
      reuseExistingServer: !process.env.CI,
      timeout: 240_000,
      env: {
        PORT: frontendPort,
        SKIP_ENV_VALIDATION: "1",
        DEER_FLOW_AUTH_DISABLED: "1",
        BETTER_AUTH_SECRET: "local-dev-secret",
        // Leave NEXT_PUBLIC_* unset so the frontend uses its built-in
        // next.config rewrites (same-origin proxy) instead of talking to the
        // gateway cross-origin — cross-origin fetches drop the auth cookies.
        // Just point that proxy at the replay gateway.
        DEER_FLOW_INTERNAL_GATEWAY_BASE_URL: gatewayInternalUrl,
      },
    },
  ],
});

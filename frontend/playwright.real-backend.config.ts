import { resolve } from "node:path";

import {
  defineConfig,
  devices,
  type PlaywrightTestConfig,
} from "@playwright/test";

type RealBackendEnvironment = Readonly<Record<string, string | undefined>>;

function isolatedPort(
  environment: RealBackendEnvironment,
  name: "E2E_FRONTEND_PORT" | "E2E_GATEWAY_PORT",
  fallback: number,
): string {
  const raw = environment[name] ?? String(fallback);
  if (!/^\d+$/.test(raw)) {
    throw new Error(`${name} must be a numeric task-local port`);
  }
  const port = Number(raw);
  if (!Number.isSafeInteger(port) || port < 1024 || port > 65_535) {
    throw new Error(`${name} must be between 1024 and 65535`);
  }
  if (port === 3000) {
    throw new Error(`${name} must not use the ambient development port 3000`);
  }
  return String(port);
}

function replayWorkerMode(
  environment: RealBackendEnvironment,
): "immediate" | "delayed" {
  const mode = environment.E2E_REPLAY_WORKER_MODE ?? "immediate";
  if (mode !== "immediate" && mode !== "delayed") {
    throw new Error(
      "E2E_REPLAY_WORKER_MODE must be either immediate or delayed",
    );
  }
  return mode;
}

export function createRealBackendPlaywrightConfig(
  environment: RealBackendEnvironment,
): PlaywrightTestConfig {
  const frontendPort = isolatedPort(environment, "E2E_FRONTEND_PORT", 3317);
  const gatewayPort = isolatedPort(environment, "E2E_GATEWAY_PORT", 8117);
  if (frontendPort === gatewayPort) {
    throw new Error("E2E_FRONTEND_PORT and E2E_GATEWAY_PORT must differ");
  }

  const frontendUrl = `http://localhost:${frontendPort}`;
  const gatewayUrl = `http://localhost:${gatewayPort}`;
  const gatewayInternalUrl = `http://127.0.0.1:${gatewayPort}`;
  const workerMode = replayWorkerMode(environment);
  const reuseExistingServer = environment.E2E_REUSE_EXISTING_SERVER === "1";
  const replayReadbackPath =
    environment.E2E_REPLAY_READBACK_PATH ??
    resolve("test-results", "replay-gateway-readback.json");

  return defineConfig({
    testDir: "./tests/e2e-real-backend",
    fullyParallel: false,
    forbidOnly: !!environment.CI,
    retries: environment.CI ? 1 : 0,
    workers: 1,
    reporter: environment.CI ? "github" : "html",
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
        reuseExistingServer,
        timeout: 180_000,
        gracefulShutdown: { signal: "SIGTERM", timeout: 30_000 },
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ACT_WEAVE_AUTH_DISABLED: "1",
          ACT_WEAVE_REPLAY_BOOTSTRAP_SCHEMA: "1",
          ACT_WEAVE_REPLAY_REPEAT_COUNT: "2",
          E2E_REPLAY_WORKER_MODE: workerMode,
          E2E_REPLAY_READBACK_PATH: replayReadbackPath,
        },
      },
      {
        command: "pnpm build && pnpm start",
        url: frontendUrl,
        reuseExistingServer,
        timeout: 240_000,
        gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
        env: {
          PORT: frontendPort,
          SKIP_ENV_VALIDATION: "1",
          ACT_WEAVE_AUTH_DISABLED: "1",
          BETTER_AUTH_SECRET: "local-dev-secret",
          ACT_WEAVE_INTERNAL_GATEWAY_BASE_URL: gatewayInternalUrl,
        },
      },
    ],
  });
}

const frontendPort = isolatedPort(process.env, "E2E_FRONTEND_PORT", 3317);
const gatewayPort = isolatedPort(process.env, "E2E_GATEWAY_PORT", 8117);
process.env.E2E_FRONTEND_PORT = frontendPort;
process.env.E2E_GATEWAY_PORT = gatewayPort;

/**
 * Real frontend + Gateway + Worker Replay. The Gateway command derives and
 * owns a fresh disposable PostgreSQL database; production services and the
 * ambient :3000 development frontend are never reused by default.
 */
export default createRealBackendPlaywrightConfig(process.env);

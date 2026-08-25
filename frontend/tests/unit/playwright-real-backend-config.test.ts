import { describe, expect, test } from "@rstest/core";

import { createRealBackendPlaywrightConfig } from "../../playwright.real-backend.config";

function webServers(
  config: ReturnType<typeof createRealBackendPlaywrightConfig>,
) {
  const value = config.webServer;
  if (!Array.isArray(value)) {
    throw new Error("real-backend config must own both Gateway and Frontend");
  }
  return value;
}

describe("real-backend Playwright config", () => {
  test("uses isolated non-default ports and never reuses unknown services by default", () => {
    const config = createRealBackendPlaywrightConfig({
      PLAYWRIGHT_BASE_URL: "http://localhost:3000",
    });
    const [gateway, frontend] = webServers(config);

    expect(config.use?.baseURL).toBe("http://localhost:3317");
    expect(gateway?.command).toContain("--port 8117");
    expect(gateway?.command).toContain("--cors http://localhost:3317");
    expect(gateway?.reuseExistingServer).toBe(false);
    expect(frontend?.reuseExistingServer).toBe(false);
    expect(gateway?.gracefulShutdown).toEqual({
      signal: "SIGTERM",
      timeout: 30_000,
    });
    expect(frontend?.env).toMatchObject({
      PORT: "3317",
      ACT_WEAVE_INTERNAL_GATEWAY_BASE_URL: "http://127.0.0.1:8117",
    });
  });

  test("allows explicit task-local ports, delayed Worker mode, and opt-in reuse", () => {
    const config = createRealBackendPlaywrightConfig({
      E2E_FRONTEND_PORT: "4317",
      E2E_GATEWAY_PORT: "9117",
      E2E_REPLAY_WORKER_MODE: "delayed",
      E2E_REUSE_EXISTING_SERVER: "1",
    });
    const [gateway, frontend] = webServers(config);

    expect(config.use?.baseURL).toBe("http://localhost:4317");
    expect(gateway?.reuseExistingServer).toBe(true);
    expect(frontend?.reuseExistingServer).toBe(true);
    expect(gateway?.env).toMatchObject({
      E2E_REPLAY_WORKER_MODE: "delayed",
    });
  });

  test("rejects unsafe ports and unsupported Worker modes before spawning", () => {
    expect(() =>
      createRealBackendPlaywrightConfig({ E2E_FRONTEND_PORT: "3000" }),
    ).toThrow(/E2E_FRONTEND_PORT/);
    expect(() =>
      createRealBackendPlaywrightConfig({ E2E_GATEWAY_PORT: "not-a-port" }),
    ).toThrow(/E2E_GATEWAY_PORT/);
    expect(() =>
      createRealBackendPlaywrightConfig({
        E2E_REPLAY_WORKER_MODE: "ambient-worker",
      }),
    ).toThrow(/E2E_REPLAY_WORKER_MODE/);
  });
});

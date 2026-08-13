# Replay E2E — core front/back contract

The repository keeps one deterministic, key-free full-stack Replay scenario.
It runs the real Next.js frontend, Gateway, Worker, replay model, PostgreSQL,
and Chromium, then verifies that a streamed answer and file artifact render.

This check protects a contract that frontend unit mocks cannot cover: the
browser-visible combination of Gateway admission, durable stream frames,
Worker execution, and frontend rendering. It is intentionally a single core
scenario, not a historical regression catalogue.

## Fixture and runtime

The committed fixture is:

```text
backend/tests/fixtures/replay/write_read_file.ultra.json
```

`tests/replay_provider.py` returns recorded assistant turns keyed by a
normalized hash of the model caller and conversation. Dates, UUIDs, temporary
paths, and system-prompt wording are excluded from the stable match. A missing
match fails loudly; Replay requires no model-provider API key.

`tests/_replay_fixture.py` builds isolated runtime configuration,
`tests/replay_agent_router.py` exposes only the authenticated Agent preparation
needed by this scenario, and `scripts/run_replay_gateway.py` starts the
test-only Gateway/Worker boundary. These files are test infrastructure and must
not become production fallbacks.

## Run

```bash
cd frontend
pnpm exec playwright test \
  tests/e2e-real-backend/real-backend-render.spec.ts \
  -c playwright.real-backend.config.ts
```

The Playwright configuration requires a disposable PostgreSQL target. It must
never point at the ordinary application or business database.

## Manual gate and limits

Run this scenario explicitly whenever either side of the front/back contract
changes. Preserve the Playwright report or render artifacts with the private
project's release evidence when they are needed for review.

- It does not validate live model providers, external MCP services, Sandbox
  modes, browsers other than the configured Chromium, or deployment topology.
- The committed fixture must be replaced deliberately if the Agent graph or
  browser-visible contract changes; a replay hash miss is not accepted as a
  new baseline automatically.

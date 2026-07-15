# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, and others) when working with code in this repository. It is the source of truth; the sibling `CLAUDE.md` imports it via `@AGENTS.md`.

## Project Overview

DeerFlow is a LangGraph-based AI super agent system with a full-stack architecture. The backend provides a "super agent" with sandbox execution, persistent memory, subagent delegation, and extensible tool integration - all operating in per-thread isolated environments.

**Architecture**:
- **Gateway API** (port 8001): REST API plus embedded LangGraph-compatible agent runtime
- **Frontend** (port 3000): Next.js web interface
- **Nginx** (port 2026): Unified reverse proxy entry point
- **Provisioner** (port 8002, optional in Docker dev): Started only when sandbox is configured for provisioner/Kubernetes mode

**Runtime**:
- `make dev`, Docker dev, and production all run the agent runtime in Gateway via `RunManager` + `run_agent()` + `StreamBridge` (`packages/harness/deerflow/runtime/`). Nginx exposes that runtime at `/api/langgraph/*` and rewrites it to Gateway's native `/api/*` routers.
- Scheduled-task executions must reuse that same Gateway run lifecycle. The scheduler may decide *when* work runs, but it must dispatch through the existing run path rather than introducing a parallel execution stack.

**Project Structure**:
```
deer-flow/
├── Makefile                    # Root commands (check, install, dev, stop)
├── config.yaml                 # Main application configuration
├── extensions_config.json      # MCP servers and skills configuration
├── backend/                    # Backend application (this directory)
│   ├── Makefile               # Backend-only commands (dev, gateway, lint)
│   ├── langgraph.json         # LangGraph Studio graph configuration
│   ├── packages/
│   │   └── harness/           # deerflow-harness package (import: deerflow.*)
│   │       ├── pyproject.toml
│   │       └── deerflow/
│   │           ├── agents/            # LangGraph agent system
│   │           │   ├── lead_agent/    # Main agent (factory + system prompt)
│   │           │   ├── middlewares/   # middleware components (see Middleware Chain section)
│   │           │   ├── memory/        # Memory extraction, queue, prompts
│   │           │   └── thread_state.py # ThreadState schema
│   │           ├── sandbox/           # Sandbox execution system
│   │           │   ├── local/         # Local filesystem provider
│   │           │   ├── sandbox.py     # Abstract Sandbox interface
│   │           │   ├── tools.py       # bash, ls, read/write/str_replace
│   │           │   └── middleware.py  # Sandbox lifecycle management
│   │           ├── subagents/         # Subagent delegation system
│   │           │   ├── builtins/      # general-purpose, bash agents
│   │           │   ├── executor.py    # Background execution engine
│   │           │   └── registry.py    # Agent registry
│   │           ├── tools/builtins/    # Built-in tools (present_files, ask_clarification, view_image)
│   │           ├── mcp/               # MCP integration (tools, cache, client)
│   │           ├── models/            # Model factory with thinking/vision support
│   │           ├── skills/            # Skills discovery, loading, parsing
│   │           ├── config/            # Configuration system (app, model, sandbox, tool, etc.)
│   │           ├── community/         # Community tools (search/fetch/scrape, image search, AIO sandbox)
│   │           ├── reflection/        # Dynamic module loading (resolve_variable, resolve_class)
│   │           ├── utils/             # Utilities (network, readability)
│   │           └── client.py          # Embedded Python client (DeerFlowClient)
│   ├── app/                   # Application layer (import: app.*)
│   │   ├── gateway/           # FastAPI Gateway API
│   │   │   ├── app.py         # FastAPI application
│   │   │   └── routers/       # FastAPI route modules (models, mcp, memory, skills, uploads, threads, artifacts, agents, suggestions, channels)
│   │   └── channels/          # IM platform integrations
│   ├── tests/                 # Test suite
│   └── docs/                  # Documentation
├── frontend/                   # Next.js frontend application
└── skills/                     # Agent skills directory
    ├── public/                # Public skills (committed)
    └── custom/                # Custom skills (gitignored)
```

## Important Development Guidelines

### Documentation Update Policy
**CRITICAL: Always update README.md and AGENTS.md after every code change**

When making code changes, you MUST update the relevant documentation:
- Update `README.md` for user-facing changes (features, setup, usage instructions)
- Update `AGENTS.md` for development changes (architecture, commands, workflows, internal systems). `CLAUDE.md` imports it via `@AGENTS.md`, so editing `AGENTS.md` updates both.
- Keep documentation synchronized with the codebase at all times
- Ensure accuracy and timeliness of all documentation

## Commands

**Root directory** (for full application):
```bash
make check      # Check system requirements
make install    # Install all dependencies (frontend + backend)
make dev        # Start all services (Gateway + Frontend + Nginx), with config.yaml preflight
make start      # Start production services locally
make stop       # Stop all services
```

**Backend directory** (for backend development only):
```bash
make install            # Install backend dependencies
make dev                # Run Gateway API with reload (port 8001)
make gateway            # Run Gateway API only (port 8001)
make test               # Run all backend tests
make test-blocking-io   # Run strict Blockbuster runtime gate on tests/blocking_io/
make lint               # Lint with ruff
make format             # Format code with ruff
make migrate-rev MSG="..."  # Autogenerate a new alembic revision (see Schema Migrations section)
make setup-db           # 显式创建目标 PostgreSQL 数据库并 bootstrap 到 head
make setup-m4-migration-db  # 创建/验证固定在0007的legacy SQLite private-work迁移库
make migrate-db         # 仅升级已存在数据库，不执行管理员建库操作
make migrate-assets ARGS="--dry-run ..."  # shared asset 脱敏 inventory / 显式 cutover
make migrate-private-work ARGS="--dry-run ..."  # M4 core private-work staged migration
make rotate-credentials ARGS="--dry-run --key-id m3-next"  # credential envelope key rotation
make check-db           # 只读检查连接、revision 和必需表
```

The `detect-blocking-io` target parses `app/`, `packages/harness/deerflow/`,
and `scripts/` with AST. By default it reports only blocking IO candidates that
are inside async code, reachable from async code in the same file, or reachable
from sync-only `AgentMiddleware` before/after hooks that LangGraph can execute
on the async graph path. It prints a concise summary and writes complete JSON
findings to `.deer-flow/blocking-io-findings.json` at the repository root
(both `make detect-blocking-io` from the repo root and `cd backend && make
detect-blocking-io` resolve to the same repo-root path). JSON findings include
`priority`, `location`, `blocking_call`, `event_loop_exposure`, `reason`, and
`code` for model-assisted or manual review. `priority` is a deterministic
review ordering from operation type, not proof of a bug. Bare-name same-file
calls are resolved by function name, so duplicate helper names in one file can
conservatively over-report async reachability. It is intentionally
informational and is not run from CI in this round.

For a diff-scoped view of the same findings, `scripts/scan_changed_blocking_io.py`
(repo root) reports findings on the added lines of `git diff <base>...HEAD`
plus findings new versus the merge base (so a new async caller exposing an
untouched sync helper in the same file is still reported) — used by the
`blocking-io-guard` skill (`.agent/skills/blocking-io-guard/`) as the
deterministic scope step before routing each candidate to a fix and/or a
`tests/blocking_io/` runtime anchor.

Regression tests related to Docker/provisioner behavior:
- `tests/test_docker_sandbox_mode_detection.py` (mode detection from `config.yaml`)
- `tests/test_provisioner_kubeconfig.py` (kubeconfig file/directory handling)
- `tests/test_provisioner_request_threading.py` (keeps provisioner sandbox CRUD
  endpoints as sync FastAPI handlers so synchronous K8s client calls run in the
  Starlette worker pool instead of on the ASGI event loop)

Blocking-IO runtime gate (`tests/blocking_io/`):
- Wraps every item under `tests/blocking_io/` with a strict Blockbuster
  context scoped to `app.*` and `deerflow.*` (see
  `tests/support/detectors/blocking_io_runtime.py`). Any sync blocking IO
  call whose stack passes through DeerFlow business code while running on
  the asyncio event loop raises `BlockingError` and fails the test.
- Regression anchors live there: `test_skills_load.py` (locks the
  `asyncio.to_thread` offload around `LocalSkillStorage.load_skills`, fix
  for #1917); `test_jsonl_run_event_store.py` (locks `JsonlRunEventStore`'s async
  API offloading its file IO via `asyncio.to_thread`);
  `test_uploads_middleware.py` (locks `UploadsMiddleware.abefore_agent`
  offloading the uploads-directory scan off the event loop); and
  `test_uploads_router.py` (locks Gateway upload/list/delete endpoints
  offloading upload directory creation, staged writes, chmod/cleanup,
  directory scans/deletes, and remote sandbox sync off the event loop).
- `test_gate_smoke.py` is a meta-test asserting the gate actually catches
  unoffloaded blocking IO and that the `@pytest.mark.allow_blocking_io`
  opt-out works.
- Coverage boundary: the gate only sees code that test execution actually
  touches. Static AST coverage is a separate concern (out of scope for
  this PR).
- CI: runs on every PR via `.github/workflows/backend-blocking-io-tests.yml`,
  hard-fail.

Boundary check (harness → app import firewall):
- `tests/test_harness_boundary.py` — ensures `packages/harness/deerflow/` never imports from `app.*`

CI runs these regression tests for every pull request via [.github/workflows/backend-unit-tests.yml](../.github/workflows/backend-unit-tests.yml).

## Architecture

### Harness / App Split

The backend is split into two layers with a strict dependency direction:

- **Harness** (`packages/harness/deerflow/`): Publishable agent framework package (`deerflow-harness`). Import prefix: `deerflow.*`. Contains agent orchestration, tools, sandbox, models, MCP, skills, config — everything needed to build and run agents.
- **App** (`app/`): Unpublished application code. Import prefix: `app.*`. Contains the FastAPI Gateway API and IM channel integrations (Feishu, Slack, Telegram, DingTalk).

**Dependency rule**: App imports deerflow, but deerflow never imports app. This boundary is enforced by `tests/test_harness_boundary.py` which runs in CI.

**Import conventions**:
```python
# Harness internal
from deerflow.agents import make_lead_agent
from deerflow.models import create_chat_model

# App internal
from app.gateway.app import app
from app.channels.service import start_channel_service

# App → Harness (allowed)
from deerflow.config import get_app_config

# Harness → App (FORBIDDEN — enforced by test_harness_boundary.py)
# from app.gateway.routers.uploads import ...  # ← will fail CI
```

Package import hygiene: the `deerflow.agents` and `deerflow.subagents` package
roots expose heavyweight graph/executor entrypoints lazily. Internal modules
that only need lightweight types, config, or registries should import the
concrete submodule instead of adding eager package-root imports that pull in the
tool graph or subagent executor during state/schema imports.

### Agent System

**Lead Agent** (`packages/harness/deerflow/agents/lead_agent/agent.py`):
- Entry point: `make_lead_agent(config: RunnableConfig)` registered in `langgraph.json`
- Dynamic model selection via `create_chat_model()` with thinking/vision support
- Tools loaded via `get_available_tools()` - combines sandbox, built-in, MCP, community, and subagent tools
- System prompt generated by `apply_prompt_template()` with skills, memory, and subagent instructions

**ThreadState** (`packages/harness/deerflow/agents/thread_state.py`):
- Extends `AgentState` with: `sandbox`, `thread_data`, `title`, `artifacts`, `todos`, `uploaded_files`, `viewed_images`, `goal`, `promoted`, `delegations`, `skill_context`, `summary_text`
- Uses custom reducers: `merge_artifacts` (deduplicate), `merge_viewed_images` (merge/clear), `merge_goal` (preserve the active goal across ordinary state updates unless the goal writer replaces it), `merge_promoted` (catalog-hash-scoped deferred tool promotions), `merge_delegations` (append task delegation entries, same id latest wins, terminal status never downgraded, capped to the most recent entries), and `merge_skill_context` (dedupe active-skill references by path, keep the most recently read entries; entries store a name/path/description reference, not the SKILL.md body). `summary_text` is a LastValue channel updated by summarization and projected into model requests as durable context data instead of being stored as a `messages` item.

**Runtime Configuration** (via `config.configurable`):
- `thinking_enabled` - Enable model's extended thinking
- `model_name` - Select specific LLM model
- `is_plan_mode` - Enable TodoList middleware
- `subagent_enabled` - Enable task delegation tool

### Middleware Chain

Lead-agent middlewares are assembled in strict order across three functions: the shared base in `packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py` (`_build_runtime_middlewares`, exposed via `build_lead_runtime_middlewares`), then the lead-only middlewares appended in `packages/harness/deerflow/agents/lead_agent/agent.py` (`build_middlewares`). Items marked *(optional)* are appended only when their config/runtime condition holds, so the live chain length varies.

**Shared runtime base** (`build_lead_runtime_middlewares`; subagents reuse most of this via `build_subagent_runtime_middlewares`):

1. **InputSanitizationMiddleware** - First, so it is the outermost `wrap_model_call` wrapper; every inner middleware (including LLM retries) sees sanitized messages
2. **ToolOutputBudgetMiddleware** - Caps tool output size (per app config) before it re-enters the model context
3. **ToolResultSanitizationMiddleware** - Neutralizes framework/injection tags (e.g. `<system-reminder>`) and boundary markers in *remote-content* tool results (`web_fetch`/`web_search`/`image_search`) so attacker-controlled fetched pages cannot forge trusted framework context. Mirrors `InputSanitizationMiddleware`'s user-input guardrail for the other untrusted-content entry point; sits inner of `ToolOutputBudgetMiddleware` (neutralizes the raw output, then the budget truncates). Local tool output (bash/read_file) is left untouched. Scope is a name-based allowlist, so MCP remote-content tools registered under other names (e.g. `fetch_url`) are not yet covered — a metadata-tagging follow-up is tracked in the middleware source
4. **ThreadDataMiddleware** - Creates per-thread directories under the user's isolation scope (`backend/.deer-flow/users/{user_id}/threads/{thread_id}/user-data/{workspace,uploads,outputs}`); resolves `user_id` via `get_effective_user_id()` (falls back to `"default"` in no-auth mode)
5. **UploadsMiddleware** - Tracks and injects newly uploaded files into conversation (lead agent only)
6. **SandboxMiddleware** - Acquires sandbox, stores `sandbox_id` in state
7. **DanglingToolCallMiddleware** - Injects placeholder ToolMessages for AIMessage tool_calls that lack responses (e.g., user interruption), preserving raw provider tool-call payloads in `additional_kwargs["tool_calls"]`; malformed empty tool-call names are normalized to a recoverable error so strict OpenAI-compatible providers do not reject the next request
8. **LLMErrorHandlingMiddleware** - Normalizes provider/model invocation failures into recoverable assistant-facing errors before later stages run
9. **GuardrailMiddleware** - *(optional, if `guardrails.enabled`)* Pre-tool-call authorization via pluggable `GuardrailProvider`; returns an error ToolMessage on deny. Providers: built-in `AllowlistProvider` (zero deps), OAP policy providers (e.g. `aport-agent-guardrails`), or custom. See [docs/GUARDRAILS.md](docs/GUARDRAILS.md)
10. **SandboxAuditMiddleware** - Audits sandboxed shell/file operations for security logging before tool execution
11. **ReadBeforeWriteMiddleware** - *(optional, if `read_before_write.enabled`, default on)* Outermost write gate (issue #3857): `read_file` stamps a content hash onto its ToolMessage; `write_file` (append/overwrite-existing) and `str_replace` are blocked unless the newest mark for that path matches the file's current hash. Sits outside ToolProgressMiddleware and ToolErrorHandlingMiddleware so a blocked write returns immediately without consuming a ToolProgress slot. Blocked results call `normalize_tool_result` directly to stamp `deerflow_tool_meta` (`recoverable_by_model=True`) before returning, keeping the result well-formed for any outer consumer. Marks live on messages, so summarization dropping the read result invalidates the gate automatically; writes never refresh marks, forcing a re-read between consecutive edits. Gate check + tool execution are serialized per (thread, path) so same-turn parallel writes cannot reuse one stale mark; on sandboxes whose `read_file` reports failures as `"Error: ..."` strings instead of raising (AIO/E2B), uninspectable targets fail open (creation proceeds, no mark stamped)
12. **ToolProgressMiddleware** - *(optional, if `tool_progress.enabled`)* State-machine-based stagnation guard (RFC #3177). Outer wrapper around ToolErrorHandlingMiddleware so its `wrap_tool_call` receives results already stamped with `deerflow_tool_meta`. Tracks per-(thread, tool) consecutive "no-new-info" calls across three error categories: (a) `recoverable_by_model=True` (no_results, not_found, permission, Jaccard-duplicate success): ACTIVE → WARNED (terminal — hint re-injected on each subsequent problem); (b) `recoverable_by_model=False, action≠stop` (rate_limited, transient): ACTIVE → WARNED → BLOCKED after `warn_escalation_count` more problems; (c) `recoverable_by_model=False, action=stop` (auth, config, internal): immediately BLOCKED on first occurrence. **Division of labor with LoopDetectionMiddleware:** ToolProgressMiddleware is a result-quality guard — fires after tool execution and blocks specific tools that stop producing new information; LoopDetectionMiddleware is a call-pattern guard — fires after the model responds and hard-stops the whole turn when the model repeatedly issues identical tool_calls. Both can inject HumanMessage hints in the same model call without conflict; neither reads the other's internal state.
13. **ToolErrorHandlingMiddleware** - Receives `AppConfig`, converts tool exceptions into error `ToolMessage`s so the run can continue instead of aborting, stamps every result with `deerflow_tool_meta` (status / error_type / recoverable_by_model / recommended_next_action / source) via `tool_result_meta.normalize_tool_result`, stamps structured metadata for task exception wrappers, and stamps skill-read metadata for downstream durable-context capture. Task tool result text is generated from the same status/result/error inputs as the structured metadata so callers do not hand-write a second protocol string.

**Lead-only middlewares** (`build_middlewares`, appended after the base):

14. **DynamicContextMiddleware** - Injects the current date (and optionally memory) as a `<system-reminder>` into the first HumanMessage, keeping the base system prompt fully static for prefix-cache reuse
15. **SkillActivationMiddleware** - Detects strict `/skill-name task` syntax on the latest real user message, resolves only enabled and runtime-allowed skills, injects the `SKILL.md` body as hidden current-turn context, and records a `middleware:skill_activation` audit event
16. **DurableContextMiddleware** - Captures `task` delegations into `ThreadState.delegations` (including in-progress dispatches and terminal result summaries) and loaded skill-file references (name/path/description, parsed in-memory - not the body) into `ThreadState.skill_context` before summarization can compact the paired tool-call/result messages, then projects durable context into each model request. Static authority rules are injected as a `SystemMessage`; untrusted field values (`summary_text`, delegation results, skill descriptions) are injected separately as a hidden `HumanMessage` data block so compressed history, delegated work, and which skills are active stay visible without being stored as `messages` or promoted to system-role instructions.
17. **SummarizationMiddleware** - *(optional, if enabled)* Context reduction when approaching token limits
18. **TodoListMiddleware** - *(optional, if `is_plan_mode`)* Task tracking with the `write_todos` tool
19. **TokenUsageMiddleware** - *(optional, if `token_usage.enabled`)* Records token usage metrics; subagent usage is merged back into the dispatching AIMessage by message position
20. **TitleMiddleware** - Auto-generates the thread title after the first complete exchange and normalizes structured message content before prompting the title model. If a first-turn run is interrupted before this middleware can write a title, `runtime/runs/worker.py` keeps the run in a finalizing state, persists a local fallback title from the latest checkpoint or original run input, and then syncs it to `threads_meta.display_name`. Replacement runs admitted by `multitask_strategy="interrupt"` / `"rollback"` wait for older same-thread finalization before entering the graph; the interrupted run only skips the fallback title write once a later run has started and may have advanced the checkpoint.
21. **MemoryMiddleware** - Queues conversations for async memory update (filters to user + final AI responses)
22. **ViewImageMiddleware** - *(optional, if the model supports vision)* Injects base64 image data before the LLM call
23. **McpRoutingMiddleware** - *(optional, if `tool_search.enabled` and PR1 MCP routing metadata produce a routing index)* Auto-promotes matching deferred MCP tool schemas before the model call by writing a minimal `promoted` state update. It matches only the latest real `HumanMessage`, uses the global `tool_search.auto_promote_top_k` limit (default 3, clamped to 1..5), never executes tools, and must be installed before `DeferredToolFilterMiddleware`
24. **DeferredToolFilterMiddleware** - *(optional, if `tool_search.enabled`)* Hides deferred (MCP) tool schemas from the bound model until `tool_search` or `McpRoutingMiddleware` promotes them (reads per-thread promotions from `ThreadState.promoted`, hash-scoped)
25. **SystemMessageCoalescingMiddleware** - Merges every SystemMessage into a single leading SystemMessage per request; provider-agnostic fix for strict backends (vLLM/SGLang/Qwen/Anthropic) that reject non-leading system messages. Touches the per-request payload only (checkpoint state unchanged); on midnight crossings only the latest `dynamic_context_reminder` SystemMessage survives
26. **SubagentLimitMiddleware** - *(optional, if `subagent_enabled`)* Truncates excess `task` tool calls to enforce the `MAX_CONCURRENT_SUBAGENTS` limit
27. **LoopDetectionMiddleware** - *(optional, if `loop_detection.enabled`)* Detects repeated tool-call loops; hard-stop clears both structured `tool_calls` and raw provider tool-call metadata before forcing a final text answer; stamps `loop_capped` via `consume_stop_reason` (#3875 Phase 2), symmetric to `TokenBudgetMiddleware`
28. **TokenBudgetMiddleware** - *(optional, if `token_budget.enabled`)* Enforces per-run token limits
29. **Custom middlewares** - *(optional)* Any `custom_middlewares` passed to `build_middlewares` are injected here, before the safety/clarification tail
30. **SafetyFinishReasonMiddleware** - *(optional, if `safety_finish_reason.enabled`)* Suppresses tool execution when the provider safety-terminated the response (e.g. `finish_reason=content_filter`); registered after custom middlewares so LangChain's reverse-order `after_model` dispatch runs it first
31. **ClarificationMiddleware** - Intercepts `ask_clarification` tool calls, writes a readable `ToolMessage.content` fallback plus structured `ToolMessage.artifact.human_input` request payload, and interrupts via `Command(goto=END)` (must be last). Because this middleware can short-circuit tool execution before LangChain emits `on_tool_end`, `RunJournal` performs a root-run final reconciliation for allowlisted clarification `ToolMessage`s whose `tool_call_id` was produced by the current run, so human-input request cards remain recoverable from `run_events` after checkpoint compaction. Human Input Card replies are submitted as `hide_from_ui` `HumanMessage`s with `additional_kwargs.human_input_response`; `RunJournal` persists only allowlisted hidden response sources (currently `ask_clarification`) as `llm.human.input`, which preserves answered-card state after compaction without exposing generic internal hidden context.

### Configuration System

**Main Configuration** (`config.yaml`):

Setup: Copy `config.example.yaml` to `config.yaml` in the **project root** directory.

**Config Versioning**: `config.example.yaml` has a `config_version` field. On startup, `AppConfig.from_file()` compares user version vs example version and emits a warning if outdated. Missing `config_version` = version 0. Run `make config-upgrade` to auto-merge missing fields. When changing the config schema, bump `config_version` in `config.example.yaml`.

**Config Caching**: `get_app_config()` caches the parsed config, but automatically reloads it when the resolved config path or file content signature changes. The signature includes file metadata and a content digest, so Gateway and LangGraph reads stay aligned with `config.yaml` edits even on object-store or network mounts where mtime can remain stale.

**Config Hot-Reload Boundary**: Gateway dependencies route through `get_app_config()` on every request, so per-run fields like `models[*].max_tokens`, `summarization.*`, `title.*`, `memory.*`, `subagents.*`, `tools[*]`, and the agent system prompt pick up `config.yaml` edits on the next message. `AppConfig` is intentionally **not** cached on `app.state` — `lifespan()` keeps a local `startup_config` variable for one-shot bootstrap work and passes it to `langgraph_runtime(app, startup_config)`.

Infrastructure fields are **restart-required**. The authoritative list lives in `packages/harness/deerflow/config/reload_boundary.py::STARTUP_ONLY_FIELDS` and is mirrored by the standardised `"startup-only:"` prefix on the corresponding `Field(description=...)` in `AppConfig`, so IDE hover on those fields surfaces the reason inline (no need to context-switch into this table). Currently registered: `database`, `run_events`, `stream_bridge`, `sandbox`, `log_level`, `logging`, `channels`, `channel_connections`. Adding a new restart-required field requires updating the registry; drift is pinned by `tests/test_reload_boundary.py`.

**Persistence configuration**: the unified `database.url` setting is
PostgreSQL-only and supplies the Gateway's LangGraph checkpointer, LangGraph
Store, and DeerFlow SQL repositories. It defaults from `DATABASE_URL` and
accepts `postgresql://` or `postgresql+asyncpg://`; the standalone
`checkpointer` section is rejected. PostgreSQL dependencies are installed by
default. Runtime ORM, schema bootstrap, checkpointer, and store providers are
PostgreSQL-only; startup probes the configured database and never creates it.

Configuration priority:
1. Explicit `config_path` argument
2. `DEER_FLOW_CONFIG_PATH` environment variable
3. `config.yaml` in current directory (backend/)
4. `config.yaml` in parent directory (project root - **recommended location**)

Config values starting with `$` are resolved as environment variables (e.g., `$OPENAI_API_KEY`).
`ModelConfig` also declares `use_responses_api` and `output_version` so OpenAI `/v1/responses` can be enabled explicitly while still using `langchain_openai:ChatOpenAI`.

**Extensions Configuration** (`extensions_config.json`):

MCP servers and skills are configured together in `extensions_config.json` in project root:

Docker development mounts the project directory at `/app/project` and points
`DEER_FLOW_CONFIG_PATH` / `DEER_FLOW_EXTENSIONS_CONFIG_PATH` into that directory.
Keep mutable config files behind a directory bind mount: single-file bind mounts
can become stale or inaccessible when a host editor replaces a file on save.

Configuration priority:
1. Explicit `config_path` argument
2. `DEER_FLOW_EXTENSIONS_CONFIG_PATH` environment variable
3. `extensions_config.json` in current directory (backend/)
4. `extensions_config.json` in parent directory (project root - **recommended location**)

### Gateway API (`app/gateway/`)

FastAPI application on port 8001 with health check at `GET /health`. Set `GATEWAY_ENABLE_DOCS=false` to disable `/docs`, `/redoc`, and `/openapi.json` in production (default: enabled).

CORS is same-origin by default when requests enter through nginx on port 2026. Split-origin or port-forwarded browser clients must opt in with `GATEWAY_CORS_ORIGINS` (comma-separated exact origins); Gateway `CORSMiddleware` and `CSRFMiddleware` both read that variable so browser CORS and auth-origin checks stay aligned.

**Routers**:

| Router | Endpoints |
|--------|-----------|
| **Models** (`/api/models`) | `GET /` - list models; `GET /{name}` - model details |
| **Features** (`/api/features`) | `GET /` - report config-gated feature availability (currently `agents_api.enabled`) for frontend UI gating |
| **Project governance** (`/api/projects`, `/api/project-invitations`) | Authenticated member, invitation, deletion, and restore APIs use immutable `ProjectContext` plus scoped repositories. Public invitation claim exchanges a fragment token for a ten-minute AES-GCM authenticated-encrypted opaque HttpOnly cookie derived from the Auth secret; claim/redeem use a PostgreSQL-shared 5-attempt/5-minute limiter whose single-statement admission admits only the first five concurrent attempts, clears on success, and stores SHA-256 keys only. 每次 admission 在同一事务内按 `expires_at` 索引使用 `FOR UPDATE SKIP LOCKED` 最多清理 100 条过期计数，避免公开接口产生无界表增长或无界清理。 |
| **Console** (`/api/console`) | Read-only cross-thread observability for the current user (the data layer for an operations dashboard or external monitoring): `GET /stats` - headline counters (runs/threads/agents/tokens/cost); `GET /runs` - paginated run history joined with thread titles (per-run cost); `GET /usage` - zero-filled daily token series + per-model breakdown with spend. Queries `runs`/`threads_meta` directly as a reporting layer (no new `RunStore` methods) and uses the required PostgreSQL connection from `database.url`. A legacy internal memory-backend guard remains until task 3 provider cleanup, but memory is not selectable through public AppConfig. Real-cost estimation reads optional `models[*].pricing` (`currency`, `input_per_million`, `output_per_million`, `input_cache_hit_per_million`; `ModelConfig` is `extra="allow"`, so no schema change) and prices each run from its `token_usage_by_model` input/output split. Pricing is **cache-aware**: `RunJournal` accumulates prompt-cache hits from `usage_metadata.input_token_details.cache_read` into a sparse `cache_read_tokens` bucket key (also threaded through `SubagentTokenCollector` → `record_external_llm_usage_records`), and cache-hit input tokens are billed at `input_cache_hit_per_million` (omitted → billed at the miss price, a conservative upper bound). Legacy rows fall back to run-level totals at `model_name`; unpriced models yield `cost: null` and cost fields are null when no pricing is configured |
| **MCP** (`/api/mcp`) | `GET /config` - get config; `PUT /config` - update config (saves to extensions_config.json) |
| **Skills** (`/api/skills`) | `GET /` - list skills; admin-only `GET /content/{name}` - read-only raw `SKILL.md` for a skill visible in the current user's storage scope; `GET /{name}` - details; `PUT /{name}` - update enabled; `POST /install` - install from .skill archive (accepts standard optional frontmatter like `version`, `author`, `compatibility`) |
| **Memory** (`/api/memory`) | `GET /` - memory data; `POST /reload` - force reload; `GET /config` - config; `GET /status` - config + data |
| **Uploads** (`/api/threads/{id}/uploads`) | `POST /` - upload files (auto-converts PDF/PPT/Excel/Word); `GET /list` - list; `DELETE /{filename}` - delete |
| **Threads** (`/api/threads/{id}`) | `DELETE /` - remove DeerFlow-managed local thread data after LangGraph thread deletion; `POST /branches` - create a new main-thread branch from a completed assistant turn checkpoint. Workspace files are not checkpointed, so the branch only best-effort copies the current workspace when branching from the **latest** turn (`workspace_clone_mode="current_thread_best_effort"`); branching from an older/historical turn skips the copy (`workspace_clone_mode="skipped_historical_turn"`) so the branch never inherits files that only exist in a later timeline; `GET /goal`, `PUT /goal`, `DELETE /goal` - read, set, and clear the active thread goal; `POST /compact` - manually summarize older active context into `summary_text` and retain the recent message window, blocked while a run is in flight; unexpected failures are logged server-side and return a generic 500 detail |
| **Artifacts** (`/api/threads/{id}/artifacts`) | `GET /{path}` - serve artifacts; active content types (HTML/XHTML/SVG, JavaScript/ECMAScript, XML, and PDF, after lowercasing and removing parameters) are always forced as download attachments to reduce content-sniffing/script risk; `?download=true` still forces download for other file types |
| **Suggestions** (`/api/suggestions`) | `GET /config` - returns global suggestions config boolean; `POST /threads/{id}/suggestions` - generate follow-up questions; rich list/block model content is normalized and inline reasoning (`<think>...</think>`, including unclosed/truncated blocks from reasoning models like MiniMax-M3) is stripped before JSON parsing |
| **Input Polish** (`/api/input-polish`) | `POST /` - rewrite a composer draft before it is sent. This is a short authenticated `runs:create` LLM request using `input_polish` config; it does not create a LangGraph run, persist a message, or modify thread state. Shares the non-graph one-shot LLM path (`deerflow.utils.oneshot_llm.run_oneshot_llm`) with the suggestions route so model build + Langfuse metadata + invoke stay in one place; validates the same stripped view of the draft it sends to the model, and preserves literal `<think>` substrings in the rewrite (`strip_think_blocks(truncate_unclosed=False)`) |
| **Thread Runs** (`/api/threads/{id}/runs`) | `POST /` - create background run; `POST /stream` - create + SSE stream; `POST /wait` - create + block; `POST /regenerate/prepare` - prepare clean input + checkpoint metadata for regenerating the latest assistant answer; `GET /` - list runs; `GET /{rid}` - run details; `POST /{rid}/cancel` - cancel; `GET /{rid}/join` - join SSE; `GET /{rid}/messages` - paginated messages `{data, has_more}`; `GET /{rid}/events` - full event stream; `GET /{rid}/workspace-changes` - workspace/output file change summary and optional diffs; `GET /../messages` - thread messages with feedback; `GET /../token-usage` - aggregate tokens |
| **Feedback** (`/api/threads/{id}/runs/{rid}/feedback`) | `PUT /` - upsert feedback; `DELETE /` - delete user feedback; `POST /` - create feedback; `GET /` - list feedback; `GET /stats` - aggregate stats; `DELETE /{fid}` - delete specific |
| **Runs** (`/api/runs`) | `POST /stream` - stateless run + SSE; `POST /wait` - stateless run + block; `GET /{rid}/messages` - paginated messages by run_id `{data, has_more}` (cursor: `after_seq`/`before_seq`); `GET /{rid}/feedback` - list feedback by run_id |
| **GitHub Webhooks** (`/api/webhooks/github`) | `POST /` - receive GitHub App / repo webhook deliveries. Verifies `X-Hub-Signature-256` against `GITHUB_WEBHOOK_SECRET`; exempt from auth + CSRF because authenticity is enforced by HMAC. The route is fail-closed: mounted only when `GITHUB_WEBHOOK_SECRET` is set, or when explicit dev opt-in `DEER_FLOW_ALLOW_UNVERIFIED_GITHUB_WEBHOOKS=1` is set. Recognized events include `ping`, `issues`, `issue_comment`, `pull_request`, `pull_request_review`, and `pull_request_review_comment`; unknown events return 200 with `handled=false`. Fan-out runtime failures return 503 so GitHub retries; permanent/non-retryable conditions such as `channels.github.enabled: false`, unknown events, malformed payloads, or unavailable channel service return 200 with a skipped/handled response. |
| **GitHub Event-Driven Agents** | Custom agents can declare a `github:` block in their `config.yaml` to bind to repos and event triggers. Webhook fan-out publishes one `InboundMessage` per matching binding to the channel bus; `GitHubChannel` routes those messages through `ChannelManager`. The response `dispatch` summarizes matched/fired/skipped agents. |

`POST /api/threads/{id}/state` 会基于实际读取到的 checkpoint 创建一个新的子
checkpoint，而不是覆盖原记录。新记录使用新的 UUID6；写入配置保留原
checkpoint 的 `checkpoint_id` 作为 parent，同时保留 `thread_id`、
`checkpoint_ns` 和 owner 相关配置。接口会为每个更新的 channel 调用
`get_next_version`，同步推进 `channel_versions`，并把 `new_versions` 交给
saver 持久化非基础类型的 blob。请求显式携带历史 `checkpoint_id` 时，新记录
从该历史节点分支，parent 链仍可继续遍历。

**Workspace change review**: `packages/harness/deerflow/workspace_changes/`
captures a pre-run and post-run snapshot of the thread-owned `workspace` and
`outputs` directories. `runtime/runs/worker.py` performs the filesystem scan via
`asyncio.to_thread` and writes a `workspace_changes` event with category
`workspace` when changes exist. Uploads are intentionally excluded. Text diffs
are size-limited; binary, large, and sensitive-looking paths are persisted as
metadata only.

**RunManager / RunStore contract**:
- `RunManager.get()` is async; direct callers must `await` it.
- When a persistent `RunStore` is configured, `get()` and `list_by_thread()` hydrate historical runs from the store. In-memory records win for the same `run_id` so task, abort, and stream-control state stays attached to active local runs.
- `cancel()` and `create_or_reject(..., multitask_strategy="interrupt"|"rollback")` persist interrupted status through `RunStore.update_status()`, matching normal `set_status()` transitions.
- Store-only hydrated runs are readable history. If the current worker has no in-memory task/control state for that run, cancellation APIs can return 409 because this worker cannot stop the task.
- `POST /wait` (both thread-scoped and `/api/runs/wait`) drains the stream bridge via `wait_for_run_completion()` instead of bare `await record.task`, so it honours the run's `on_disconnect` setting and cancels the background run on real client disconnect rather than returning a stale checkpoint (issue #3265).
- All three thread-scoped run-create routes (`POST /runs`, `/runs/stream`, and `/runs/wait`) run pure request/checkpoint shape preflight before permission or storage dependencies. `start_run()` then completes thread authorization before agent/input normalization, config assembly, and checkpoint-saver existence validation; only after those checks succeed may it persist a run, apply interrupt/rollback, or update thread status. This ordering preserves anti-enumeration (unauthorized callers cannot probe checkpoints) and keeps validation failures immediately retryable without orphaned pending runs or interrupted healthy work.
- Redis `StreamBridge` keys use a rolling retained-buffer TTL (`stream_bridge.stream_ttl_seconds`, refreshed on `publish()` / `publish_end()`) as a leak safety net, not as a run timeout. Supported launchers pass authoritative `GATEWAY_WORKERS` to both uvicorn's explicit `--workers` argument and the application. Startup orphan recovery runs only when that value resolves to exactly one, then publishes `END_SENTINEL` and schedules stream cleanup for recovered runs. Multi-worker startup skips recovery because runs have no worker ownership/liveness lease; malformed `Last-Event-ID` reconnect values live-tail new Redis events rather than replaying the retained buffer.
- Thread-scoped run creation accepts `checkpoint` / `checkpoint_id`; Gateway validates the checkpoint belongs to the request thread before writing `checkpoint_id` / `checkpoint_ns` into `config.configurable` for LangGraph branching.
- Thread-scoped Gateway runs evaluate an active `ThreadState.goal` after the visible turn completes. `runtime/goal.py` asks a non-thinking evaluator model to judge only visible conversation evidence and return a typed blocker; the evaluator model is created once per run and reused across hidden continuation checks. Satisfied goals are cleared; every non-satisfied evaluation — continuable or stand-down — is persisted with `last_evaluation` (the blocker, reason, and evidence summary; outcomes that stop the loop additionally record a `stand_down_reason` for observability), but only `goal_not_met_yet` evaluations are streamed as hidden `HumanMessage` continuations, and only when a durable assistant end-of-turn checkpoint exists, the run has not been aborted, the thread did not change during evaluation, and the no-progress breaker has not fired. The continuation cap is 8 — a hard maximum in the `0`–`8` range; callers requesting more are clamped (`set_goal`/TUI) or rejected with 422 (`PUT /goal`). The no-progress breaker keys on the latest visible assistant evidence (not the evaluator's free-text reason, which an LLM rewords every turn), so two consecutive continuations that add no new visible assistant output stop the loop after 2 attempts. Model-response cleanup helpers such as think-block stripping and code-fence stripping live in `deerflow.utils.llm_text` so `runtime/goal.py` and Gateway suggestion parsing share the same JSON-prep behavior.

**Project-private run admission and exact assets**:
- `PrivateRunAdmissionService` revalidates an issued `PrivateWorkContext` with both private-run and shared-asset execution capabilities, then preserves the project → membership → Thread → Run/assets lock order. It writes the pending Run and exact secret-free Agent/Skill/MCP/grant snapshot atomically before any graph, model, tool, or MCP construction.
- `PrivateAssetRuntime` reloads only those persisted exact IDs and verifies their versions, checksums, catalog generation, and current locked credential-grant closure. The selected Agent model must still be configured before materialization starts. Drift fails closed with the stable private-work error contract.
- Project Skill files are parsed from a per-run temporary tree and exposed through a typed, run-matching read-only sandbox mount; their read/list path bypasses global Skill enablement and storage state, and they never enter global Skill discovery/cache state. MCP schemas and proxy tools are run-local. Plaintext credentials exist only inside the M3 materializer and one-shot MCP call variables; results and discovery schemas are recursively rejected if they echo credential material.
- `prepare_private_run_config()` recursively removes client authority, identity, internal-mode, sandbox, and asset-context fields from top-level config, `context`, `configurable`, and nested data. The server injects only the trusted Thread ID and opaque private scope; the worker overwrites the runtime Run ID and derives user/owner authority from the admitted persisted Run.
- `start_private_run()` registers the persisted Run with the existing `RunManager`, launches the existing `run_agent()` worker exactly once, and uses the existing stream/journal/completion lifecycle. `app/gateway/routers/private_work.py` is mounted at `/api/projects/{project_id}/private-work` for readiness, Thread, run/stream/wait, feed, file, and artifact operations. Gateway lifespan initializes the project-scoped checkpointer plus private Thread, Run, file, and streaming services on `app.state`. It also installs a dedicated PostgreSQL `private_run_event_store`; project workers and feed endpoints use it regardless of the legacy `run_events.backend`, so the default in-memory legacy store cannot erase project chat history on restart. `PrivateWorkCutoverGuard` reads the singleton marker on every request/runtime boundary: project routes require the final Alembic revision plus `cutover_complete`, while completed cutover closes the legacy Thread/run/Memory/channel connection/upload/artifact routers and shared `start_run` before downstream state access. `scripts/migrate_private_work.py` is the explicit runnable-first cutover path for empty installs or legacy PostgreSQL Thread/run/event/feedback plus checkpoint metadata markers. It requires a direct owner UUID→active project UUID map, keeps dry-run zero-write, commits a stable per-domain ledger, upgrades through current head, then writes the final marker. Non-empty filesystem, Memory, file/artifact, or connection migration remains deferred and fails closed before finalize.
- Project-private file authority restores only scoped `ready` PostgreSQL rows into a run lease, finalizes bounded workspace/output changes before terminal success, and copies latest-visible-turn branch files through locked database authority. Local, AIO LocalContainer, E2B, and Boxlite provide run-scoped private leases with bounded binary I/O and read-only exact-Skill delivery; AIO RemoteProvisioner currently lacks the required hardened runtime capability and fails before Pod allocation. Private cleanup retains failed leases for bounded retry, while legacy non-project sandbox behavior remains separate.
- M5 Automation persistence uses final project+owner-scoped `scheduled_tasks` definitions and durable `scheduled_task_runs` occurrences. `ScheduledTaskRepository` and `ScheduledTaskRunRepository` are session-bound: callers own the transaction, pass an exact `PrivateResourceScope` as the first authority argument, and every read/mutation carries both `project_id` and `owner_user_id` predicates. Definition mutations use version CAS, accept only the explicit `ScheduledTaskPatch` field allowlist, and soft-delete through the dedicated CAS method; occurrence completion is terminal CAS. Project-scoped Agent references are guarded bidirectionally in PostgreSQL so the Agent project must equal the task project, while system Agents remain global. Do not add naked owner/task lookup methods, `session.get()` followed by an owner check, arbitrary `.values()` mappings, task-row leases, or legacy `user_id` aliases back to these repositories/models.
- M5 Automation dispatch keeps Task 5 claim rows free of speculative runtime foreign keys. Task 6 derives deterministic Thread/Run IDs, creates or strictly adopts a real scoped M4 Thread, starts the Run only through `start_private_run()`, and atomically backfills both real foreign keys while moving `launching` to `running`. Fresh and reuse modes both fail closed on scope, Agent, version, metadata, or current-authority drift. Once a matching M4 Run exists, the occurrence is never requeued with null runtime references, and dispatch never falls back to shared `start_run()`.
- M5 Automation completion and restart recovery are owned by `AutomationReconciler`, not callback metadata or the legacy scheduler repositories. The worker invokes its terminal completion hook exactly once only after private runtime/file-authority cleanup has established the final authoritative M4 Run status. Completion re-derives project+owner scope, locks project/membership -> definition -> occurrence -> Run, and increments the parent count only after the first terminal occurrence CAS. With `scheduler.enabled=true`, `GATEWAY_WORKERS=1` is the quick topology guard and `AutomationSchedulerOwnership` is the authoritative cross-process singleton: it holds fixed PostgreSQL session advisory lock `0x0DEE_12F1_0A55_0007` on one dedicated connection for the Gateway runtime lifetime. Ownership is acquired before automation restart reconciliation and generic M4 orphan recovery; lock contention or reconciliation/database failure aborts startup before generic mutation. An expired `launching` occurrence is requeued only when no deterministic private Run exists, while an admitted active Run is interrupted and never replayed. The occurrence-driven `ScheduledTaskService` then reserves, claims, and dispatches through the M4 private runtime. Disabled scheduler mode takes no ownership lock and leaves manual Automation APIs available; unsupported multi-worker mode remains initialized but does not reconcile or poll. Shutdown cancels the scheduler before releasing ownership and closing runtime dependencies.
- `app.automations` owns M5's public-safe error mapping, frozen application DTOs, cutover guard, and read-only readiness result. Project Automation remains closed unless the M4 private-work marker, M5 automation marker, and final M5 revision (or a known descendant) are all complete; legacy Automation closes when the M5 marker completes. Marker/revision or database failures fail closed with stable public codes. `scheduler.enabled=false` reports scheduler availability separately and never closes an otherwise-ready project Automation API.

Proxied through nginx: `/api/langgraph/*` → Gateway LangGraph-compatible runtime, all other `/api/*` → Gateway REST APIs.

### Sandbox System (`packages/harness/deerflow/sandbox/`)

**Interface**: Abstract `Sandbox` with `execute_command(command, env=None)`, `read_file`, `write_file`, `list_dir`. The optional `env` injects per-call environment variables (request-scoped secrets — see Request-Scoped Secrets below); `LocalSandbox` merges it via `subprocess.run(env=...)` and `AioSandbox` routes env-bearing commands through the `bash.exec(env=...)` API on a fresh session.
**Provider Pattern**: `SandboxProvider` with `acquire`, `acquire_async`, `get`, `release` lifecycle. Async agent/tool paths call async sandbox lifecycle hooks so Docker sandbox creation, discovery, cross-process locking, readiness polling, and release stay off the event loop.
**Environment policy** (`sandbox/env_policy.py`): `execute_command` no longer inherits the full `os.environ`. `build_sandbox_env()` scrubs secret-looking names (`*KEY*`/`*SECRET*`/`*TOKEN*`/`*PASSWORD*`/`*CREDENTIAL*`) from the inherited environment before layering injected request secrets on top, so platform credentials (e.g. `OPENAI_API_KEY`) never leak into skill subprocesses. Benign vars (`PATH`, `HOME`, `LANG`, `VIRTUAL_ENV`, ...) are preserved.
**Implementations**:
- `LocalSandboxProvider` - Local filesystem execution. `acquire(thread_id)` returns a per-thread `LocalSandbox` (id `local:{thread_id}`) whose `path_mappings` resolve `/mnt/user-data/{workspace,uploads,outputs}` and `/mnt/acp-workspace` to that thread's host directories, so the public `Sandbox` API honours the `/mnt/user-data` contract uniformly with AIO. `acquire()` / `acquire(None)` keeps the legacy generic singleton (id `local`) for callers without a thread context. Per-thread sandboxes are held in an LRU cache (default 256 entries) guarded by a `threading.Lock`. All Local variants, including `local-run:{run_id}`, use the same central classifier for confinement, masking, and host-bash guards. Legacy global-custom mounts are gated by the same user-scoped skill discovery rule used for prompt/list visibility; providers must not infer visibility from raw directory presence alone. Private exact-Skill runs use a separate `(user, thread, run)` sandbox entry whose skills root replaces all global Skill mappings. Worker cleanup retries bounded transient removal failures, marks the runtime closed only after success, and logs persistent failure generically without exposing the host path. Because host bash can mutate translated host paths outside file-tool read-only checks, Local private exact runs fail closed when `sandbox.allow_host_bash=true`; providers without true run-scoped read-only mount support fail capability preflight before model construction.
- `AioSandboxProvider` (`packages/harness/deerflow/community/`) - Docker-based isolation. Active-cache and warm-pool entries are checked with the backend during acquire/reuse; definitively dead containers are dropped from all in-process maps so the thread can discover or create a fresh sandbox instead of reusing a stale client. Backend health-check failures are treated as unknown, not dead; local discovery likewise treats an unverifiable container as not adoptable and falls through to create rather than failing acquire. `get()` remains an in-memory lookup for event-loop-safe tool paths. Legacy global-custom mounts follow the same shared visibility helper as local and remote providers.
- `BoxliteProvider` (`packages/harness/deerflow/community/boxlite/`) - BoxLite micro-VM isolation. The `boxlite` runtime is optional (`deerflow-harness[boxlite]`) and lazy-imported only when this provider is selected. The provider owns one private asyncio event loop on a daemon thread because BoxLite handles are loop-affine; sync `Sandbox` calls marshal onto that loop with `run_coroutine_threadsafe`.
  Boxes are named deterministically from `user_id:thread_id`, released into an in-process warm pool after each agent turn, and reclaimed only by the same user/thread. Warm-pool health checks use a short explicit timeout and forward that timeout through both BoxLite `exec(timeout=...)` and the private-loop `.result(timeout)` bridge so a hung VM cannot pin the per-thread acquire lock indefinitely.
  `sandbox.replicas` caps active + warm VMs per gateway process; if capacity is exhausted, only warm-pool VMs are evicted. `sandbox.idle_timeout` stops idle warm VMs after the configured seconds. `reset()` is intentionally a lightweight registry clear for `reset_sandbox_provider()` and does not close boxes, stop the idle reaper, or close the private loop; full teardown remains `shutdown()`.


**Shared warm-pool lifecycle:** community sandbox providers that keep released sandboxes alive for fast reuse share `deerflow.community.warm_pool_lifecycle.WarmPoolLifecycleMixin`. The mixin owns the common `DEFAULT_IDLE_TIMEOUT=600`, `IDLE_CHECK_INTERVAL=60`, `DEFAULT_REPLICAS=3`, idle-checker loop, warm-pool expiry, oldest-warm eviction, replica counting, and soft-cap logging. Providers remain responsible for their own active registries, creation/discovery, health checks, and destroy hook (`_destroy_warm_entry`): AIO destroys `SandboxInfo` through its backend; Boxlite closes loop-affine `BoxliteBox` handles. AIO keeps active-idle cleanup outside the mixin and delegates only warm-pool expiry to the shared helper.

**Virtual Path System**:
- Agent sees: `/mnt/user-data/{workspace,uploads,outputs}`, `/mnt/skills`
- Physical: `backend/.deer-flow/users/{user_id}/threads/{thread_id}/user-data/...`, `deer-flow/skills/`
- Translation: `LocalSandboxProvider` builds per-thread `PathMapping`s for the user-data prefixes at acquire time; `tools.py` keeps `replace_virtual_path()` / `replace_virtual_paths_in_command()` as a defense-in-depth layer (and for path validation). AIO has the directories volume-mounted at the same virtual paths inside its container, so both implementations accept `/mnt/user-data/...` natively.
- Detection: `is_local_sandbox()` accepts `sandbox_id == "local"` (legacy / no-thread), `sandbox_id.startswith("local:")` (per-thread), and `sandbox_id.startswith("local-run:")` (private exact-Skill run)

**Sandbox Tools** (in `packages/harness/deerflow/sandbox/tools.py`):
- `bash` - Execute commands with path translation and error handling. For `LocalSandbox` (host bash), POSIX output is captured through bounded pipe-drain threads and stdin is `/dev/null`, so a backgrounded long-lived process (`server &`) returns immediately instead of blocking the turn on an inherited pipe, while unredirected background output is drained without growing anonymous temp files. Commands that read stdin get immediate EOF. The command runs in its own process group with a wall-clock timeout (`sandbox.bash_command_timeout`, default 600s); on timeout the whole group is killed and the agent gets a notice telling it to background long-lived processes. The bash tool description itself also instructs the model to background long-lived processes (e.g. servers) up front so it doesn't waste the turn waiting on a foreground server. See `LocalSandbox.execute_command` / `_run_posix_command` and `bash_tool`'s docstring.
- `ls` - Directory listing (tree format, max 2 levels)
- `read_file` - Read file contents with optional line range
- `write_file` - Write/append to files, creates directories; overwrites by default and exposes the `append` argument in the model-facing schema for end-of-file writes; subject to the read-before-write gate when `read_before_write.enabled` (see Middleware Chain)
- `str_replace` - Substring replacement (single or all occurrences); same-path serialization is scoped to `(sandbox.id, path)` so isolated sandboxes do not contend on identical virtual paths inside one process; subject to the read-before-write gate when `read_before_write.enabled` (see Middleware Chain)

### Subagent System (`packages/harness/deerflow/subagents/`)

**Built-in Agents**: `general-purpose` (all tools except `task`) and `bash` (command specialist)
**Execution**: Dual thread pool - `_scheduler_pool` (3 workers) + `_execution_pool` (3 workers)
**Concurrency**: `MAX_CONCURRENT_SUBAGENTS = 3` enforced by `SubagentLimitMiddleware` (truncates excess tool calls in `after_model`); default subagent timeout `subagents.timeout_seconds=1800` (30 min) and built-in `general-purpose` `max_turns=150` (raised from 100/15-min so deep-research subtasks stop hitting `GraphRecursionError` out of the box)
**Flow**: `task()` tool → `SubagentExecutor` → background thread → poll 5s → SSE events → result
**Events**: `task_started`, `task_running`, `task_completed`/`task_failed`/`task_timed_out`
**Guardrail caps & `stop_reason` (#3875 Phase 2)**: three independent axes can end a subagent run early, and all now surface *why* through one additive field rather than a new status enum. **Turn axis**: `recursion_limit` on the subagent `run_config` equals `max_turns`, so exhausting the turn budget raises `GraphRecursionError` from `agent.astream`; `executor.py::_aexecute` catches it specifically (before the generic `except Exception`). **Token axis**: `TokenBudgetMiddleware` is attached per-agent via `build_subagent_runtime_middlewares` from `subagents.token_budget` (default 2,000,000 tokens, warn at 0.7, hard-stop at 1.0 — a backstop against a subagent that burns tokens on trivial work). It does *not* raise: at the hard-stop threshold it strips the in-flight turn's tool calls, forces `finish_reason="stop"`, and lets the run complete naturally with a final answer. **Loop axis**: `LoopDetectionMiddleware` (attached at the same point) catches repeated identical tool-call sets — or one tool *type* called many times with varying args — and its hard-stop likewise strips `tool_calls` and forces a final answer without raising, recording `loop_capped`. Each guard exposes its cap on a per-`run_id` `consume_stop_reason(run_id)` accessor; `_aexecute` collects **every** middleware with that method (duck-typed via `hasattr`, so the executor has no import coupling to the guard classes) and surfaces the first non-`None` reason — adding a future guard needs no executor change. **Surfacing**: whichever axis fired, `_aexecute` stamps a normal status plus an additive reason — `completed` + `stop_reason=token_capped|turn_capped|loop_capped` when a usable final answer (or partial recovered from the last streamed chunk via `_extract_final_result` → `utils/messages.py::message_content_to_text`, returning a `"No response Generated"` sentinel when no text survived) was produced; `failed` + `stop_reason=turn_capped` when nothing usable survived. `SubagentResult.stop_reason` flows through `task_tool.py::_task_result_command` → `format_subagent_result_message` (renders `Task Succeeded (capped: ...)` / `Task failed (capped: ...)`) and `make_subagent_additional_kwargs`, which stamps the additive `subagent_stop_reason` key alongside the normal `subagent_status`. **Why additive, not an enum**: a new status value would break v1 consumers; an optional field is ignored by older frontends and ledger readers, so the cross-language contract (`contracts/subagent_status_contract.json` v2 + `subagents/status_contract.py` + `frontend/.../subtask-result.ts`, pinned by `test_status_values_match_contract` / `test_stop_reason_values_match_contract`) stays backward-compatible. The durable delegation ledger captures `stop_reason` onto the entry and renders model-facing guidance ("hit a guardrail cap with a partial result; reuse it, retry tighter, or raise the per-agent budget (`max_turns` / `token_budget`)") so the lead reuses a capped completion knowingly instead of mistaking it for a clean one. (Phase 1 shipped this surfacing as a `MAX_TURNS_REACHED` status enum in #3949; Phase 2 replaced that enum with the additive `stop_reason` field per the agreed design — the `max_turns_reached` status value and `SubagentStatus.MAX_TURNS_REACHED` are gone.)
**Step capture & persistence (#3779)**: `executor.py` captures both assistant turns (`AIMessage`) **and** tool outputs (`ToolMessage`) via `subagents/step_events.py::capture_new_step_messages`, which walks the *newly-appended tail* of each `stream_mode="values"` chunk (not just `messages[-1]`) so a multi-tool-call turn — where LangGraph's `ToolNode` appends several `ToolMessage`s in one super-step — keeps every tool output instead of dropping all but the last. `runtime/runs/worker.py::_SubagentEventBuffer` additionally persists these `task_*` custom events to the `RunEventStore` as `subagent.start`/`subagent.step`/`subagent.end` (`category="subagent"`, `task_id` in `metadata`). It **batches** writes via `put_batch` (flushing on a terminal `subagent.end`, at `FLUSH_THRESHOLD` events, and in the worker's `finally`) rather than one `put()` per step, since `put()` is a documented low-frequency path (per-thread advisory lock per call) and a deep subagent (`max_turns=150`) emits hundreds of steps on the hot stream loop. `build_subagent_step` caps both the per-step `text` and each tool call's serialized `args` at `SUBAGENT_STEP_MAX_CHARS` (flagged `truncated` / `args_truncated`) so a large `write_file`/`bash` payload can't produce an unbounded row. The dedicated category keeps them out of `list_messages` (the thread feed) while `list_events` returns them for the frontend's fetch-on-expand backfill. `list_events` accepts `task_id` (filters on `metadata["task_id"]` — SQL-side in `DbRunEventStore` via `event_metadata["task_id"].as_string()`, in-memory in the JSONL/memory stores) plus an `after_seq` forward cursor, so the card pages through one subagent's steps without the run-wide `limit` truncating the tail (no schema migration: the filter rides the existing run-scoped index). `step_events.py` is a pure, unit-tested layer (`build_subagent_step` / `subagent_run_event`).
**Deferred MCP tools** (if `tool_search.enabled`): `SubagentExecutor._build_initial_state` assembles deferral after policy filtering via the shared `assemble_deferred_tools` (fail-closed), appends the `tool_search` tool, injects the `<available-deferred-tools>` section into the subagent's `SystemMessage`, and threads the setup to `_create_agent`, which attaches `McpRoutingMiddleware` (when PR1 routing metadata matches deferred tools) before `DeferredToolFilterMiddleware` through `build_subagent_runtime_middlewares(...)`. Subagents thus withhold full MCP schemas until promotion, same as the lead agent; each task run gets a fresh `ThreadState` so promotion is isolated per run
**Checkpointer isolation**: Subagent graphs are compiled with `checkpointer=False` to avoid inheriting the parent run's checkpointer, since subagents are one-shot and never resume.

### Tool System (`packages/harness/deerflow/tools/`)

`get_available_tools(groups, include_mcp, model_name, subagent_enabled)` assembles:
1. **Config-defined tools** - Resolved from `config.yaml` via `resolve_variable()`
2. **MCP tools** - From enabled MCP servers (lazy initialized, cached with mtime invalidation)
3. **Built-in tools**:
   - `present_files` - Make output files visible to user (only `/mnt/user-data/outputs`)
   - `ask_clarification` - Request clarification (intercepted by ClarificationMiddleware, which preserves text fallback and adds `artifact.human_input` for Web UI Human Input Cards)
   - `view_image` - Read image as base64 (added only if model supports vision)
   - `setup_agent` - Bootstrap-only: persist a brand-new custom agent's `SOUL.md` and `config.yaml`. Bound only when `is_bootstrap=True`.
   - `update_agent` - Custom-agent-only: persist self-updates to the current agent's `SOUL.md` / `config.yaml` from inside a normal chat (partial update + atomic write). Bound when `agent_name` is set and `is_bootstrap=False`.
4. **Subagent tool** (if enabled):
   - `task` - Delegate to subagent (description, prompt, subagent_type)

Scheduled-task runtime note:
- Scheduled background runs set `context.non_interactive=true` and therefore exclude `ask_clarification` from the lead-agent tool list. This keeps scheduler-triggered runs from stalling on human confirmation mid-execution. `non_interactive` is an internal-only context key: it is merged from `body.context` only when the request authenticated as the process-internal user (the scheduler path), never from arbitrary HTTP/IM clients.
- Project Automation dispatch calls the private runtime adapter with a server-supplied deterministic Run ID and `server_context={"non_interactive": true}`. `start_private_run()` strips all client authority first and then admits that one trusted server flag; public request models expose neither `run_id` nor `server_context`, and client-provided `non_interactive` is always discarded.

**Community tools** (`packages/harness/deerflow/community/`): optional integrations, each in its own subpackage and wired through `config.yaml`. Documented examples:
- `tavily/` - Web search (5 results default) and web fetch (4KB limit)
- `jina_ai/` - Web fetch via Jina reader API with readability extraction
- `firecrawl/` - Web scraping via Firecrawl API
- `image_search/` - Image search via DuckDuckGo
- `aio_sandbox/` - Docker-based isolation (`AioSandboxProvider`)

Additional providers also live here (`boxlite`, `brave`, `browserless`, `crawl4ai`, `ddg_search`, `e2b_sandbox`, `exa`, `fastcrw`, `groundroute`, `infoquest`, `searxng`, `serper`); see each subpackage for specifics.

**ACP agent tools**:
- `invoke_acp_agent` - Invokes external ACP-compatible agents from `config.yaml`
- ACP launchers must be real ACP adapters. The standard `codex` CLI is not ACP-compatible by itself; configure a wrapper such as `npx -y @zed-industries/codex-acp` or an installed `codex-acp` binary
- Missing ACP executables now return an actionable error message instead of a raw `[Errno 2]`
- Each ACP agent uses a per-thread workspace at `{base_dir}/users/{user_id}/threads/{thread_id}/acp-workspace/`. The workspace is accessible to the lead agent via the virtual path `/mnt/acp-workspace/` (read-only). In docker sandbox mode, the directory is volume-mounted into the container at `/mnt/acp-workspace` (read-only); in local sandbox mode, path translation is handled by `tools.py`

### MCP System (`packages/harness/deerflow/mcp/`)

- Uses `langchain-mcp-adapters` `MultiServerMCPClient` for multi-server management
- **Lazy initialization**: Tools loaded on first use via `get_cached_mcp_tools()`
- **Cache invalidation**: Detects config file changes via mtime comparison
- **Transports**: stdio (command-based), SSE, HTTP
- **OAuth (HTTP/SSE)**: Supports token endpoint flows (`client_credentials`, `refresh_token`) with automatic token refresh + Authorization header injection
- **Routing hints**: `extensions_config.json -> mcpServers.<server>.routing` and
  `tools.<original_tool_name>.routing` are soft preference metadata. The effective
  routing is resolved while `mcp/tools.py::get_mcp_tools()` still has both
  `source_name` and the original MCP tool name, then stored on `tool.metadata`
  under `deerflow_mcp_routing`. Prompt rendering uses
  `tools/builtins/tool_search.py::get_mcp_routing_hints_prompt_section`, which
  references `tool_search` when a hinted MCP tool is currently deferred; do not
  add a parallel routing middleware for PR1-style preference hints.
- **Stdio file outputs**: Persistent stdio sessions are scoped by `user_id:thread_id`. For stdio transports only, DeerFlow pins the subprocess default `cwd` to the thread workspace and `TMPDIR`/`TMP`/`TEMP` to `workspace/.mcp/tmp/`, unless the operator explicitly configured `cwd` or temp env values. SSE/HTTP transports skip this filesystem prep entirely.
- **Stdio path translation**: MCP-returned local file references are not copied. If a `ResourceLink` or conservative free-text path resolves to an existing file inside the thread's mounted user-data tree, it is translated deterministically to `/mnt/user-data/...`; paths outside that tree remain unchanged.
- **Runtime updates**: Gateway API saves to extensions_config.json; the Gateway-embedded runtime detects changes via mtime

### Skills System (`packages/harness/deerflow/skills/`)

- **Location**: `deer-flow/skills/{public,custom}/`
- **Format**: Directory with `SKILL.md` (YAML frontmatter: name, description, license, allowed-tools, required-secrets)
- **Loading**: `load_skills()` recursively scans `skills/{public,custom}` for `SKILL.md`, parses metadata, and reads enabled state from extensions_config.json
- **Read-only Web preview content**: Admin-only `GET /api/skills/content/{name}` resolves only a `Skill` visible through the current user's storage, validates that `Skill.skill_file` is an allowed regular `SKILL.md`, and offloads discovery, path checks, and the filesystem read with `asyncio.to_thread`. The existing list response, `GET /api/skills/{name}` metadata response, and embedded `DeerFlowClient` skill contracts remain unchanged.
- **Injection (legacy / default)**: Enabled skills are listed in the agent system prompt with full metadata and container paths (`<available_skills>` block). Controlled by `skills.deferred_discovery: false` (default).
- **Deferred discovery** (`skills.deferred_discovery: true`): Skills are listed by name only in a compact `<skill_index>` block, keeping the system prompt prefix-cache friendly. The agent calls the `describe_skill` tool at runtime to fetch full metadata for skills it wants to use, then loads the SKILL.md via `read_file`. Two new modules support this path:
  - `skills/catalog.py` — `SkillCatalog` (immutable, searchable; query forms: `select:a,b`, `+prefix`, free-text regex); `select:` returns all requested skills without a result cap; other modes cap at `MAX_RESULTS=5`.
  - `skills/describe.py` — `build_describe_skill_tool(catalog)` builds the `describe_skill` tool as a closure; `build_skill_search_setup(skills, enabled, ...)` produces a `SkillSearchSetup(describe_skill_tool, skill_names)` that is wired into both the LangGraph agent factory (`agent.py`) and the embedded client (`client.py`).
- **Slash activation**: `/skill-name task` loads that enabled skill's `SKILL.md` for the current model call only. The resolver rejects leading whitespace, missing separators, reserved channel commands (`/new`, `/help`, `/bootstrap`, `/status`, `/models`, `/memory`, `/goal`), disabled skills, and skills outside a custom agent's whitelist.
- **Installation**: `POST /api/skills/install` extracts .skill ZIP archive to custom/ directory
- **SkillScan**: `packages/harness/deerflow/skills/skillscan/` is the native deterministic scanner for `.skill` archives and agent-managed skill writes. It runs offline before the LLM scanner, emits structured findings (`rule_id`, `severity`, `file`, `line`, `message`, `remediation`, redacted `evidence` — category/analyzer are encoded in the `rule_id` prefix), blocks `CRITICAL`, and passes warning findings into `scan_skill_content()`. `scan_archive_preflight()` / `scan_skill_dir()` are pure sync functions (dispatch off the event loop); `enforce_static_scan()` keeps the backward-compatible findings-only API and applies the `skill_scan.enabled` kill switch, while `enforce_static_scan_result()` applies the same blocking policy and returns the complete `ScanResult`, including `scanner_errors`. Durable shared-asset ingestion must use the result API and reject any non-empty errors. Do not add Semgrep/OpenGrep or YAML rule-engine dependencies to the core path; Phase 1 rule specs live in Python constants next to their analyzers in `skillscan/orchestrator.py`.

#### Request-Scoped Secrets (`required-secrets`)

Lets a caller pass per-request, short-lived end-user credentials (e.g. an ERP token) to a skill's sandbox scripts without the value entering the prompt, tool arguments, the executed command string, or traces (issue #3861).

- **Declare**: a skill lists the secrets it needs in `SKILL.md` frontmatter — `required-secrets:` as a string list or `{name, optional}` mappings. `name` is both the lookup key and the env var name exposed to scripts. Parsed by `skills/parser.py::parse_required_secrets` into `Skill.required_secrets` (`SecretRequirement`); malformed entries are dropped with a warning.
- **Carry**: the caller sends values out-of-band in the run request's `context.secrets` mapping (never a message). `runtime/secret_context.py` owns the contract (`SECRETS_CONTEXT_KEY`, `extract_request_secrets`). The existing `context` passthrough carries it to `runtime.context` without mirroring into `configurable`. `build_run_config` still sets `configurable.thread_id` on the context path — the checkpointer requires it.
- **Bind (point A+)**: `SkillActivationMiddleware._resolve_secret_bindings` recomputes the injection set (`runtime.context[__active_skill_secrets]`) on every model call from two unioned sources, then REPLACES the key. (1) *Slash*: the run's most recent `/skill` activation, persisted as a source on the run context (only the activated skill's **canonical container path**, never its declared secrets) so the whole tool loop after the activation call keeps the binding; a new activation replaces it. Slash reads the genuine user text via `get_original_user_content_text`; `InputSanitizationMiddleware` preserves it (`ORIGINAL_USER_CONTENT_KEY`), so activation fires even after sanitization. (2) *In-context* (autonomous invocation): skills the model actually loaded in this thread — `ThreadState.skill_context` entries. **Both sources resolve the live registry skill by normalized container path on every call** (`_resolve_registry_skill`) and bind only that skill's own declared secrets — enabled + allowlist checked for both; the `secrets-autonomous: false` opt-out (malformed values fail closed to `false`) additionally gates the in-context path but exempts explicit slash. Resolving by registry — not by trusting the source's stored data — is what makes a caller-forged `__slash_skill_secret_source` harmless (`runtime.context` is caller-mergeable; the gateway also strips caller `__`-keys in `build_run_config`), #3938. Authorization is three-gated regardless of activation style: skill **enabled** by the operator × values **supplied per-request** by the caller (`context.secrets`) × names **declared** in frontmatter (∩ semantics). Because the set is recomputed per call, a skill evicted from `skill_context` (capacity) or a caller that stops supplying a value loses injection on the next call. The injected value always comes from the caller's request, never the host environment (scrubbed first — see below), so a declared name that also exists in the host env is safe: the caller's value wins and the host value is dropped (the #3861 per-user-key-overrides-shared-key case). Missing required secrets are logged once per binding change, not injected; binding changes are recorded as a `middleware:skill_secrets` journal event (skill and secret names only, never values).
- **Inject**: `bash_tool` reads the injection set and passes it as `execute_command(env=...)`. Scope is the activation turn/run only — a run without `/skill` activation injects nothing.
- **AIO image requirement**: on `AioSandbox` the env path uses the `bash.exec` API (`POST /v1/bash/exec`), which upstream all-in-one-sandbox only ships since `1.9.3` — older images (including a `latest` tag frozen on the `1.0.0.x` line) 404 the whole `/v1/bash/*` namespace. `AioSandbox` detects the 404, remembers the capability gap on the instance, and fails fast with an actionable upgrade error instead of letting the model retry raw 404s; there is deliberately **no** fallback through the legacy shell path because none keeps the secret values out of the command string (#3921). Regression tests: `tests/test_aio_sandbox.py::TestBashExecUnsupportedFailFast`.
- **Inherited-env scrub**: `execute_command` no longer leaks the Gateway's `os.environ` to skill subprocesses — `env_policy.build_sandbox_env` drops secret-looking names (`*KEY*`/`*SECRET*`/`*TOKEN*`/`*PASSWORD*`/`*CREDENTIAL*`/`*DSN*` + a connection-string denylist like `DATABASE_URL`/`REDIS_URL`/`GH_PAT`) so platform credentials never reach a skill; a skill that needs one must declare it.
- **Leak surfaces sealed** (verified by a real-gateway e2e run — secret reaches the sandbox but none of these): prompt (value never in a message), trace (`tracing/metadata.py` never copies `context`), checkpoint (secrets live on `runtime.context`, not graph state), audit (journal records names only), stdout (`tools.py::mask_secret_values` redacts injected values from bash output), and **run-record persistence + run API** (`services.py::start_run` stores `redact_config_secrets(body.config)` so `runs.kwargs_json` and `RunResponse.kwargs` never carry the secret).
- **Scope / non-goals**: no persistence/vaulting — values are request-scoped and never stored server-side, so long-lived use means the caller re-supplies `context.secrets` on each request while the skill stays in `skill_context`; subagents do not inherit the injection set; the MCP per-user-credential gap (#3322) is a sibling, not covered here. Tests: `tests/test_skill_request_scoped_secrets.py`.

### Model Factory (`packages/harness/deerflow/models/factory.py`)

- `create_chat_model(name, thinking_enabled)` instantiates LLM from config via reflection
- Supports `thinking_enabled` flag with per-model `when_thinking_enabled` overrides
- Supports vLLM-style thinking toggles via `when_thinking_enabled.extra_body.chat_template_kwargs.enable_thinking` for Qwen reasoning models, while normalizing legacy `thinking` configs for backward compatibility
- Supports `supports_vision` flag for image understanding models
- Config values starting with `$` resolved as environment variables
- Missing provider modules surface actionable install hints from reflection resolvers (for example `uv add langchain-google-genai`)

### vLLM Provider (`packages/harness/deerflow/models/vllm_provider.py`)

- `VllmChatModel` subclasses `langchain_openai:ChatOpenAI` for vLLM 0.19.0 OpenAI-compatible endpoints
- Preserves vLLM's non-standard assistant `reasoning` field on full responses, streaming deltas, and follow-up tool-call turns
- Designed for configs that enable thinking through `extra_body.chat_template_kwargs.enable_thinking` on vLLM 0.19.0 Qwen reasoning models, while accepting the older `thinking` alias

### IM Channels System (`app/channels/`)

Bridges external messaging platforms (Feishu, Slack, Telegram, Discord, DingTalk, GitHub) to the DeerFlow agent via Gateway's LangGraph-compatible API.

**Architecture**: Channels communicate with Gateway through the `langgraph-sdk` HTTP client (same as the frontend), ensuring threads are created and managed server-side. The internal SDK client injects process-local internal auth plus a matching CSRF cookie/header pair so Gateway accepts state-changing thread/run requests from channel workers without relying on browser session cookies. Run create/wait/stream calls additionally carry `X-DeerFlow-Runtime-User-Id`, a path-safe execution/storage bucket derived from `_channel_storage_user_id`; Gateway honors it only after internal-token authentication and copies it into a dedicated runtime-storage `ContextVar`. The repository/authorization `_current_user` remains the authenticated owner or internal/default identity, so the runtime bucket never becomes thread/run/event ownership, project/private-work authority, or checkpoint trace attribution.

**Components**:
- `message_bus.py` - Async pub/sub hub (`InboundMessage` → queue → dispatcher; `OutboundMessage` → callbacks → channels)
- `store.py` - JSON-file persistence mapping `channel_name:chat_id[:topic_id]` → `thread_id` (keys are `channel:chat` for root conversations and `channel:chat:topic` for threaded conversations)
- `manager.py` - Core dispatcher. Project-bound ordinary text first resolves the persisted connection through `ProjectInboundDispatcher` and runs the resolved private Thread through `start_private_run`; the initial runnable path returns the final text only. Legacy/non-project routes still create threads through `client.threads.create()`, route commands including `/goal`, keep Slack/Discord on `client.runs.wait()`, stream Feishu/Telegram updates, and use `client.runs.create()` for fire-and-forget policies.
- `base.py` - Abstract `Channel` base class (start/stop/send lifecycle)
- `service.py` - Manages lifecycle of all configured channels from `config.yaml`; during Gateway lifespan it builds the project connection callback service plus inbound resolver/dispatcher from the shared SQL session factory and project-scoped checkpointer
- `slack.py` / `feishu.py` / `telegram.py` / `discord.py` / `dingtalk.py` - Platform-specific implementations (`feishu.py` tracks the running card `message_id` in memory and patches the same card in place; `telegram.py` registers the "Working on it..." placeholder as the stream target and edits it in place via `editMessageText`; `dingtalk.py` optionally uses AI Card streaming for in-place updates when `card_template_id` is configured)
- `github.py` - Webhook-driven GitHub channel. Inbound messages come from `POST /api/webhooks/github`; outbound is log-only because GitHub agents post explicitly with `gh` from their sandbox when they choose to comment or create a PR
- `app/gateway/routers/channel_connections.py` - Browser-facing user connection and disconnect APIs
- `deerflow.persistence.channel_connections` - SQL-backed project+owner-scoped connection, optional credential, OAuth state, and conversation store

**Message Flow**:
1. External platform -> Channel impl -> `MessageBus.publish_inbound()`
   - For GitHub, the webhook router verifies the delivery then calls `fanout_event(bus, ...)`; matching agent bindings publish one `InboundMessage` each instead of a long-polling channel worker.
2. `ChannelManager._dispatch_loop()` consumes from queue
3. For project-bound channel connections, the provider identity is resolved against the connected SQL row; that row supplies the authoritative project and owner, active membership is re-resolved, and the conversation maps to a project-private Thread. Message `owner_user_id`, `project_id`, and headers are routing hints only and cannot replace this lookup. The raw platform user id remains `channel_user_id`. The Gateway forwards `channel_user_id` from `body.context` into the runtime context only (never `configurable`, which is checkpointed), and `bash_tool` exposes it to sandbox commands as the fixed env var `DEERFLOW_CHANNEL_USER_ID` — via a shell-quoted command-string prefix, NOT the `execute_command(env=...)` channel, which is reserved for request-scoped secrets and would switch `AioSandbox` onto the `bash.exec` path (image >= 1.9.3, fresh session per call). Per-call injection keeps group-chat identity correct (one thread/sandbox, many senders) **without depending on the AIO shell's session semantics**: every IM-channel command carries an explicit `export VAR=<id>; ` (valid id) or `unset VAR; ` (empty / non-str / over the 256-char cap, since `body.context` is client-writable). The AIO no-env path reuses a persistent shell session (the reason for the class lock, #1433), so a bare command could otherwise resolve a stale id an earlier sender exported; the `unset` closes the window the length/type guard would open (a dropped id would inherit the previous sender's value). Non-IM runs (no `channel_user_id` in context) are left untouched. Not injected on the Windows local sandbox (its PowerShell/cmd.exe fallback has no `export`/`unset`). Propagates across `task` delegation: `task_tool` captures the dispatching turn's id and the subagent executor forwards it into the subagent's runtime context, same as the guardrail attribution fields. The var is informational, never authorization-grade: any bash command can overwrite it (and web clients can set `body.context.channel_user_id`), so skills must not treat it as authenticated identity. Tests: `tests/test_channel_user_id_env.py`
4. For chat: look up/create thread through Gateway's LangGraph-compatible API
5. Feishu/Telegram chat: `runs.stream()` → accumulate AI text → publish multiple outbound updates (`is_final=False`) → publish final outbound (`is_final=True`)
6. Slack/Discord chat: `runs.wait()` → extract final response → publish outbound
6b. GitHub chat (`ChannelRunPolicy.fire_and_forget=True`): `runs.create()` returns once the run is `pending`; the manager does not wait for the final state and does not publish an outbound. The agent posts its own reply mid-run via `gh` from the sandbox. `ConflictError` on a busy thread still trips the standard `THREAD_BUSY_MESSAGE` path (log-only on GitHub).
7. Feishu channel sends one running reply card up front, then patches the same card for each outbound update (card JSON sets `config.update_multi=true` for Feishu's patch API requirement)
8. Telegram streaming: the "Working on it..." placeholder message is registered as the stream target; non-final updates `editMessageText` it in place (channel-side throttle: 1s in private chats, 3s in groups due to Telegram's 20 msg/min group cap; 4096-char truncation; rate-limited updates dropped); the final update performs the last edit and splits >4096 texts into follow-up messages
9. DingTalk AI Card mode (when `card_template_id` configured): `runs.stream()` → create card with initial text → stream updates via `PUT /v1.0/card/streaming` → finalize on `is_final=True`. Falls back to `sampleMarkdown` if card creation or streaming fails
10. For commands (`/new`, `/status`, `/models`, `/memory`, `/goal`, `/help`): handle locally or query Gateway API
11. Outbound → channel callbacks → platform reply
    - GitHub is the exception: the channel logs the final assistant message and does **not** auto-post it to GitHub. Agents use the sandbox `gh` CLI (`gh issue comment`, `gh pr comment`, `gh pr create`, etc.) for intentional writeback, so silence is cheap when several agents fan out on the same event.

**Owner-scoped file storage**: inbound files, uploads, and output artifacts are staged under the DeerFlow owner's bucket so they land where the agent run reads/writes (`users/{user_id}/threads/{thread_id}/user-data/{uploads,outputs}`). `ChannelManager._handle_chat` resolves the storage owner once via `_channel_storage_user_id(msg)` (sanitized owner id, falling back to `safe(msg.user_id)` for unbound auth-enabled channels — mirroring `_resolve_run_params`'s run identity; `None` only when no identity is available) and threads it as the `user_id=` kwarg through the file pipeline:
- `Channel.receive_file(msg, thread_id, user_id=...)` — owner-bound channels persist downloaded files under the owner's bucket instead of the default bucket
- `_ingest_inbound_files(...)` and the underlying `ensure_uploads_dir` / `get_uploads_dir` — owner-scoped via the same kwarg
- `_resolve_attachments` / `_prepare_artifact_delivery` — resolve output artifacts from the bound owner's bucket
The cached value is reused for both the blocking (`runs.wait`) and streaming (`_handle_streaming_chat`) paths, so uploads and artifact delivery always target the same bucket even if a channel returns a rewritten `InboundMessage` from `receive_file`. The bucket id matches the memory bucket resolved by `_resolve_memory_user_id` (both normalize through `make_safe_user_id`).
The same cached bucket is sent out-of-band on every channel run create/wait/stream request. `start_run()` installs it only in the live runtime context and in the background task's dedicated runtime-storage `ContextVar`; it never overwrites the repository `_current_user` and is not written to run kwargs, checkpoint authority fields, metadata, `RunManager.create_or_reject(user_id=...)`, event ownership, or thread ownership. Bound channels continue to send the separate raw owner header for authorization and persistence, while unbound auth-enabled channels keep distinct sanitized platform-user runtime buckets instead of converging on `default`.

**Configuration** (`config.yaml` -> `channels`):
- `langgraph_url` - LangGraph-compatible Gateway API base URL (default: `http://localhost:8001/api`)
- `gateway_url` - Gateway API URL for auxiliary commands (default: `http://localhost:8001`)
- In Docker Compose, IM channels run inside the `gateway` container, so `localhost` points back to that container. Use `http://gateway:8001/api` for `langgraph_url` and `http://gateway:8001` for `gateway_url`, or set `DEER_FLOW_CHANNELS_LANGGRAPH_URL` / `DEER_FLOW_CHANNELS_GATEWAY_URL`.
- Per-channel configs: `feishu` (app_id, app_secret), `slack` (bot_token, app_token), `telegram` (bot_token), `dingtalk` (client_id, client_secret, optional `card_template_id` for AI Card streaming), `github` (operator kill-switch `enabled`, plus `default_mention_login` for mention-required GitHub triggers)

**User-owned channel connections** (`config.yaml` -> `channel_connections`):
- Disabled by default. It is a user-binding layer on top of the existing `channels.*` runtime config, not a replacement for provider bot credentials.
- M4 product methods are keyed by exact `(project_id, owner_user_id)` scope. Connect callbacks consume the stored project OAuth/connect state and re-resolve membership before writing the connection. Ordinary bound text uses the same Gateway run lifecycle via `start_private_run`; attachment delivery and incremental streaming remain on the legacy compatibility path until a later task.
- `app/gateway/routers/project_connections.py` is mounted at `/api/projects/{project_id}/connections` for list/connect/disconnect; Gateway lifespan initializes the shared `ChannelConnectionRepository` and `ProjectConnectionService`, and connect requires the selected Agent asset reference so the provider callback can persist the server-owned runtime target. The legacy `/api/channels/*` binding API is not adapted back to owner-only repository calls.
- No public IP, OAuth callback URL, or provider webhook route is required by the current implementation.
- Telegram uses a deep-link `/start <code>` flow over the existing long-polling worker. Slack, Discord, Feishu/Lark, DingTalk, WeChat, and WeCom use `/connect <code>` over their existing outbound channel workers.
- Frontend APIs: `GET /api/channels/providers`, `GET /api/channels/connections`, `POST /api/channels/{provider}/connect`, and `DELETE /api/channels/connections/{connection_id}`.
- Browser APIs remain protected by normal Gateway auth/CSRF. Provider messages arrive through the already-configured channel workers.
- Provider-level `connection_status` reflects the user's newest connection row. With no binding it is `not_connected`, except in auth-disabled local mode where a configured running channel reports `connected` because all channel messages already route to the default user.
- Slack replies use the configured operator bot token from `channels.slack` unless per-connection credentials are present; unreadable or corrupt stored credentials are treated as unavailable.
- Telegram, Slack, Discord, Feishu/Lark, DingTalk, WeChat, and WeCom workers resolve incoming platform identities to connection records before reaching `ChannelManager`.
- **Connect-code ordering vs `allowed_users`**: inbound workers consume a valid `/connect <code>` (or Telegram `/start <code>`) **before** applying the `allowed_users` filter, so a newly allowlisted-but-unbound user can bootstrap their first bind via the browser flow. Consequence: `allowed_users` is **not** a bind-time defense — any sender who possesses a valid code can consume it (not only allowlisted users). The bind security model rests on the code's confidentiality: `secrets.token_urlsafe(16)`, 600 s TTL, one-time `consume_oauth_state`, and codes surfaced only in the initiating browser (never echoed to chat). `allowed_users` still gates ordinary (non-bind) messages.
- **Single-active-owner transfer semantics**: an external identity is keyed by `(provider, external_account_id, workspace_id)`. The latest successful bind wins — `upsert_connection` revokes other owners' active rows for the same identity (ownership transfer). This invariant is enforced at the DB layer by the partial unique index `uq_channel_connection_active_identity` (`WHERE status != 'revoked'`), so concurrent connects from different owners cannot both end `connected`; the losing writer retries against the now-visible state. `find_connection_by_external_identity` therefore resolves deterministically.
- See `backend/docs/IM_CHANNEL_CONNECTIONS.md` for provider setup and operational notes.

**GitHub event-driven agents**:
- Configure agent-level bindings in a custom agent's `config.yaml` under `github:`. The global `config.yaml` `channels.github` block is only for the operator kill-switch (`enabled`) and the default mention login; per-agent `installation_id`, `bot_login`, repo bindings, and triggers live with the custom agent.
- Bindings are opt-in by event. `DEFAULT_TRIGGERS` only supplies per-event field defaults for events a binding declared. `GitHubAgentConfig` enforces a single binding per repo per agent; merge trigger maps instead of duplicating a repo.
- Threading is deterministic: fan-out sets `metadata["preferred_thread_id"]` from UUID5 over `(repo, PR/issue number, agent_name)`, and `ChannelManager._create_thread` passes it to `client.threads.create(thread_id=...)`. Different agents on the same PR intentionally get different LangGraph threads. ChannelStore uses `topic_id = f"{number}:{agent_name}"` so each agent's cached mapping is independent.
- Thread-create race recovery is narrow by design: only `langgraph_sdk.errors.ConflictError` (HTTP 409) is treated as a concurrent-create collision and followed by `threads.get(preferred_thread_id)` verification. Other create failures propagate so the delivery can fail/retry rather than caching an unverified mapping.
- Mention-handle precedence for `require_mention` triggers is `trigger.mention_login` → `github.bot_login` → `channels.github.default_mention_login` → `agent.name`. Whitespace-only defaults are treated as unset.
- Set `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY_PATH` (or `GITHUB_APP_PRIVATE_KEY`) to enable installation-token minting. `ChannelManager` mints a short-lived installation token from the binding's `installation_id` on the bus-consumer side and passes the token string in `run_context["github_token"]`; the bash tool exposes it to sandbox commands as `GH_TOKEN` / `GITHUB_TOKEN` via per-call `extra_env`. No global `os.environ` mutation is used, so concurrent GitHub runs for different repos do not clobber each other.
- Tokens are not auto-refreshed past GitHub's 1h TTL. Long-running agents may need to finish GitHub writes before expiry until refresh is reintroduced. If minting fails, the agent still runs without push/write credentials.


### Memory System (`packages/harness/deerflow/agents/memory/`)

**Components**:
- `updater.py` - LLM-based memory updates with fact extraction, whitespace-normalized fact deduplication, and both legacy file and project PostgreSQL save paths
- `queue.py` - Legacy `threading.Timer` queue plus an asyncio-debounced project queue; project items freeze project, owner, Thread, Run, namespace, and membership version at enqueue time
- `prompt.py` - Prompt templates for memory updates
- `storage.py` - Legacy file storage with per-user isolation plus async `ProjectMemoryStorage`, keyed by `(project_id, owner_user_id, namespace)` and protected by optimistic versions

**Project-private path (M4)**:
- A runtime carrying `private_scope` uses PostgreSQL `user_project_memories` and `user_project_memory_facts`; it never falls back to the legacy user file
- The project queue stays on the asyncio event loop, revalidates the captured membership before writing, and passes the captured scope directly to the updater
- Project prompt injection asynchronously reads only the runtime scope and namespace while preserving the existing token budget and reminder format
- `app/gateway/routers/project_memory.py` mounts project Memory status/list/reload/import/export/update/delete operations at `/api/projects/{project_id}/memory`; Gateway lifespan initializes the shared `PrivateMemoryService` on `app.state`
- Before the private-work marker completes, legacy Memory remains the explicit non-project compatibility path; after completion the legacy Memory HTTP router is closed by `PrivateWorkCutoverGuard`. The runnable-first private-work migrator currently rejects non-empty legacy Memory instead of dropping or guessing its scope; Memory payload migration remains a follow-up.

**Per-User Isolation**:
- Memory is stored per-user at `{base_dir}/users/{user_id}/memory.json`
- Per-agent per-user memory at `{base_dir}/users/{user_id}/agents/{agent_name}/memory.json`
- Custom agent definitions (`SOUL.md` + `config.yaml`) are also per-user at `{base_dir}/users/{user_id}/agents/{agent_name}/`. The legacy shared layout `{base_dir}/agents/{agent_name}/` remains read-only fallback for unmigrated installations
- `user_id` is resolved via `get_effective_user_id()` from `deerflow.runtime.user_context`
- The `/api/memory*` endpoints resolve the owner through `_resolve_memory_user_id(request)`: trusted internal callers (IM channel workers carrying the `X-DeerFlow-Owner-User-Id` header, e.g. a bound `/memory` command) act for the connection owner; browser/API callers fall back to `get_effective_user_id()`. The header is only honored after `AuthMiddleware` validated the internal token, mirroring `get_trusted_internal_owner_user_id` used by the threads router
- In no-auth mode, `user_id` defaults to `"default"` (constant `DEFAULT_USER_ID`)
- Absolute `storage_path` in config opts out of per-user isolation
- **Migration**: Run `PYTHONPATH=. python scripts/migrate_user_isolation.py` to move legacy `memory.json`, `threads/`, and `agents/` into per-user layout. Supports `--dry-run` (preview changes) and `--user-id USER_ID` (assign unowned legacy data to a user, defaults to `default`).

**Data Structure** (stored in `{base_dir}/users/{user_id}/memory.json`):
- **User Context**: `workContext`, `personalContext`, `topOfMind` (1-3 sentence summaries)
- **History**: `recentMonths`, `earlierContext`, `longTermBackground`
- **Facts**: Discrete facts with `id`, `content`, `category` (preference/knowledge/context/behavior/goal), `confidence` (0-1), `createdAt`, `source`

**Workflow**:
1. `MemoryMiddleware` filters messages (user inputs + final AI responses). Project runs enqueue an immutable private item; non-project runs capture `user_id` via `get_effective_user_id()` for the legacy queue
2. The project queue debounces with an asyncio task and revalidates membership; the legacy queue debounces with a `threading.Timer`
3. The updater invokes the LLM to extract context updates and facts. Project updates load/save the same scope and optimistic version; legacy updates use the captured `user_id`
4. Project data is committed transactionally to PostgreSQL; legacy data is published atomically with temp-file rename and cache invalidation
5. **Staleness pass** (same LLM invocation as step 3, no extra API call): when `staleness_review_enabled` is `true` and at least `staleness_min_candidates` aged facts exist, `_select_stale_candidates` selects facts older than `staleness_age_days` that are not in `staleness_protected_categories` (default: `correction`), surfaces them in the prompt, and the LLM judges each as KEEP or REMOVE. `_apply_updates` enforces the guardrail unconditionally at apply time: it intersects the LLM-returned removal set with `_select_stale_candidates` output before applying the per-cycle cap (`staleness_max_removals_per_cycle`), so protected and non-aged facts can never be deleted regardless of model behavior or the feature flag setting.
6. Next interaction injects top 15 facts + context into `<memory>` tags in system prompt

**Token counting** (`packages/harness/deerflow/agents/memory/prompt.py`):
- `_count_tokens` budgets the injection. In default `tiktoken` mode, the encoding is loaded lazily and cached.
- Failed tiktoken loads are cached with a timestamp. During the fixed cooldown (`_TIKTOKEN_RETRY_COOLDOWN_S`, 600s), callers fall back to char estimation immediately instead of re-triggering the blocking BPE download; after the cooldown, transient outages can self-heal without a restart.
- In-flight loads are cached as a LOADING sentinel so concurrent callers fall back instead of spawning more blocking threads.
- Set `memory.token_counting: char` to skip tiktoken entirely and use the network-free CJK-aware char estimate.

Focused regression coverage for the updater lives in `backend/tests/test_memory_updater.py`.

**Configuration** (`config.yaml` → `memory`):
- `enabled` / `injection_enabled` - Master switches
- `storage_path` - Path to memory.json (absolute path opts out of per-user isolation)
- `debounce_seconds` - Wait time before processing (default: 30)
- `model_name` - LLM for updates (null = default model)
- `max_facts` / `fact_confidence_threshold` - Fact storage limits (100 / 0.7)
- `max_injection_tokens` - Token limit for prompt injection (2000)
- `token_counting` - Token counting strategy for the injection budget: `tiktoken` (default, accurate but may download BPE data from a public endpoint on first use — can block for a long time in network-restricted environments, see issues #3402/#3429) or `char` (network-free CJK-aware char estimate, never touches tiktoken)
- `staleness_review_enabled` - Enable proactive staleness pruning of aged facts (default: `true`; only triggers when aged candidates exist)
- `staleness_age_days` - Age in days before a fact becomes a staleness candidate (default: 180; range: 1–3650)
- `staleness_min_candidates` - Minimum aged candidates required to trigger a review cycle (default: 3; range: 1–50)
- `staleness_max_removals_per_cycle` - Maximum facts removed in a single cycle; lowest-confidence entries are kept when the LLM requests more (default: 5; range: 1–20)
- `staleness_protected_categories` - Fact categories that are never pruned by staleness review (default: `["correction"]`)

### Reflection System (`packages/harness/deerflow/reflection/`)

- `resolve_variable(path)` - Import module and return variable (e.g., `module.path:variable_name`)
- `resolve_class(path, base_class)` - Import and validate class against base class

### Schema Migrations (`packages/harness/deerflow/persistence/migrations/`)

DeerFlow's application tables (`runs`, `threads_meta`, `feedback`, `users`, `run_events`, plus the four `channel_*` tables) are owned by alembic via a **hybrid bootstrap** strategy. LangGraph's checkpointer tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) live in the same database but are owned by LangGraph and excluded from alembic's view via `migrations/_env_filters.py::include_object`.

**Convention**: every ORM model change (new column, new table, new index) MUST ship as an alembic revision under `migrations/versions/`. The Gateway runs `alembic upgrade head` automatically on startup; users do not run `alembic` manually in production.

**Hybrid bootstrap** (`persistence/bootstrap.py::bootstrap_schema`, invoked from `persistence/engine.py::init_engine`):

| DB state                                  | Action                                  |
|-------------------------------------------|-----------------------------------------|
| empty (no DeerFlow tables)                | `create_all` + `alembic stamp head`     |
| legacy (DeerFlow tables, no `alembic_version`) | `create_all` (baseline tables only, backfill) + `alembic stamp 0001_baseline` + `upgrade head` |
| versioned (`alembic_version` row exists)  | `alembic upgrade head`                  |

The legacy branch handles pre-alembic databases that already have at least one DeerFlow-owned table. `create_all` runs first because stamping at `0001_baseline` makes alembic skip the baseline's own `create_table` DDL on the subsequent upgrade — so any baseline table introduced into `Base.metadata` after the user's DB was first provisioned (e.g. the `channel_*` tables from PR #1930 for users upgrading across multiple releases) would otherwise never be created, and the first request hitting that table would 500 with `no such table`. The backfill is **restricted to `_BASELINE_TABLE_NAMES`** so it does not also create tables that future revisions introduce — those revisions' own `op.create_table` would otherwise fail with `relation already exists`. A guard test pins `_BASELINE_TABLE_NAMES` against `0001_baseline.upgrade()`'s actual output, so editing 0001 to add or remove a table forces a matching update to the constant. Column-level shape (pre-#3658 vs post-#3658 vs manual-ALTER for `token_usage_by_model`) is answered by each `versions/*.py` revision via the idempotent helpers in `migrations/_helpers.py` (`safe_add_column` / `safe_drop_column`) which no-op when the change is already present and `logger.warning` on shape drift. **Adding a new ORM column / table only requires a new revision file — no edit to `bootstrap.py` is needed** *unless* the new revision adds a new baseline table (rare; only happens when a new model is part of the baseline rather than introduced by its own revision).

The empty-DB path keeps using `create_all` because `Base.metadata` is the authoritative PostgreSQL schema source; this avoids keeping a hand-written baseline in lockstep. `0001_baseline.upgrade()` is therefore almost never executed in practice; it exists as a stamp target + chain root.

**Concurrency safety**: PostgreSQL uses `pg_advisory_lock` to serialise concurrent Gateway instances. Column revisions in `versions/` additionally use idempotent helpers (`_helpers.py::safe_add_column`, `safe_drop_column`) so repeated post-baseline changes and retries are no-ops when the change is already present.

**本地初始化与检查**：`make setup-db` 仅从显式 `POSTGRES_ADMIN_URL` 取得连接
`postgres` maintenance database 的管理员连接，并从显式 `DATABASE_URL` 取得目标连接。
它验证 database/role identifier、确认目标 role 已存在、并发安全地创建目标数据库，随后
在覆盖完整 bootstrap 的目标库 advisory lock 内复用 `bootstrap_schema` 升级到 Alembic
head，并用同一个显式 `DATABASE_URL` 幂等执行 LangGraph
`AsyncPostgresSaver.setup()` / `AsyncPostgresStore.setup()`；它不创建 role，也不提升权限。
`make migrate-db` 对已存在目标库执行相同的 ORM/Alembic + LangGraph 完整初始化，但不读取
管理员连接；`make check-db` 只执行参数化只读查询，业务表要求包含
`projects`、`project_memberships`，此外还要求
`checkpoint_migrations`、`checkpoints`、`checkpoint_blobs`、`checkpoint_writes`、
`store_migrations`、`store`。三个命令均不输出 username、password 或完整 URL。Gateway
runtime 仍只验证目标库，绝不自动创建数据库。
完整 bootstrap 使用真正 `AUTOCOMMIT` 的独立 `NullPool` coordination engine，不占用
setup/application pool，也绝不能持有 transaction/virtualxid（LangGraph 使用
`CREATE INDEX CONCURRENTLY`）。专用 session 在取锁前执行 session-level
`SET statement_timeout = 0`、`SET idle_in_transaction_session_timeout = 0` 和
`SET idle_session_timeout = 0`，避免合法并发锁等待或长 DDL 被托管 PostgreSQL 误杀。
`idle_session_timeout` 先通过 `current_setting(..., true)` 探测，旧版 PostgreSQL 不支持时
跳过该项，不能因 unknown parameter 阻断 setup。
M1 明确不使用 PostgreSQL RLS。应用 role 必须是普通非 superuser；路由只能通过可信
`ProjectContext` 和强制作用域 `ProjectRepository` 访问项目数据。建库、schema migration
和 SQLite cutover 是显式 trusted operations，不得把 unscoped repository 暴露给普通路由。
专用 session 关闭自动恢复设置并作为 unlock 兜底，禁止放宽运行期连接超时。
取锁必须使用 `pg_try_advisory_lock` + client-side 短轮询，禁止阻塞式
`pg_advisory_lock`：后者等待时自身持有 virtualxid，会与 LangGraph
`CREATE INDEX CONCURRENTLY` 形成 wait-cycle。未取得锁时取消只关闭专用 session；取得锁
后才在 finally 显式 unlock。

**一次性 SQLite 数据迁移**：`make migrate-sqlite ARGS="..."` 调用
`scripts/migrate_sqlite_to_postgres.py`。脚本只通过 Task 1 的 `mode=ro` /
`PRAGMA query_only=ON` 路径读取 SQLite；固定映射 ORM 表，并使用 LangGraph serializer
解码 checkpoint/writes 后再写 PostgreSQL，禁止把 SQLite BLOB 直接放进 JSONB。
`--dry-run` 执行所有来源的 schema、冲突和语义预检且不写 target/ledger/sequence；实际
迁移前必须用 `--backup-dir` 生成 size/SHA256 已验证的原子备份。ORM、checkpoint writes
与 store 每表 target+ledger 同事务；checkpoint Saver API 无法加入该事务，因此使用冲突
预检、语义 read-back、ledger replay 的安全收敛边界。未知表、非空
`projects`/`project_memberships`、目标值冲突
或校验差异均 fail closed。SQLite provider 包不是运行或迁移依赖；synthetic 测试只使用
stdlib `sqlite3` 和 `JsonPlusSerializer`，真实 PostgreSQL 测试只创建随机
`deerflow_test_*` 数据库。
迁移器接受的每张 ORM source schema 与 primary key 都由显式常量锁定；空表也必须 exact，
不得调用 ORM callable default 猜测缺列。多来源在备份前合并检查 PK、unique index/constraint
和 checkpoint blob identity。dry-run 固定每个来源的 SHA256/size，后续 backup 与正式迁移
必须使用相同 fingerprint，并在结束时复验；非空 `-wal`/`-shm` 直接拒绝。checkpoint 与
blob 使用 `INSERT ... ON CONFLICT DO NOTHING` 后 semantic compare，绝不通过 Saver UPSERT
覆盖目标；Saver 仅用于完整 read-back 验证。
多来源计划严格遵循用户顺序：当前来源只能引用 target、当前来源或更早来源；dry-run 用
累计 planned checkpoint/FK keys 模拟此前来源已落库。正式迁移只读取已经校验的 backup
snapshot，不再重新读取可能变化的原 source。checkpoint+blob 在一个 asyncpg transaction
内直接重建并核对 semantic digest，writes 在同事务核对完整 PK、task_path/channel/value
后才写 ledger，提交后再用 Saver pending_writes 二次验证。channel active identity 的 partial
unique 显式使用 `status != 'revoked'`，revoked 行不参与冲突集合。
Store 在 raw SQL transaction 内验证 timestamp/TTL/value 并提交 ledger 后，还必须通过
`AsyncPostgresStore.aget(..., refresh_ttl=False)` 做公共 API semantic read-back。迁移错误只
输出结构化安全字段（code、table、source SHA 前缀、stable key hash），不得渲染原始异常、
业务值、路径或连接 URL。

跨来源重复用户归并默认关闭，只允许显式方案 A：同时提供
`--reconcile-users-by-email`、精确的 `--reconcile-expected-conflicts`，并按每个
`--source` 的顺序重复提供完整 `--reconcile-source-sha256`。只有首来源中唯一的
`system_admin` 可以按唯一 email 吸收后续来源中同 email、不同 id、角色为 `admin` 的
用户；顺序、fingerprint、数量、角色或唯一性任一不符都必须在 target connect 前失败。
被吸收 users 行不写第二个用户，而以 `status=reconciled`、canonical target key 和决策
digest 写 migration ledger；引用行保留原 source key，并以归一化后的 target key/digest
写 ledger。原 source、dry-run 和 backup 规则不变，snapshot 必须重建出完全相同的决策。
首次写被吸收 users ledger 前还必须确认目标库不存在其 legacy 原 id；若已存在且没有匹配
的 `reconciled` ledger，users 事务立即回滚，禁止把既有目标用户静默吸收。

**M1/M2/M3 PostgreSQL 发布门禁**：`tests/integration/test_m1_postgres_cutover.py` 串联
inventory、backup、0004→SQLite migration→head、完整 runtime schema、默认项目 bootstrap
与来源不变性；`tests/integration/test_project_isolation_postgres.py` 验证跨项目/账户 API、
`ProjectContext` 和 repository scope；`tests/integration/test_m2_project_governance_postgres.py`
使用两个项目验证成员/邀请跨项目读取和 mutation 统一 404 且零写入，并发邀请只能成功兑换
一次，双 Admin 并发降级不能绕过最后一名 active Admin 保护；
`tests/integration/test_m3_shared_assets_postgres.py` 串联系统 catalog 发布、固定 binding、精确
version resolver、MCP 审批隔离、跨项目 404、suspend 和 credential revoke fail-closed。
四个文件只复用
`POSTGRES_TEST_URL` 创建随机
`deerflow_test_*` 数据库，缺变量才 skip，连接或清理失败必须 fail。CI 入口为
`.github/workflows/project-foundation-postgres-tests.yml`，CI 缺少 `POSTGRES_TEST_URL` 时必须在
pytest 前硬失败；本地命令：
`POSTGRES_TEST_URL="$POSTGRES_TEST_ADMIN_URL" uv run pytest -m "postgres and integration" tests/integration -q`。
`POSTGRES_TEST_ADMIN_URL` 必须是仅供测试的 maintenance/admin URL，具备 `CREATEDB`、
terminate connection 和 drop 随机测试库的权限，并且只能指向可丢弃的隔离测试实例；绝不
允许使用 production URL，也不能把普通应用 `DATABASE_URL` 直接复用为该值。

用户引用禁止按列名或未声明的 SQLAlchemy FK 猜测。固定 allowlist 仅为
`threads_meta.user_id`、`runs.user_id`、`run_events.user_id`、`feedback.user_id`、
`scheduled_tasks.user_id`、`channel_connections.owner_user_id`、
`channel_oauth_states.owner_user_id`、`channel_conversations.owner_user_id`。
`channel_connections.bot_user_id` 是外部平台 bot 标识，绝不改写。schema 新增潜在内部
用户引用时，exact schema 校验应先 fail closed，再通过代码、测试和本节文档显式扩展。

**Authoring a new revision**:
```bash
cd backend && POSTGRES_ADMIN_URL="postgresql://.../postgres" make migrate-rev MSG="add foo column to runs"
```
This creates a random `deerflow_autogen_*` PostgreSQL database from the explicit
maintenance URL, upgrades it from migration history, invokes `alembic revision
--autogenerate` against the live ORM models, and drops the random database in a
`finally` block. It never falls back to `DATABASE_URL`, and it rejects non-disposable
database names. Make exports `MSG` as `MIGRATION_MESSAGE`; recipe lines never interpolate
the message into shell syntax, and Python rejects empty, overlong, or control-character
messages without printing them. Review the generated file under `migrations/versions/` and switch raw
`op.add_column` / `op.drop_column` calls to the idempotent helpers from `_helpers.py`
before committing. Production migration execution goes through the same bootstrap API
used by Gateway startup: `make migrate-db` exposes that path explicitly for an existing
database, while `make setup-db` is the only production command allowed to create the
target database.

**Where things live**:
- `migrations/env.py` — PostgreSQL-only alembic environment; delegates filtering to `_env_filters.py`
- `migrations/_env_filters.py::include_object` — drops LangGraph checkpointer tables from alembic's view
- `migrations/_helpers.py` — `safe_add_column` / `safe_drop_column`
- `migrations/versions/0001_baseline.py` — chain root, matches the schema `create_all` produces from `Base.metadata`
- `migrations/versions/0002_runs_token_usage.py` — fixes issue #3682
- `migrations/versions/0004_migration_ledger.py` — per-source-row SQLite migration ledger
- `migrations/versions/0005_project_foundation.py` — PostgreSQL project/membership tables and platform-role rename
- `migrations/versions/0006_project_governance.py` — 项目成员生命周期、邀请、限流和项目删除恢复字段
- `migrations/versions/0007_project_shared_assets.py` — M3 Agent、Skill、MCP、Credential 类型化共享资产 schema、复合约束与数据库 trigger
- `migrations/versions/0012_project_automation_expand.py` — M5 nullable project Automation expansion, migration receipts, and supporting indexes
- `migrations/versions/0013_project_automation_finalize.py` — M5 fail-before-DDL final scope constraints, durable occurrence indexes, and cutover probe
- `persistence/bootstrap.py` — `bootstrap_schema(engine)`, the three-branch decision + PostgreSQL advisory locking
- Tests: `tests/test_persistence_bootstrap.py` (branches), `tests/test_persistence_bootstrap_concurrency.py` (concurrency), `tests/test_persistence_bootstrap_regression.py` (issue #3682), `tests/test_persistence_migrations_env.py` (filter), `tests/blocking_io/test_persistence_bootstrap.py` (asyncio.to_thread anchor)

**M3 共享资产持久化边界**：`deerflow.persistence.shared_assets` 分别定义 Agent、
Skill、MCP 和 Credential 的类型化 ORM，不使用通用 JSONB 资产注册表。逻辑资产以
`system|project` 作用域 CHECK 和作用域内部分唯一索引隔离名称；版本、绑定、credential
slot/grant 均使用复合外键证明资产与版本归属。三类 system binding 只能固定到对应 system
asset 的 `published` 版本；绑定存续期间数据库禁止把该版本降级为其他工作流状态。

版本 payload、Skill 文件、Agent dependency refs 和 MCP credential slots 在版本离开
`draft` 后不可变：draft 创建事务可以写入初始子表行，published 后的 INSERT、UPDATE、
DELETE 均由 PostgreSQL trigger 拒绝。版本工作流只允许 `draft→published`、
`draft→pending_approval` 和 `pending_approval→published|rejected`；`published` 与 `rejected`
均为终态，不能回退到 draft。Credential semantic version 只允许 `active→retired|revoked` 和
`retired→revoked`，revoked 不可逆。publish、archive、suspend、binding、grant 和 revoke 等
影响 resolver 的变更会递增 `asset_catalog_state.generation`；migration 只建单例表，不预置
`id=1`，首次相关事务或 cutover 才创建状态行。ORM 的 trigger 安装 listener 只在完整 M3
metadata `create_all` 时执行，legacy bootstrap 的 baseline selective `create_all` 不得发出任何
M3 DDL。`0007` downgrade 仅允许所有 M3 表（包括状态行）均为空时执行，任一表有数据必须在
任何 schema 变更前拒绝。

**M3 共享资产应用授权与 domain contract**：`app.shared_assets` 定义 `system|project`
scope、`agent|skill|mcp` kind、四态 workflow、不可变选择与 typed resolved snapshot。
项目调用方继续使用 `ProjectContext.require()`；只有认证域 UUID 用户且
`system_role=system_admin` 才能通过 `resolve_asset_actor()` 获得独立的
`SystemAssetGovernanceContext`，该 context 不能伪装成项目 membership context。
`shared_assets.manage_bindings` 与 `mcp.credentials.approve` 仅授予项目 Admin，Editor 仍保留
`shared_assets.edit`。共享资产错误固定为 404/403/409/422/503 且实例只保存公共 code 所需的
类型和 `request_id`。平台 override 的默认治理 sink 只记录 actor、project、asset、version、
action、`request_id` 六类治理元数据；禁止记录 payload、diff、credential metadata 或任何
私有 Thread、run、file、Memory、automation 资源 ID。M6 可替换持久化 sink，但不得改变
service 调用接口。

**M3 共享资产 HTTP API**：项目成员通过 `/api/projects/{project_id}/agents|skills|mcp-servers|credentials`
读取明确分开的 `system_items` 与 `project_items`，并在同一前缀下执行资产版本、发布、状态、
credential 和三类 system binding 操作；所有项目调用先解析不可变 `ProjectContext`。平台治理位于
`/api/admin/assets/*` 与 `/api/admin/projects/{project_id}/assets/*`，只接受
`system_admin` 并构建独立 `SystemAssetGovernanceContext`；带项目 ID 的治理 context 只锁定目标
active project，不创建或伪造 membership，也不能进入 `ProjectRepository` 的成员 scope。router
只转换严格 `extra="forbid"` 的 request/response model、调用 domain service，并把共享资产错误稳定
映射为 404/403/409/422/503。credential 响应仅包含名称、类型、状态、版本与时间元数据，永不返回
plaintext、ciphertext、nonce、key ID、storage locator 或 secret hash。平台 override 的治理事件由
service 在成功事务后写入 `SharedAssetGovernanceEventSink`，router 不接触 payload 或 secret。
`GET /api/admin/assets/credentials/rotation-status` 必须在动态 credential ID route 之前注册，只允许
`system_admin`。它按 rotation CLI 的 eligibility（active logical credential、non-revoked semantic
version、active envelope）统计 active key 已覆盖与待轮换数量，严格响应只含
`eligible_total/current/pending/status`，不得返回 key ID 或 envelope 存储字段。

**M3 Agent domain**：`app.shared_assets.agent_repository.AgentRepository` 不提供裸
`project_id` 的项目资产接口；每个 project 读写都接收可信 `ProjectContext`，并在 SQL 中
同时固定 `agents.scope='project'`、`agents.project_id=context.project_id` 与 active、未过期
membership/project scope。错误 scope、跨项目、陈旧 membership 和不存在统一为
`AssetNotFound`。system Agent 读写使用独立 `SystemAssetGovernanceContext`，项目 context
不能调用 system 写路径。project `list_visible` 在整个列表事务内先共享锁定并校验 project 与
membership 的 ID/user/version/status，陈旧 context 返回 404，且 membership 不能在 project
与 system 两条可见性查询之间失效。`AgentService` 在一个事务内锁逻辑 Agent、校验
`expected_asset_version`、重验 workflow 与依赖 closure、发布 version 并移动
`current_published_version_id`；因此并发 publish 只有一个成功，其他调用稳定返回
`AssetConflict`。publish 在 closure 重验后从锁定的 Agent version 与当前 dependency refs
重建 canonical payload 并校验创建时 checksum，draft refs 漂移固定返回 422。version 创建会在第一次数据库 await 前把 payload collection 复制为不可变
tuple snapshot，后续 checksum、dependency refs 与返回 view 始终使用同一份已校验内容。system Agent 只接受 active system published Skill/MCP version；project
Agent 只接受同项目 active published version，或本项目 enabled binding 固定的 active system
published version。archived dependency 仅保留既有 published version 的历史固定引用，不能
供新 version；suspended dependency 立即不能通过 create/publish closure。Agent version
payload 与 dependency refs 离开 draft 后继续由 PostgreSQL trigger 保证不可变；archive 与
suspend 使用相同 optimistic asset version，其中 suspend 的项目路径限定为 Admin capability。
所有 schema-bound 用户输入在打开 session 前完成长度校验；IntegrityError 只有明确的 Agent
slug/version unique constraint 映射为 409，未知约束与存储错误统一为无 SQL 细节的 503。

**M3 Skill domain**：`app.shared_assets.skill_repository.SkillRepository` 与
`SkillService` 延续 Agent domain 的 context 锁、项目 SQL 强制作用域、system governance、
optimistic asset version 和安全错误映射；跨项目、陈旧 membership 与错误 scope 统一返回
404。每个 Skill version 保存完整目录快照，路径先规范为 Unicode NFC 的 POSIX 相对路径，并以
NFC + casefold identity 拒绝大小写、NFC/NFD 重复和文件/ancestor alias；同时拒绝绝对路径、
`..`、Windows drive、symlink、executable media type 与 ELF/PE/Mach-O fat32/fat64 magic。
每个 path segment 还必须跨 Windows host 安全：拒绝 trailing dot/space、colon/NTFS ADS、Win32
非法字符、control char，以及带 extension/大小写变体的 CON/PRN/AUX/NUL/COM1-9/LPT1-9；
Win32 设备名比较还必须覆盖 COM¹-³/LPT¹-³，但不能为此改变普通 Unicode path 的 NFC
canonicalization。不能依赖 host tempfile 的 alias 行为。根目录必须有 `SKILL.md`，总未压缩大小
上限为 100 MiB。
每个文件保存 SHA-256，version checksum 由按路径
排序的 normalized path、file SHA 和 size 生成，和调用方输入顺序无关。创建与发布均在 worker
thread 中复用现有 frontmatter validator、Skill parser 和强制启用的 SkillScan；任何
scanner/read error 都 fail-closed，数据库只保存 allow/warn decision、rule ID 与 severity
count，不保存 finding evidence。调用 parser/validator 前先用 duplicate-key-rejecting SafeLoader
无日志解析 raw frontmatter；任意 mapping level 的重复 key、非字符串 top-level key 与非
canonical `required-secrets`/`secrets-autonomous` 都稳定返回 422，因此 shadowed secret value
不会进入 parser warning 或持久化；只保存 name/optional schema metadata，不创建 credential 或
grant。
发布事务锁定 Skill/version 后从当前文件行重建并重新验证快照、checksum 与 scan metadata；
publish/load 的 row reconstruction、SHA 与 checksum 全部经 `asyncio.to_thread`，不得阻塞事件
循环。项目加载同时共享锁定 project、
membership、Skill/version 与 system binding，避免与 suspend 或 binding 变更并发穿透。
archived Skill 不再允许创建新版本，但历史 published version 仍可按 ID 加载；suspended Skill
立即不可加载。Skill 文件行离开 draft 后继续由 PostgreSQL trigger 保证不可变。

**M3 Credential crypto 边界**：`app.shared_assets.keyring` 只从
`DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID` 与 `DEER_FLOW_CREDENTIAL_KEYRING_JSON` 加载
密钥；每个 base64 value 必须严格解码为 32 bytes，active key 必须存在。配置失败只返回
稳定、无 secret 的错误，不记录 keyring JSON 或 key material。`app.shared_assets.crypto`
使用 AES-256-GCM、12-byte 随机 nonce 和 canonical JSON；AAD 固定绑定 credential version
UUID、`system|project` scope、project UUID 或 system sentinel，以及 payload schema version。
明文只能包含结构化 `env`、`headers`、`oauth` 顶层字段且最大 64 KiB。未知 key ID、篡改、
错误 AAD、非 canonical 明文或解密后 schema 失败统一为无细节的 decrypt failure，不尝试其他
key。所有持有 key、nonce 或 ciphertext 的 dataclass repr 必须隐藏对应字段；日志、异常和
API 均不得输出 keyring JSON、plaintext、ciphertext 或 nonce。Task 3 仅提供内存 crypto
primitive；envelope 持久化、service、grant resolver 与 rotation CLI 由后续任务负责。

**M3 MCP 与 Credential domain**：`app.shared_assets.mcp_repository`、
`credential_repository` 的项目公开方法只接受可信 `ProjectContext`，并在每次查询中同时固定
active project、membership version、scope 与 project ID；`SystemAssetGovernanceContext`
分别承载 system 治理或显式 project override，不能伪装成项目成员。MCP version 只保存
无 secret 的 transport、command、args、URL、非敏感 env/header、OAuth 协议元数据、routing、
tool override 与 credential slot schema。打开 session 前必须扫描 description、command、args、
URL、env/header、OAuth、routing、tool override 的全部可持久化字符串；key 同时按分隔词和
无分隔 canonical form（如 `CLIENTSECRET`、`PRIVATEKEY`、`APIKEY`、`ACCESSTOKEN`）扫描，header
另外把 `X-Auth` 等 auth carrier 视为敏感 key，URL 结构化拒绝 userinfo 与 sensitive query。
递归 value scan 拒绝 Bearer/Basic authorization、`--api-key=...`/secret assignment 与
private-key PEM 等明确 marker；CLI scan 对 `args` token 与安全 `shlex` 拆分后的 command token
先移除 leading dashes 与 assignment，再复用 definition key 的同一套 canonical + compact taxonomy，
不维护独立 carrier 列表。`APIKEY`、`CLIENTSECRET`、`ACCESSTOKEN`、`PRIVATEKEY` 以及 secret、auth、
authorization、cookie、credential、password/passwd、access key/token 等 option 无论大小写、
underscore/dash、是否带 dash、分离 value 或 assignment form 都在 storage 前拒绝；option 自身即触发，
不读取或记录后续 value。`auth_mode`、`authentication_mode`、`oauth_mode` 是明确的安全 control
allowlist，`--port 8080` 与 OAuth protocol field-name metadata 也继续合法。credential slot payload
schema 中的 secret 名称是 grant contract，
不是 secret value，不能套用 definition key denylist。这些检查稳定 422，异常、repr 与日志不包含
命中的值或完整敏感 URL；绝不写回全局 `extensions_config.json`。项目无 slot MCP 允许 Admin/Editor
直接发布；credential MCP 必须先进入 `pending_approval`，再由项目 Admin 或 system override
批准；system MCP 可由 system admin 从 draft 直接完成同一事务。审批锁序固定为
`project -> MCP asset -> MCP version/slot -> all logical credentials -> all credential versions -> grant`：
先解析全部 provided semantic version 的 immutable logical credential 引用，按 credential UUID
全局排序并锁完所有 logical rows，之后才按 `(credential UUID, credential version UUID)` 排序锁
semantic rows；禁止按 slot 做 `credential -> version` 交错锁。scope、project、slot schema、active
状态与 optimistic asset version 任一不符都以安全的
404/409/422 返回。审批核心参数是可由 JSON object 表示的
`slot_name -> credential_version_id` mapping：service 在 session 前复制并校验 mapping，所有
required slot 必须提供、optional slot 可以省略、unknown slot 拒绝；bulk lock 完成后每个
provided slot 独立重验 credential scope/status/schema，只为 provided slot 创建 grant。单 slot 调用也必须显式
传 `{"primary": version_id}`，不能把一个 credential 暗中复制到全部 slot。

`CredentialService.create/replace/revoke` 通过 Task 3 AES-GCM primitive 保存 active envelope；
replace 锁定逻辑 credential 后创建新的语义 version 与 envelope、retire 旧 version 并移动
current pointer，但不移动既有 grant。retired version 不能创建新 grant，既有 grant 在逻辑
credential 仍 active 时继续有效；revoke 不可逆，并把逻辑 credential 与所有 semantic
version 置为 revoked，使旧 grant 立即不可用。Credential API view 均为 frozen 安全视图，
不包含 plaintext、ciphertext、nonce、key ID 或任何 secret hash；MCP version view 则保留
`payload_checksum`，它是无 secret immutable definition 的 version identity，供 resolver/UI diff
使用，不是 credential secret hash。已知唯一竞争映射为 409；DBAPI 与 SQLAlchemy pool timeout、
crypto/keyring 等 availability 故障只返回无连接 URL 或底层细节的 503，编程型
`InvalidRequestError` 等异常不得被宽泛吞成 503。

**M3 binding 与 resolver**：`BindingService` 只接受可信 `ProjectContext` 或带明确
`project_id` 的 `SystemAssetGovernanceContext`。项目成员必须具有
`shared_assets.manage_bindings`（仅 Admin）；锁序固定为
`project -> binding -> system asset -> exact published version`。binding 永久保留一行，
`enable/upgrade/rollback/disable` 都使用 optimistic binding version；upgrade 只能移动到更高
`version_number`，rollback 只能移动到更低版本，disabled binding 重新 enable 也必须携带当前
expected version。新建、升级或重启时拒绝 archived/suspended system asset，并重新验证 Agent 的
精确 Skill/MCP binding closure 与 MCP grant；已存在 binding 所指 asset 后续 archived 仍可解析，
suspended 则立即 fail closed。系统发布新 version 绝不自动移动项目 binding。

`ProjectAssetResolver` 在同一事务内共享锁定可信 project/membership，并按
`binding -> asset -> exact version -> slot -> sorted credential -> sorted semantic version -> active envelope -> grant`
锁定和重验精确 dependency/credential closure；dependency binding、asset 与 version 分行锁定，
避免 joined nullable row lock 与不稳定锁序。项目资产只解析本项目
`current_published_version_id`，system 资产只解析本项目 enabled binding 的 pinned version。
Agent 含多个 MCP dependency 时必须先收集本事务全部 MCP version 的 slot/grant reference，
具体顺序是先按 `(MCP version UUID, slot name, slot UUID)` 对所有 target 的 slot 行取得
`FOR UPDATE`，全部 slot 锁完成后才能读取任何 active grant reference；该锁必须与 grant
insert/re-pin 外键检查需要的 key-share 冲突，使 required 与空 optional slot 的 reference 集合
在事务内都保持稳定。随后再对 logical credential union 按 UUID 全局排序锁完，并按
`(credential UUID, semantic version UUID)` 锁 semantic version，最后稳定排序锁 active envelope
与 grant 并逐 MCP 验证；禁止按 dependency ref 逐 MCP 获取 closure，避免与 MCP bulk approval 的
全局 `C1→C2` 顺序形成 `C2→C1` 锁环。resolver 与 BindingRepository 复用同一个 batch closure，
单 MCP 入口也只是该 batch primitive 的 adapter。
snapshot 携带闭包全部验证完成后最后读取的 catalog generation、version UUID、checksum、dependency
version UUID 和 grant UUID；MCP definition 只含无 secret 字段，不得包含 plaintext、ciphertext、
nonce、key ID、storage locator 或 credential secret hash。普通 resolver 只锁定并确认 active envelope
存在，不读取其 ciphertext/nonce/key ID，也不解密 secret。

`materialize_mcp_secrets(context, resolved)` 必须显式接收可信 `ProjectContext`；打开 session 前拒绝
错误 context/snapshot 类型，并要求 `shared_assets.execute`；Viewer 即使具备 `shared_assets.read` 也
必须在 session 创建前失败，Runner/Admin 才能 materialize。事务内先锁定并重验 project、membership
status/version 与 capability。
项目 MCP 必须属于 `context.project_id`；system MCP 必须重新锁定本项目 enabled exact binding，且仍
指向 snapshot 的 version。随后按上述全局顺序锁定完整 credential closure，并从数据库锁定的
version/slot 重建无 secret definition，与 snapshot definition 精确比较；不得只信任调用方携带的
checksum、UUID 或 grant。materializer 不维护第二套 closure：必须复用共享 batch 返回的 locked
slot/grant/credential/version/envelope material，在任何 decrypt 前同时校验 snapshot grant IDs 和
locked grant 的 `credential_slot_id`/`credential_version_id` 是否仍与最初 reference 一致，因此
snapshot 的 `catalog_generation` 还必须是非 bool 的非负整数；materializer 在完整 closure、definition
与 grant IDs 校验结束后、加载 keyring 或执行任何 decrypt 前最后读取当前 catalog generation，并要求
与 snapshot 严格相等，否则以零次解密 fail closed。调用方不能用相同 grant ID 或 checksum 绕过
generation freshness；fresh resolve 后才可 materialize 新一代闭包。
在锁取得前已经发生的 grant 原地 re-pin 必须 fail closed；合法 re-pin mutation 必须沿用 Task 6
全局顺序，先按稳定顺序锁 old/new slot、再更新 grant，若 resolver/materializer 已持有 slot
`FOR UPDATE` 则等待其稳定旧 closure 完成后再提交。绕过该顺序直接裸 `UPDATE` grant 不属于应用
mutation contract。向原本空 optional slot 新增 active grant 同样必须等待当前事务释放 slot 锁后
才能提交，不能让 snapshot generation 与 closure 跨代。retired semantic version 的既有 grant
可继续使用，membership/binding
失效、re-pin、错误项目、伪造 definition、revoke、suspend、grant 替换或缺失 required slot 均在
解密前失败。解密边界把 asyncpg UUID 规范化为精确 `uuid.UUID` 以匹配 AES-GCM AAD；返回的短生命
周期 `MaterializedMcpSecrets.by_slot` 使用 `repr=False`，不得写入 cache、日志、API、checkpoint 或
run event。Skill/Agent snapshot 不允许 materialize secret。

resolver-visible mutation 的 generation 由 `0007` PostgreSQL statement trigger 在业务事务内自动
递增；现有 service 不得额外调用 `CatalogStateRepository.bump_generation()` 造成双增。该显式 helper
只供未来没有 trigger 覆盖的 mutation 使用：合法空表读取为 generation `0`，首次 bump 使用
PostgreSQL upsert 建立 singleton 并返回 `1`，后续并发 bump 单调递增。resolver 必须在 snapshot 与
全部 closure 验证结束后最后读取 generation；cache 消费者只能在数据库 generation 未变化的窗口
复用 snapshot。

**M3 system asset runtime cutover**：harness 只依赖
`deerflow.assets.catalog.AssetCatalogProvider` 的安全 snapshot protocol，不得反向导入
`app.*`；Gateway lifespan 在自己的 event loop 安装 `PostgresAssetCatalogProvider`，退出时必须
清空 registry。`asset_catalog_state.cutover_at` 之前保留 legacy file loader；marker 之后 Agent、
Skill、MCP runtime 与 legacy GET 只读取 PostgreSQL published system snapshot，空 catalog、项目
snapshot、坏 checksum/归档或数据库不可用都 fail closed，绝不回退文件。每次 lookup 先读取
generation，变化时整体清空 Agent/Skill/MCP cache；同步 Agent/Skill loader 与工作线程中的 MCP
初始化都必须桥接回 provider owning loop，禁止 asyncpg pool 跨 event loop。

Skill bytes 复用 Task 7 `_verified_archive_files` 校验后，原子物化到 gitignored 的
`skills/custom/.asset-catalog/<generation>/`；slug、POSIX/Windows 路径与 symlink 均需拒绝越界，
逻辑 category 仍为只读 `PUBLIC`。MCP 的无 secret definition 可进入短生命期安全配置；Task 7
materializer 只接受可信 `ProjectContext`；env/header 按 transport 合并合法 connection key，OAuth
只在局部 token manager 中换成 HTTP/SSE `Authorization` header，绝不能把未知 `oauth` key 传给
adapter。明文不得进入 `ExtensionsConfig`、全局 MCP cache、日志、checkpoint 或文件。
cutover 后 legacy Agent/Skill/MCP 文件写 API 在任何 IO 前统一返回
`409 ASSET_CATALOG_CUTOVER` 并引导 `/admin/assets`；legacy custom Skill 读取不再暴露文件内容。

**M3 asset migration 与 credential rotation 运维**：两个脚本都必须显式选择 `--dry-run` 或
`--execute`，禁止 startup 自动导入。`migrate_assets.py` 使用 runtime 相同的 `.deer-flow` /
`DEER_FLOW_HOME`、canonical extensions config resolution，inventory 覆盖 repo 默认/system Agent、
public Skill、MCP 和 user custom Agent/Skill。system source 要求唯一或显式 system-admin actor；
project/user/legacy shared source 必须通过 owner map 固定 active default project，任何未解析或失效映射
都在首个 asset/version write 前 fail closed。execute 先创建 0700 run/backup 目录和 0600 文件；含
secret 的 source 使用 AES-GCM 认证加密，ledger 只保留 source-key hash、原始 checksum/size、相对
restore path 和 backup filename，不记录明文、ciphertext 或 nonce。导入以 source fingerprint 幂等，
Agent 固定首次导入的 dependency version；counts、stored canonical checksum、dependency/credential
closure、decrypt 四类 probe 在同一 cutover transaction 全通过后才写 marker。

`rotate_credentials.py` 只接受 keyring 中存在且等于 active key 的目标 `--key-id`。execute 对 active、
non-revoked semantic version 使用独立事务批次与 `FOR UPDATE SKIP LOCKED`；空 worker page 还必须经过
不跳锁的 authoritative pending barrier，防止临时锁住的低 UUID 被 high-water cursor 永久遗漏。
`resume_cursor` 是脱敏审计 checkpoint，不是排除条件；`max_batches` 未证明耗尽时只写 incomplete。
每个 envelope 在切换前完成 decrypt/schema/re-encrypt/decrypt 验证，tamper 或认证失败回滚当前批，
已提交批可重跑收敛。聚焦 PostgreSQL 测试必须从隔离 maintenance `POSTGRES_TEST_URL` 创建随机
`deerflow_test_*` 并清理，禁止使用 `DATABASE_URL`、业务库或 production 实例。
M3 runtime adapter 不得从 client 字典、project ID 或其他请求字段合成 `ProjectContext`。MCP secret
materialization 只接受 app 内部已经解析出的真实 opaque `ProjectContext`；lead/subagent 只透传该
对象，嵌入式 `DeerFlowClient` 在 cutover 后缺少该对象或收到 client-shaped dict 必须在 tool loading
前 fail closed。把真实 project context 注入项目私有 Thread/run 生命周期是 M4 工作，不属于本适配器。

**Platform and project roles**: `users.system_role` is restricted to
`system_admin|user`; the legacy platform value `admin` is converted by revision 0005.
Project authorization is independent and lives in `project_memberships.role` as
`admin|editor|runner|viewer`. Project IDs are native PostgreSQL UUIDs, while foreign
keys to the pre-existing `users.id` deliberately remain `VARCHAR(36)` UUID strings.
Do not grant project membership implicitly from a platform role.
Revision 0005 refuses to downgrade while either project table contains data; operators
must migrate that data explicitly before retrying. Its users-role constraint lookup is
bound to the current schema's `users` table and fails closed on type or definition drift.

**项目授权边界**：`app.projects` 持有唯一的 role → capability 显式矩阵和不可变
`ProjectContext`。解析器只接受认证域的 UUID user ID，并用一个 SQL statement 联结项目与
active membership；项目不存在、外部用户、平台 `system_admin` 未入项目、暂停项目和非
active 状态统一返回 `project_not_found`。UUID 对象按 project ID 解析，字符串始终按 slug
解析（即使字符串形似 UUID），禁止接收客户端 role/capability 作为授权依据。
`app.projects` 可以依赖 `deerflow.persistence.projects`，harness 永远不得反向 import
`app.*`。
项目 repository 的读取和修改必须同时绑定 context 中的 user/project/membership/version，
并重复检查 active membership 与 active、未暂停项目；陈旧 context 与 outsider 统一
`project_not_found`。列表从 membership 出发，使用 pinned、last-entered、created-at、UUID
四键 keyset cursor，不允许 offset；member count 只聚合 active membership，禁止 N+1。
`resolve_project_context` 和每个 repository public 方法分别拥有一个显式 transaction；
resolver 的单条 joined SELECT 退出后先结束只读 transaction，mutation 再在自己的 transaction
中复查完整 scope/version，并把写入与 read-back 放在同一 transaction，只有 read-back 成功
才 commit。调用方不得预先开启 transaction；该误用的 `InvalidRequestError` 不得伪装成
数据库不可用。

**M4 private-work 授权边界**：`app.private_work.PrivateWorkContext` 只能由精确的服务端
`ProjectContext` 通过 `from_project()` 派生，禁止直接构造和 subclass；工厂签发的同进程对象以
identity 和完整字段快照登记，`resource_scope` 与 revalidator 在读取权限字段前先校验签发状态，
普通 copy/deepcopy/pickle/state hook/dataclass replace、直接 fabrication 与字段篡改均 fail closed。
这是应用消费边界的 provenance 约束，不宣称能阻止任意 trusted reflective code。客户端
`body.config` 的 context/configurable/metadata 与 `body.context` 中所有嵌套的
user/project/owner/membership/role/capability/system-role/runtime `user_role`/project-context 和双下划线内部字段，
必须在进入 runtime 前递归丢弃。`body.checkpoint.checkpoint_map` 与
`body.config.configurable.checkpoint_map` 进入同一个 pre-dependency normalization boundary：map 必须是
Mapping，其他形状在 lifecycle dependency、run/thread persistence、saver 与 worker 前以不回显输入的
固定 400 拒绝；合法 Mapping 只清洗一次并在 persistence/saver/live 间复用。两来源并存时 typed
`body.checkpoint` 覆盖 configurable 的 checkpoint ID/namespace/map，消除 reserved-field 歧义。
stateless 与 thread-scoped 的 stream/wait handler 必须在 thread ID 解析和任何 runtime singleton
读取前调用同一 pure preflight；`_resolve_thread_id()` 自身也不得在未验证 configurable shape 时
调用 `.get()`。两个 thread-scoped stream/wait route 的 decorator 顺序必须保持 router 注册在最外层、
pure preflight wrapper 居中、`require_permission` 在内，使 malformed body 在 thread owner store 与
其他 runtime dependency 前固定失败，同时合法 body 仍完整进入认证授权。direct apply defense 即使 canonical checkpoint ID 为空，也必须先把 normalized config
和 reserved-field removals 安装到调用方 config 后再返回。
`start_run()` 在创建 run/thread record 前生成 authority-sanitized
metadata/config/body-context 副本，并一致用于持久化、API response 与 live config；secret redaction
发生在 authority sanitize 之后。graph input/messages 不经过该递归清洗，消息 `role` 是合法数据。
身份仅由后续服务端认证、trusted internal owner 或独立的 trusted internal runtime/storage header 注入；后者只设置 live execution bucket，不能成为 owner、repository scope、`ProjectContext` 或 `PrivateWorkContext`。普通 HTTP/API 请求即使伪造该 header 也会被忽略，`body.context.user_id` 仍会被清洗。harness 只接收不含 role/capability 的
`deerflow.runtime.PrivateResourceScope`，继续禁止 import `app.*`。每个 mutation 或副作用边界
通过 `PrivateWorkRevalidator.require()` 在调用方已有 transaction 内重验 active project、未暂停
状态、active membership、membership ID/version 和 capability；失效 scope 统一 404，当前 scope
缺 capability 才返回 403。`resolve_project_context_in_transaction()` 不开始、提交或回滚调用方
transaction；`lock=True` 在同一 transaction 内先执行 project `FOR UPDATE`，确认后再执行
membership `FOR UPDATE`，以两条显式语句固定 project → membership 锁序。private-work HTTP 错误只映射固定
`code/message/request_id`，不得拼接底层异常或资源细节。

M4 项目私有 backend 已完成并挂载：`/api/projects/{project_id}/private-work`
负责 readiness、Thread、run/stream/wait、feed、file 和 artifact，项目 Memory 与 Connections
使用各自的 project UUID 路由。Gateway lifespan 只在同一 PostgreSQL 配置上创建 scoped
repositories、`ProjectScopedCheckpointer` 和专用 PostgreSQL private run-event store；普通项目
代码不得取得 raw saver 或 unscoped repository。`.github/workflows/project-foundation-postgres-tests.yml`
固定运行 M1 cutover/isolation、M2 governance、M3 shared assets 与 M4 private-work/migration 六个
真实 PostgreSQL integration 文件，CI 缺少 `POSTGRES_TEST_URL` 时在 pytest 前硬失败。

`make migrate-private-work` 是当前 runnable-first staged cutover：owner map 必须是 legacy owner UUID
到 active project UUID 的直接 JSON 映射，dry-run 零写入，execute 依次完成 0008 expand、分域 ledger、
0009 finalize、0010/0011 与 `cutover_complete` marker。当前 CLI 的 `--backup-dir` 是保留参数，不写
filesystem backup，也不消费 `DEER_FLOW_M4_BACKUP_KEY`；operator 必须在维护窗口前独立完成并保存
PostgreSQL backup proof。非空 legacy filesystem、Memory、file/artifact 或 connection source 在 DDL 前
fail closed。故障分界、幂等重跑和 marker 决策见 `docs/operations/m4-private-work-migration.md`。

M4 项目 Thread 的普通业务入口是 `app.private_work.PrivateThreadService`，repository 的每条
create/get/search/check-access/update/delete SQL 必须把 `project_id`、`owner_user_id` 与
`deleted_at` 放在数据库 predicate/insert authority 中；active 读取同时排除 frozen row，mutation
使用 `version` 做 optimistic compare。legacy user-only Thread API 只能经显式
`TrustedUnscopedThreadMetaStore`，final schema 下 trusted create 还必须在 adapter 构造时给出
project、Agent 和 membership-version authority，禁止猜默认 project/Agent。项目 checkpoint 只能从
`ProjectScopedCheckpointer.for_context()` 取得；sync/async get/tuple/list/put/put-writes/delete 每次先
按 project → membership → Thread 锁序取得 PostgreSQL row lock，并在 raw saver IO 完成前保持
同一 transaction，从而让多 worker 的 read/write/delete 与 membership revoke 串行；写入服务端
`deerflow_private_scope={project_id, owner_user_id}` marker，读取和 list 每项同时核验 thread ID 与
marker。Viewer 对自己的既有 Thread 可读取和删除，但 create/branch/put/put-writes 仍要求
`private_work.create`。删除顺序固定为先将 Thread 标记 deleted +
`checkpoint_delete_status=pending`，再调用 raw saver；raw 删除失败保留不可见并标记
`retry_required`，成功标记 `complete`。Gateway production lifespan 只把 raw saver 放在私有
`app.state._raw_checkpointer`；`get_checkpointer` 仅供 legacy，项目模块不得 import/call，项目依赖使用
`get_project_checkpointer`。legacy delete 也必须经该 getter 取得 production raw saver，先保留不可见
tombstone，再记录 `complete/retry_required`，不得物理删除唯一 authority row；tombstone/frozen row
在 permissive legacy access check 中仍然 fail closed。production legacy factory 不猜 project/Agent，
缺少显式 create authority 时，POST Thread 与 stateless run 必须在 checkpoint 校验、run admission 和
graph launch 前返回稳定 `409 PRIVATE_WORK_CUTOVER`。create 固定锁序为 project → membership，再验
`private_work.create` 与 Agent 可执行性、写 Thread、写 root checkpoint；root write 失败或结果不确定
时先补偿删除 raw checkpoint，branch 同时回滚 PostgreSQL authority hook；只有两者成功才物理清除
补偿 tombstone，失败则保留 `retry_required`。branch 禁止读取宿主 Thread 目录。

已登记的活动项目 run 使用 `AuthorizationBoundary`，不再把 admission 时的
`membership_version` 当成长时运行资格：Admin 降为 Editor/Runner 后 model、checkpoint、tool、MCP
和 sandbox 边界继续按当前 active membership + 当前 execute capability 运行；降为 Viewer、退出、
移除、项目暂停或 pending deletion 时，在治理 transaction 内按 project → membership → active runs
写 `authorization_cancel_requested_at/reason`，commit 后才 best-effort 通知本地 `RunManager`。远端 worker
在每次 model（包括 retry 与 title/summarization/goal 旁路）、tool/MCP dispatch、checkpoint read/write、
sandbox exec/write 及未来 file-finalization hook 前重查 marker、项目状态和当前 role；数据库异常同样
fail closed。harness 只定义 app-agnostic protocol/`AuthorizationRevoked`，不得 import `app.*`。
撤销终态固定为 `interrupted`，公开 reason 只能是 `authorization_revoked`，且不得再调用 interrupted-title
模型；成功等既有终态不写 marker，completion/status 竞争也不能覆盖已撤销 run 的 interrupted 结果。
私有 asset materialization 必须在首次 MCP discovery 前安装同一个 run-bound boundary；run 注册后只允许
把对应 `abort_event` 幂等绑定到该 boundary，禁止先 discovery 再 setter，也禁止持有数据库 transaction
跨远端 MCP 网络调用。PostgreSQL status write 使用单条 marker-aware `UPDATE ... RETURNING status,error`
返回权威终态；`RunManager` 在 store 返回前不得先向 wait/consumer 暴露请求的 success/error，旧 store
仍可沿用 bool/None contract。completion persistence 与 status persistence 必须共用同一条 per-record
写序列，且不得持有 manager 全局锁跨 store I/O；一旦 record 已是
`interrupted/authorization_revoked`，completion 只能补写 token/message 等非安全字段，不得用普通
completion status/error 覆盖内存或 store 的权威撤销终态。legacy/no-store 普通 completion 与
`error=None` 的既有语义保持不变。

离组/移除、项目暂停或 pending deletion 同一治理 transaction 冻结该 project+owner 的 Thread 和
connected channel connection，但绝不物理删除 Thread、file、Memory、credential 或其他私有内容；降为
Viewer 只停止活动 run，不冻结既有内容。原 membership row 通过 invitation rejoin，或项目 restore/resume
后，只恢复相同 project+owner 的 frozen row；revoked connection 永不恢复，且外部 identity 已被另一条
connected row 占用时原 connection 保持 frozen。connection 的全局 active-identity partial unique predicate
在 ORM、migration 与 bootstrap catalog 中统一为 `status = 'connected'`。restore/rejoin 必须先收集本次
所有 owner 的 frozen external identities，以确定性的 signed-int64 key 去重并按数值全局排序取得
PostgreSQL transaction advisory locks，再选择每 identity 的稳定 winner；project restore/resume 必须一次
bulk 调用，禁止按 member 循环拿锁。普通 connection upsert 必须复用同一 identity-lock helper，确保并发
restore 的 loser 保持 frozen 而治理 transaction 仍可提交。普通 identity ownership transfer 只能撤销
真正 `connected` 的其他 owner 并清理其 credential；`frozen` row 及其保留 credential 不得被 normal
connect 转成 revoked 或删除。

M4 项目 Run 继续复用唯一的 `RunManager`/`RunStore`/`RunEventStore`。`RunRecord.scope`
对项目 run 必须是 `PrivateResourceScope`；manager 即使命中内存记录也要比较 scope，后台
status/model/progress/completion 只从已登记的 run record 派生 scope，禁止接收客户端 owner。
PostgreSQL Run/Event/Feedback 的项目入口每条 SQL 都同时包含 `project_id + owner_user_id`；
event 与 feedback 写入先查 scoped parent run，再由 parent 派生 project/owner，忽略 event payload
或 legacy `user_id` 中的 ownership。无 scope 的项目读 fail closed，startup orphan recovery 只走
显式 `list_inflight_trusted_unscoped()`。`RunSnapshotRepository.create_run_with_snapshot()` 在单个
PostgreSQL transaction 中写 run、`run_asset_versions` 和 `run_mcp_grant_snapshots`：root Agent 固定
order 0，随后按 resolver 的 Skill、MCP 稳定顺序写 exact version/checksum/catalog generation；grant
closure 必须直接复用 M3 `lock_mcp_credential_closures(..., load_envelopes=False)` 的全局锁序与完整
校验，允许 active grant 固定到 active/retired semantic version，同时要求 active envelope、MCP/credential
精确同 scope/project 及 slot/version schema 相等；只能从返回的 locked materials 写 ID，不能另写一套
弱化查询。grant snapshot 只含 MCP version、slot、grant、credential-version UUID，绝不读取或持久化
envelope、key ID、nonce、ciphertext、storage locator。asset/closure/generation stale、真实 run conflict 和
数据库不可用必须在 `PrivateWorkContext` 边界分别映射稳定公开错误并携带真实 request ID；session-bound
repository 不得伪造 `"unknown"`。snapshot/event/file 的 run 关联必须依靠数据库复合 FK 拒绝 scope
错配，不能只靠 Python 预检。

M4 项目私有 file/artifact 的唯一 authority 是 PostgreSQL `files/file_chunks/artifacts`。
`PrivateFileRepository` 的 stage/chunk/finalize/read/list/soft-delete SQL 都绑定
`project_id + owner_user_id + thread_id`；chunk 固定最大 1 MiB，并在数据库约束和 finalize 时同时
校验 `size=octet_length(content)`、逐块 SHA-256、连续 index、整文件 size/hash。空文件允许零 chunk。
`ready` 后 bytes/metadata 不可修改；显式删除只写 `status=deleted/deleted_at`，保留 chunk，并允许同
logical path 创建递增的新 version。转换结果必须是 `kind=workspace`，通过同 scope/thread 的复合
self-FK 保存 ready source；普通 upload/output 禁止携带 `source_file_id`。

应用层 upload 不得在等待客户端 async body 时持有数据库 transaction：先短 transaction stage，按
固定 1 MiB chunk 分批 revalidate/commit，最后在一个 transaction 原子 finalize 全批；取消、限制或
数据库失败必须用预生成 exact file IDs shield 等待 staging cleanup；HTTP body 普通异常也必须在
exact cleanup 后脱敏映射为 `PRIVATE_WORK_UNAVAILABLE`，只有 cancellation/BaseException 原样传播；
finalize 是不可取消的全批原子
commit point。private 默认 count/single/total 限额分别是 10/100 MiB/100 MiB，legacy HTTP upload
保持 10/50 MiB/100 MiB。转换在创建 tempfile 或启动 worker 前先重验 `private_work.create`，只在
`0700` 私有临时目录中写 `0600` source；所有可能创建资源或阻塞 IO 的 thread worker 在取消时必须
join 后再 cleanup/rethrow。converter 输出通过 anchored directory FD 的
`O_NOFOLLOW|O_DIRECTORY` 路径遍历和 final `O_NONBLOCK` open，fstat 必须是 `st_nlink=1` regular
file；同一个已验证 fd 直接分块读取，拒绝 symlink ancestor、hardlink、FIFO、越界和 TOCTOU path
swap。读取先以 active scoped Thread join 验证 ready row 与完整首个有界 page，再用 keyset 小页和
短 session 流式输出，不得让慢 consumer 持有 idle transaction；每页重验 membership 和 Thread 的
`deleted_at/frozen_at`，任何 chunk/whole-file
tamper 都以固定 `PRIVATE_WORK_UNAVAILABLE` fail closed 且不回显 logical/host path。
`list_ready` 明确列 Thread 内全部 ready kinds（upload/workspace/output），按
`(logical_path, version, id)` keyset 稳定升序且 limit 只能为 1..100；读取需要
`private_work.read_own`，soft-delete own ready file 也使用 `private_work.read_own`；不存在、跨 scope
或非 ready 统一 404，Viewer 可列出、下载和软删除自己的 ready file，但仍不能 upload/convert。
写入和读取的 MIME 必须是严格有界 ASCII type/subtype/parameter syntax；响应文件名必须清除全部
Unicode category C 字符、使用 RFC 5987 编码并写
`X-Content-Type-Options: nosniff`；HTML/XHTML/SVG、JavaScript/ECMAScript、XML 和 PDF（去参数并
lower 后判断）一律 attachment，artifact metadata 不能把 active file MIME 降级为 inline。

M4 的 authority/service/streaming primitives 已挂载到项目 routes，并接入 sandbox
restore/finalizer。既有 `/api/threads/{thread_id}/uploads` 与
`/api/artifacts` 在 Task 11/12 cutover 前继续使用 owner-scoped host directories；legacy 与 private
默认限额保持独立，并通过纯 `app.upload_contracts` helper 共享 limit 解析；legacy 的 host `path`
response schema 只搬入该纯模块供原 router re-export，不得作为未来 project/private response。安全
header helper 仍可共享，但既有路由不能被误写成 PostgreSQL 项目入口。Thread
delete/freeze 对 file/artifact 的跨资源标记归 Task 8 `thread_service` 集成；Task 7 已保证 inactive
Thread join 后任何 file/artifact bytes 都不可读。

项目 HTTP API 统一挂载 `/api/projects`，使用 request-scoped session 与认证 dependency；
项目 path 只接受 UUID，项目专属的 path/query/body 校验统一返回
`PROJECT_VALIDATION_FAILED`，不改变其他 router。默认项目 bootstrap 使用 transaction-level
advisory lock 和固定 `default-project` slug；只在唯一 `system_admin` 可确定且现有结构完整
时 create/existing，歧义、slug collision 或 partial state 均 fail closed。普通 register
不加入默认项目；首次 initialize 后 bootstrap 失败时由 `setup-db` 安全重试恢复。
首次 initialize 在查询管理员前先取得独立的 PostgreSQL session advisory lock，并在
管理员写入和默认项目 bootstrap 完成后才释放；该锁使用短命 NullPool AUTOCOMMIT 物理连接，
不在 runtime pool 中遗留 session lock 或 idle transaction。锁顺序固定为 initialize lock
再 default-project transaction lock；`setup-db` 只取得后者，系统中不存在反向获取路径，
因此两条恢复/初始化路径不会形成锁环。

### Terminal Workbench / TUI (`packages/harness/deerflow/tui/`)

A terminal-native UI over the embedded harness, exposed as the `deerflow` console script (`[project.scripts]` in `packages/harness/pyproject.toml`). It is a UI shell over `DeerFlowClient` and does **not** fork agent behavior. `textual` is an optional dependency (`deerflow-harness[tui]`; also in the backend dev group); the console script degrades to headless help when it is absent. Full guide: [docs/TUI.md](docs/TUI.md).

**Module layout** (all layers except `app.py` are pure / Textual-free and unit-tested directly):
- `cli.py` — `plan_launch()` (pure launch-mode decision) + headless `--print` / `--json` + `main()` entry point. TTY → TUI, else headless help. Uses an **absolute** `from deerflow.tui.app import run_tui` so the `app.py` module name doesn't trip `test_harness_boundary.py` (which records relative import module names verbatim).
- `view_state.py` — `ViewState` + `reduce(state, action)`, the testable heart. Rows: user / assistant / tool / system. Title captured from `values` events.
- `runtime.py` — `translate(StreamEvent) -> [Action]` (pure) + `stream_actions()` which brackets a run with `RunStarted`/`RunEnded` and turns model errors into an `AssistantError` row.
- `message_format.py` / `command_registry.py` / `input_history.py` / `render.py` / `theme.py` — pure helpers (tool summaries, slash registry + `resolve()`, ↑/↓ history, Rich renderers).
- `app.py` — Textual `App`. Runs `DeerFlowClient.stream()` (sync) on a worker thread and marshals actions to the UI thread via `call_from_thread`. Slash palette with `/goal` management + model/thread modal pickers; priority key bindings gated by `check_action` so they never steal keys from overlays or the composer.
- `session.py` / `persistence.py` — builds the client + checkpointer and the `ThreadMetaWriter`.

**Web UI visibility**: the Web UI lists threads from the `threads_meta` SQL table (user-scoped), not the checkpointer. `persistence.py` writes a `threads_meta` row under the default user (`"default"`) into the same PostgreSQL database the Gateway reads — via the harness-only `deerflow.persistence.engine.init_engine_from_config()` — so TUI sessions appear in the Web UI sidebar **without** running the Gateway. If PostgreSQL is unavailable the best-effort writer becomes a no-op. All DB work runs on one long-lived background event loop (a SQLAlchemy async engine is bound to its creating loop).

**Tests**: `tests/test_tui_*.py` — pure layers via plain pytest, the app/palette/overlays via Textual's pilot harness with a fake in-process session, and `test_tui_persistence.py` for the `threads_meta` round-trip.

### Request Trace Context (`packages/harness/deerflow/trace_context.py`)

Request trace correlation is controlled by `logging.enhance.enabled` at **both** entry points, gated through the shared helper `deerflow.config.app_config.is_trace_correlation_enabled` so the Gateway and embedded paths cannot drift:

- **Gateway HTTP**: `app.gateway.trace_middleware.TraceMiddleware` binds one request-level trace id per HTTP request, inheriting inbound `X-Trace-Id` when present or generating a new id otherwise. The middleware writes the final value to every HTTP response at `http.response.start`, which covers SSE / streaming responses without consuming the body.
- **Embedded / TUI / CLI**: `DeerFlowClient.stream()` mints (or inherits) a request-level trace id per turn only when the flag is on. When it is off, no fresh id is minted — a caller that explicitly wraps `stream()` in `request_trace_context(...)` still opts in, because the downstream `get_current_trace_id()` read propagates that value into Langfuse metadata regardless of the flag. Because `stream()` is a sync generator (which shares the caller's context), the id binding is set/reset around each `next()` step rather than around `yield from`: this keeps LangGraph node execution and its log records inside the binding, while returning control to the caller with the ContextVar restored — avoids cross-request leak between yields and `ValueError: <Token> was created in a different Context` on GC-driven close of an abandoned generator (regression pinned by `tests/test_client_langfuse_metadata.py::test_stream_does_not_leak_trace_id_to_caller_context_between_yields` and `::test_stream_abandoned_generator_close_does_not_raise_cross_context`).

The same ContextVar value is injected into enhanced log records as `trace_id` and into Langfuse metadata as `deerflow_trace_id`.

`logging` is registered as a **restart-required** field
(`STARTUP_ONLY_FIELDS["logging"]`): `configure_logging()` installs the trace-context
filter and enhanced formatter on root handlers only during app.py lifespan startup,
and `TraceMiddleware` captures `logging.enhance.enabled` once when the FastAPI app
is constructed (via `resolve_trace_enabled(get_app_config())` in `create_app()`,
itself a thin alias for `is_trace_correlation_enabled`). This keeps the response
`X-Trace-Id` header, log `trace_id` fields, and Langfuse `deerflow_trace_id`
coherent — a runtime `config.yaml` edit to `logging.enhance.*` needs a Gateway
restart to take effect. The `deerflow_trace_id` chain inherits this guarantee
transitively because every injection point ultimately reads the same
`trace_context` ContextVar that the middleware alone populates. `DeerFlowClient`
reads its own `self._app_config` snapshot (captured at `__init__`) through the
same helper for the embedded gate.

`deerflow_trace_id` is a DeerFlow correlation metadata key, not Langfuse's native
trace id and not a DeerFlow `run_id`. Keep the existing subagent `trace_id` field
separate: that short id is still only for subagent execution logs/status.

### Tracing System (`packages/harness/deerflow/tracing/`)

LangSmith and Langfuse are both supported. The wiring lives in two layers:

- `factory.py::build_tracing_callbacks()` — returns the LangChain `CallbackHandler` list for the providers currently enabled via env vars (`LANGSMITH_TRACING`, `LANGFUSE_TRACING`, etc.). The handlers are attached at the **graph invocation root** for in-graph runs (`make_lead_agent` and `DeerFlowClient.stream` both append them to `config["callbacks"]` before invoking the graph) so a single run produces one trace with all node / LLM / tool calls as child spans. Standalone callers — anything that invokes a model outside such a graph (e.g. `MemoryUpdater`) — keep `create_chat_model`'s default `attach_tracing=True`, which falls back to model-level callback attachment.
- `metadata.py::build_langfuse_trace_metadata()` — builds the Langfuse-reserved trace attributes for `RunnableConfig.metadata`. The Langfuse v4 `langchain.CallbackHandler` lifts these onto the root trace (see its `_parse_langfuse_trace_attributes`), but only when it sees `on_chain_start(parent_run_id=None)` — which is why the callbacks have to live at the graph root, not the model.

**Trace-attribute injection points**: both `runtime/runs/worker.py::run_agent` (gateway path) and `client.py::DeerFlowClient.stream` (embedded path) merge the metadata into `config["metadata"]` right before constructing the graph. `subagents/executor.py::_aexecute` does the same for every subagent run so subagent traces group under the parent thread's session card (carrying the parent `thread_id` → `langfuse_session_id`, the user_id captured at `task_tool` → `langfuse_user_id`, and a `subagent:<normalized-name>` trace name). Caller-supplied keys win via `setdefault`, so an external `session_id` override is preserved. Field mapping:

| Langfuse field         | Source                                       |
|-----------------------|----------------------------------------------|
| `langfuse_session_id` | LangGraph `thread_id`                         |
| `langfuse_user_id`    | Gateway worker repository identity from `get_current_user()`, then persisted `RunRecord.user_id`, then `default`; embedded client uses its effective user. For subagents, captured from `runtime.context` at `task_tool` time via `resolve_runtime_user_id()` |
| `langfuse_trace_name` | `RunRecord.assistant_id` / client `agent_name` (defaults to `lead-agent`); for subagents, `subagent:<name>` (lowercased, `_` → `-`) |
| `langfuse_tags`       | `env:<DEER_FLOW_ENV>` + `model:<model_name>`  |
| `deerflow_trace_id`   | Current request/entry trace id from `deerflow.trace_context`; matches `X-Trace-Id` for enhanced Gateway HTTP requests. Gated by `logging.enhance.enabled` in both gateway and embedded paths via `is_trace_correlation_enabled` — off by default; embedded callers can still opt in per-turn by wrapping `stream()` in `request_trace_context(...)` |

Returns `{}` when Langfuse is not in the enabled providers — LangSmith-only deployments are unaffected. Set `DEER_FLOW_ENV` (or `ENVIRONMENT`) to tag traces by deployment environment. Tests live in `tests/test_tracing_factory.py`, `tests/test_tracing_metadata.py`, `tests/test_worker_langfuse_metadata.py`, `tests/test_client_langfuse_metadata.py`, and `tests/test_subagent_executor.py::TestSubagentTracingWiring`.

### Config Schema

**`config.yaml`** key sections:
- `models[]` - LLM configs with `use` class path, `supports_thinking`, `supports_vision`, provider-specific fields
- `logging.enhance` - Optional request trace correlation (`enabled`, `format`) for Gateway `X-Trace-Id`, log `trace_id`, and Langfuse `deerflow_trace_id`
- vLLM reasoning models should use `deerflow.models.vllm_provider:VllmChatModel`; for Qwen-style parsers prefer `when_thinking_enabled.extra_body.chat_template_kwargs.enable_thinking`, and DeerFlow will also normalize the older `thinking` alias
- `tools[]` - Tool configs with `use` variable path and `group`
- `tool_groups[]` - Logical groupings for tools
- `sandbox.use` - Sandbox provider class path
- `skills.path` / `skills.container_path` - Host and container paths to skills directory
- `skills.deferred_discovery` - When `true`, replaces the full-metadata `<available_skills>` prompt block with a compact `<skill_index>` (names only) and registers the `describe_skill` tool so the agent fetches metadata on demand. Defaults to `false` (legacy full-metadata injection)
- `title` - Auto-title generation (enabled, max_words, max_chars, model_name; null model_name uses fast local fallback, explicit model_name uses the prompt_template LLM path)
- `summarization` - Context summarization (enabled, trigger conditions, keep policy)
- `subagents.enabled` - Master switch for subagent delegation
- `memory` - Memory system (enabled, storage_path, debounce_seconds, model_name, max_facts, fact_confidence_threshold, injection_enabled, max_injection_tokens, staleness_review_enabled, staleness_age_days, staleness_min_candidates, staleness_max_removals_per_cycle, staleness_protected_categories)

**`extensions_config.json`**:
- `mcpServers` - Map of server name → config (enabled, type, command, args, env, url, headers, oauth, description, `routing`, `tools`, `tool_call_timeout`). `routing.mode="prefer"` emits `<mcp_routing_hints>` prompt guidance; if `tool_search` defers the hinted tool, `McpRoutingMiddleware` can also auto-promote matching deferred schemas before the model call. It does not hard-disable other tools.
- `tool_search.auto_promote_top_k` - Global MCP routing auto-promote breadth. Default `3`, clamped to `1..5`; applies only when `tool_search.enabled=true` and only to policy-filtered deferred MCP tools with `routing.mode="prefer"` and non-empty keywords.
- `skills` - Map of skill name → state (enabled)

Both can be modified at runtime via Gateway API endpoints or `DeerFlowClient` methods.

### Embedded Client (`packages/harness/deerflow/client.py`)

`DeerFlowClient` provides direct in-process access to all DeerFlow capabilities without HTTP services. All return types align with the Gateway API response schemas, so consumer code works identically in HTTP and embedded modes.

**Architecture**: Imports the same `deerflow` modules that Gateway API uses. Shares the same config files and data directories. No FastAPI dependency.

**Agent Conversation**:
- `chat(message, thread_id)` — synchronous, accumulates streaming deltas per message-id and returns the final AI text
- `stream(message, thread_id)` — subscribes to LangGraph `stream_mode=["values", "messages", "custom"]` and yields `StreamEvent`:
  - `"values"` — full state snapshot (title, messages, artifacts); AI text already delivered via `messages` mode is **not** re-synthesized here to avoid duplicate deliveries
  - `"messages-tuple"` — per-chunk update: for AI text this is a **delta** (concat per `id` to rebuild the full message); tool calls and tool results are emitted once each
  - `"custom"` — forwarded from `StreamWriter`
  - `"end"` — stream finished (carries cumulative `usage` counted once per message id)
- Agent created lazily via `create_agent()` + `build_middlewares()`, same as `make_lead_agent`
- Supports `checkpointer` parameter for state persistence across turns
- `reset_agent()` forces agent recreation (e.g. after memory or skill changes)
- See [docs/STREAMING.md](docs/STREAMING.md) for the full design: why Gateway and DeerFlowClient are parallel paths, LangGraph's `stream_mode` semantics, the per-id dedup invariants, and regression testing strategy

**Gateway Equivalent Methods** (replaces Gateway API):

| Category | Methods | Return format |
|----------|---------|---------------|
| Models | `list_models()`, `get_model(name)` | `{"models": [...]}`, `{name, display_name, ...}` |
| MCP | `get_mcp_config()`, `update_mcp_config(servers)` | `{"mcp_servers": {...}}` |
| Skills | `list_skills()`, `get_skill(name)`, `update_skill(name, enabled)`, `install_skill(path)` | `{"skills": [...]}` |
| Goals | `get_goal(thread_id)`, `set_goal(thread_id, objective, max_continuations=8)`, `clear_goal(thread_id)` | `{"goal": {...}}` or `{"goal": None}` |
| Memory | `get_memory()`, `reload_memory()`, `get_memory_config()`, `get_memory_status()` | dict |
| Uploads | `upload_files(thread_id, files)`, `list_uploads(thread_id)`, `delete_upload(thread_id, filename)` | `{"success": true, "files": [...]}`, `{"files": [...], "count": N}` |
| Artifacts | `get_artifact(thread_id, path)` → `(bytes, mime_type)` | tuple |

**Key difference from Gateway**: Upload accepts local `Path` objects instead of HTTP `UploadFile`, rejects directory paths before copying, and reuses a single worker when document conversion must run inside an active event loop. Artifact returns `(bytes, mime_type)` instead of HTTP Response. The new Gateway-only thread cleanup route deletes `.deer-flow/threads/{thread_id}` after LangGraph thread deletion; there is no matching `DeerFlowClient` method yet. `update_mcp_config()` and `update_skill()` automatically invalidate the cached agent.

**Tests**: `tests/test_client.py` (77 unit tests including `TestGatewayConformance`), `tests/test_client_live.py` (live integration tests, requires config.yaml)

**Gateway Conformance Tests** (`TestGatewayConformance`): Validate that every dict-returning client method conforms to the corresponding Gateway Pydantic response model. Each test parses the client output through the Gateway model — if Gateway adds a required field that the client doesn't provide, Pydantic raises `ValidationError` and CI catches the drift. Covers: `ModelsListResponse`, `ModelResponse`, `SkillsListResponse`, `SkillResponse`, `SkillInstallResponse`, `McpConfigResponse`, `UploadResponse`, `MemoryConfigResponse`, `MemoryStatusResponse`.

## Development Workflow

### Test-Driven Development (TDD) — MANDATORY

**Every new feature or bug fix MUST be accompanied by unit tests. No exceptions.**

- Write tests in `backend/tests/` following the existing naming convention `test_<feature>.py`
- Run the full suite before and after your change: `make test`
- Tests must pass before a feature is considered complete
- For lightweight config/utility modules, prefer pure unit tests with no external dependencies
- If a module causes circular import issues in tests, add a `sys.modules` mock in `tests/conftest.py` (see existing example for `deerflow.subagents.executor`)

```bash
# Run all tests
make test

# Run a specific test file
PYTHONPATH=. uv run pytest tests/test_<feature>.py -v
```

### Running the Full Application

From the **project root** directory:
```bash
make dev
```

This starts all services and makes the application available at `http://localhost:2026`.

**All startup modes:**

| | **Local Foreground** | **Local Daemon** | **Docker Dev** | **Docker Prod** |
|---|---|---|---|---|
| **Dev** | `./scripts/serve.sh --dev`<br/>`make dev` | `./scripts/serve.sh --dev --daemon`<br/>`make dev-daemon` | `./scripts/docker.sh start`<br/>`make docker-start` | — |
| **Prod** | `./scripts/serve.sh --prod`<br/>`make start` | `./scripts/serve.sh --prod --daemon`<br/>`make start-daemon` | — | `./scripts/deploy.sh`<br/>`make up` |

| Action | Local | Docker Dev | Docker Prod |
|---|---|---|---|
| **Stop** | `./scripts/serve.sh --stop`<br/>`make stop` | `./scripts/docker.sh stop`<br/>`make docker-stop` | `./scripts/deploy.sh down`<br/>`make down` |
| **Restart** | `./scripts/serve.sh --restart [flags]` | `./scripts/docker.sh restart` | — |

**Nginx routing**:
- `/api/langgraph/*` → Gateway embedded runtime (8001), rewritten to `/api/*`
- `/api/*` (other) → Gateway API (8001)
- `/` (non-API) → Frontend (3000)

### Running Backend Services Separately

From the **backend** directory:

```bash
# Gateway API
make gateway
```

Direct access (without nginx):
- Gateway: `http://localhost:8001`

### Frontend Configuration

The frontend uses environment variables to connect to backend services:
- `NEXT_PUBLIC_LANGGRAPH_BASE_URL` - Defaults to `/api/langgraph` (through nginx)
- `NEXT_PUBLIC_BACKEND_BASE_URL` - Defaults to empty string (through nginx)

When using `make dev` from root, the frontend automatically connects through nginx.

## Key Features

### File Upload

Multi-file upload with automatic document conversion:
- Endpoint: `POST /api/threads/{thread_id}/uploads`
- Supports: PDF, PPT, Excel, Word documents (converted via `markitdown`)
- Rejects directory inputs before copying so uploads stay all-or-nothing
- Reuses one conversion worker per request when called from an active event loop
- Files stored in thread-isolated directories under the resolving user's bucket (`users/{user_id}/threads/{thread_id}/user-data/uploads`). For IM channels the owner is threaded explicitly via the `user_id=` kwarg (see IM Channels → Owner-scoped file storage); HTTP/embedded callers resolve it from `get_effective_user_id()`
- Duplicate filenames in a single upload request are auto-renamed with `_N` suffixes so later files do not truncate earlier files
- Gateway HTTP uploads stage bytes as `.upload-*.part` files and atomically replace the destination only after size validation. These staging files are hidden from upload listings, agent upload context, and sandbox listing/search tools, and swept on Gateway startup if a hard crash leaves one behind.
- Gateway HTTP upload/list/delete handlers offload filesystem work through `deerflow.utils.file_io.run_file_io`, a dedicated ContextVar-preserving file IO executor. Non-mounted sandbox uploads acquire sandboxes with `SandboxProvider.acquire_async()` and offload `read_bytes()` plus `sandbox.update_file()` together.
- Agent receives uploaded file list via `UploadsMiddleware`

See [docs/FILE_UPLOAD.md](docs/FILE_UPLOAD.md) for details.

### Plan Mode

TodoList middleware for complex multi-step tasks:
- Controlled via runtime config: `config.configurable.is_plan_mode = True`
- Provides `write_todos` tool for task tracking
- One task in_progress at a time, real-time updates

See [docs/plan_mode_usage.md](docs/plan_mode_usage.md) for details.

### Context Summarization

Automatic conversation summarization when approaching token limits:
- Configured in `config.yaml` under `summarization` key
- Trigger types: tokens, messages, or fraction of max input
- Keeps recent messages while summarizing older ones
- Manual compaction uses `POST /api/threads/{id}/compact`, reuses the same
  `DeerFlowSummarizationMiddleware`, writes a new checkpoint with updated
  `messages` and `summary_text`, and bumps only those channel versions.
  The route shares the per-thread serialization gate used by `/goal` writes
  and run admission so compaction cannot race with goal updates or runs that
  read/write checkpoints.

See [docs/summarization.md](docs/summarization.md) for details.

### Vision Support

For models with `supports_vision: true`:
- `ViewImageMiddleware` processes images in conversation
- `view_image_tool` added to agent's toolset
- Images automatically converted to base64 and injected into state

## Code Style

- Uses `ruff` for linting and formatting
- Line length: 240 characters
- Python 3.12+ with type hints
- Double quotes, space indentation

## Documentation

See `docs/` directory for detailed documentation:
- [CONFIGURATION.md](docs/CONFIGURATION.md) - Configuration options
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) - Architecture details
- [API.md](docs/API.md) - API reference
- [SETUP.md](docs/SETUP.md) - Setup guide
- [FILE_UPLOAD.md](docs/FILE_UPLOAD.md) - File upload feature
- [PATH_EXAMPLES.md](docs/PATH_EXAMPLES.md) - Path types and usage
- [summarization.md](docs/summarization.md) - Context summarization
- [plan_mode_usage.md](docs/plan_mode_usage.md) - Plan mode with TodoList

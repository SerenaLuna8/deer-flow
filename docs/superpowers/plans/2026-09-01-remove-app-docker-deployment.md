# Remove Application Docker Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove application Docker builds and Compose deployment while retaining Docker as an optional Sandbox runtime and moving local Nginx and Sandbox Provisioner files to owned top-level directories.

**Architecture:** Gateway, Worker, Scheduler, Frontend, and Nginx run as host processes. `nginx/nginx.conf` is the sole repository Nginx configuration. Docker remains available only through the AIO Sandbox backend and the standalone `sandbox/provisioner/`; Docker readonly mounts fail closed when Worker and daemon do not share a filesystem view.

**Tech Stack:** GNU Make, POSIX shell, Python 3.12, Pydantic, pytest, Nginx, optional Docker Sandbox, optional Kubernetes Provisioner.

**Spec:** `docs/superpowers/specs/2026-09-01-remove-app-docker-deployment-design.md`

## Global Constraints

- Delete application Dockerfiles, Compose files, application Docker scripts, and the root `docker/` directory.
- Keep `scripts/setup-sandbox.sh`, `scripts/cleanup-containers.sh`, the AIO Docker Sandbox backend, and its valid tests.
- Move `docker/nginx/nginx.local.conf` to `nginx/nginx.conf` and `docker/provisioner/` to `sandbox/provisioner/`.
- Remove `sandbox.compose_dood_p03_v1_verified`; Docker readonly mounts must fail closed when `paths.run_skill_uses_distinct_host_view()` is true.
- Preserve Provisioner path mapping, including `ACT_WEAVE_HOST_BASE_DIR` where it remains part of the Kubernetes Sandbox contract.
- Clean active source, tests, scripts, and docs; do not rewrite historical backup plans/specs.
- Do not modify RAG behavior, database business logic, or the main worktree's unrelated M11 files.

---

### Task 1: Lock the repository layout and remove application containers

**Files:**
- Create: `backend/tests/test_application_runtime_layout.py`
- Move: `docker/nginx/nginx.local.conf` -> `nginx/nginx.conf`
- Move: `docker/provisioner/Dockerfile` -> `sandbox/provisioner/Dockerfile`
- Move: `docker/provisioner/README.md` -> `sandbox/provisioner/README.md`
- Move: `docker/provisioner/app.py` -> `sandbox/provisioner/app.py`
- Delete: `.dockerignore`
- Delete: `backend/Dockerfile`
- Delete: `frontend/Dockerfile`
- Delete: `docker/dev-entrypoint.sh`
- Delete: `docker/docker-compose.yaml`
- Delete: `docker/docker-compose-dev.yaml`
- Delete: `docker/docker-compose.cli-auth.yaml`
- Delete: `docker/docker-compose.dood.yaml`
- Delete: `docker/nginx/nginx.conf`
- Delete: `scripts/docker.sh`
- Delete: `scripts/deploy.sh`
- Modify: `Makefile`
- Modify: `scripts/serve.sh`
- Modify: `scripts/nginx.sh`

**Interfaces:**
- Consumes: the approved file ownership table in the spec.
- Produces: `nginx/nginx.conf`, `sandbox/provisioner/`, local Make targets, and a repository regression test.

- [x] **Step 1: Write the failing repository-layout test**

Create a test that resolves the repository root and asserts the exact boundary:

```python
def test_application_container_deployment_is_not_shipped() -> None:
    forbidden = (
        ".dockerignore",
        "backend/Dockerfile",
        "frontend/Dockerfile",
        "docker",
        "scripts/docker.sh",
        "scripts/deploy.sh",
    )
    assert [path for path in forbidden if (_ROOT / path).exists()] == []


def test_local_nginx_and_optional_sandbox_assets_are_retained() -> None:
    required = (
        "nginx/nginx.conf",
        "sandbox/provisioner/Dockerfile",
        "sandbox/provisioner/README.md",
        "sandbox/provisioner/app.py",
        "scripts/setup-sandbox.sh",
        "scripts/cleanup-containers.sh",
    )
    assert [path for path in required if not (_ROOT / path).is_file()] == []
```

Also assert that Make targets `docker-init`, `docker-start`, `docker-stop`, `docker-logs`, `docker-logs-frontend`, `docker-logs-gateway`, `up`, and `down` are absent, `setup-sandbox` remains, and both Nginx scripts contain `nginx/nginx.conf` without the old path.

- [x] **Step 2: Run the layout test and confirm it fails**

Run: `cd backend && uv run pytest tests/test_application_runtime_layout.py -q`

Expected: FAIL because the old application Docker files exist and the new owned paths do not.

- [x] **Step 3: Move retained assets and delete application deployment assets**

Use `mkdir -p nginx sandbox`, `git mv` for the local Nginx and Provisioner assets, and `git rm` for the exact delete list. After the moves, the root `docker/` directory must no longer exist.

- [x] **Step 4: Remove application Docker targets and repoint local Nginx scripts**

Delete `DOCKER`, Docker help text, and all eight application Docker/Compose targets from `Makefile`; keep `setup-sandbox`. Replace every `docker/nginx/nginx.local.conf` occurrence in `scripts/serve.sh` and `scripts/nginx.sh` with `nginx/nginx.conf`.

- [x] **Step 5: Run the layout test and commit**

Run: `cd backend && uv run pytest tests/test_application_runtime_layout.py -q`

Expected: PASS.

Commit: `refactor: remove application container deployment`

### Task 2: Remove the Compose DooD exception and fail closed

**Files:**
- Modify: `backend/packages/harness/deerflow/config/sandbox_config.py`
- Modify: `backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py`
- Modify: `backend/tests/test_app_config_access_paths.py`
- Modify: `backend/tests/test_aio_private_sandbox_lifecycle.py`
- Modify: `backend/pyproject.toml`
- Modify: `config.example.yaml`
- Delete: `backend/tests/support/p03_compose_dood_probe.py`
- Delete: `backend/tests/test_aio_run_skill_mount_lease_dood.py`

**Interfaces:**
- Consumes: `Paths.run_skill_uses_distinct_host_view() -> bool` and `LocalContainerBackend.runtime`.
- Produces: `AioSandboxProvider.run_readonly_mounts_ready() -> bool` with no Compose attestation input.

- [x] **Step 1: Rewrite the focused lifecycle expectation before implementation**

Replace attestation toggling with two explicit cases:

```python
def test_aio_run_mount_readiness_fails_closed_for_distinct_docker_host_view(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = Paths(tmp_path / "worker-state")
    backend = object.__new__(LocalContainerBackend)
    backend._runtime = "docker"
    backend.run_readonly_mounts_ready = lambda: True
    provider = object.__new__(AioSandboxProvider)
    provider._backend = backend
    monkeypatch.setattr(paths, "run_skill_uses_distinct_host_view", lambda: True)
    assert provider.run_readonly_mounts_ready() is False


def test_aio_run_mount_readiness_allows_shared_docker_host_view(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = Paths(tmp_path / "worker-state")
    backend = object.__new__(LocalContainerBackend)
    backend._runtime = "docker"
    backend.run_readonly_mounts_ready = lambda: True
    provider = object.__new__(AioSandboxProvider)
    provider._backend = backend
    monkeypatch.setattr(paths, "run_skill_uses_distinct_host_view", lambda: False)
    assert provider.run_readonly_mounts_ready() is True
```

In both tests, retain the existing monkeypatches for module `get_paths`, module `get_app_config`, the `/mnt/skills` configured root, and the environment needed for `Paths.run_skill_host_mapping_ready()`.

Remove the config acceptance test for `compose_dood_p03_v1_verified`; keep the independent BoxLite and E2B strict-boolean tests.

- [x] **Step 2: Run focused tests and confirm the new contract fails**

Run: `cd backend && uv run pytest tests/test_app_config_access_paths.py tests/test_aio_private_sandbox_lifecycle.py -q`

Expected: FAIL until provider/config behavior is changed.

- [x] **Step 3: Remove the field, marker, probe, and attestation branch**

Delete the Pydantic field and example YAML block. Remove the `p03_compose_dood` pytest marker and both Compose-only test files. Implement the readiness rule as:

```python
path_view_is_compatible = (
    self._backend.runtime != "docker"
    or not paths.run_skill_uses_distinct_host_view()
)
return path_view_is_compatible and self._backend.run_readonly_mounts_ready()
```

Retain the existing error handling around config/path/backend access.

- [x] **Step 4: Run focused tests and commit**

Run: `cd backend && uv run pytest tests/test_app_config_access_paths.py tests/test_aio_private_sandbox_lifecycle.py -q`

Expected: PASS.

Commit: `refactor(sandbox): remove compose dood attestation`

### Task 3: Clean active operational guidance and Provisioner ownership

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `Install.md`
- Modify: `backend/AGENTS.md`
- Modify: `backend/CONTRIBUTING.md`
- Modify: `sandbox/provisioner/README.md`
- Modify: `sandbox/provisioner/app.py`
- Modify: `scripts/check.py`
- Modify: `scripts/doctor.py`
- Modify: `scripts/support_bundle.py`
- Modify: `scripts/detect_uv_extras.py`
- Modify: `backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py`
- Modify: `backend/tests/knowledge/test_knowledge_tokenizer.py`
- Modify: `frontend/src/content/en/application/deployment-guide.mdx`
- Modify: `frontend/src/content/en/harness/sandbox.mdx`
- Modify: `frontend/src/content/zh/application/deployment-guide.mdx`
- Modify: `frontend/src/content/zh/harness/sandbox.mdx`
- Modify: `frontend/src/content/zh/introduction/harness-vs-app.mdx`

**Interfaces:**
- Consumes: host-process application topology and optional Docker/Kubernetes Sandbox topology.
- Produces: one consistent active operating contract with no application Docker/Compose instructions.

- [ ] **Step 1: Rewrite root and operator documentation**

Document local startup only. Replace the Docker/Compose section with a Sandbox-only Docker section that keeps `make setup-sandbox`. Update ownership to `nginx/` and `sandbox/`, and remove deployment examples and the P-03 attestation procedure.

- [ ] **Step 2: Rewrite Provisioner documentation and comments**

Describe `sandbox/provisioner/` as a standalone optional service. Provide direct image build/run or external cluster deployment commands, remove `make docker-start`, Compose network claims, and `docker-compose-dev.yaml` references, while retaining Kubernetes RBAC, path, image, and API-key warnings.

- [ ] **Step 3: Clean scripts, active guides, and comments**

Remove local application Docker mode suggestions from `check.py`, `doctor.py`, and support-bundle setup wording. Keep Docker version detection because it diagnoses the optional Sandbox runtime. Remove Docker-build wording from UV extra and tokenizer comments. Rewrite active frontend deployment and Sandbox guides to the same boundary. Preserve historical backup plans/specs.

- [ ] **Step 4: Run scoped stale-reference checks**

Run from the repository root:

```bash
rg -n -i 'docker compose|docker-compose|compose_dood|p03_compose|make docker|make up|make down|scripts/deploy\.sh|scripts/docker\.sh|docker/nginx|docker/provisioner' \
  README.md Install.md AGENTS.md Makefile scripts backend frontend/src/content config.example.yaml sandbox \
  --glob '!scripts/setup-sandbox.sh' --glob '!scripts/cleanup-containers.sh'
```

Expected: no application deployment or old-path references. Legitimate generic terms such as Python object composition and Docker Sandbox commands must be assessed by context rather than removed mechanically.

- [ ] **Step 5: Commit**

Commit: `docs: document local application runtime`

### Task 4: Verify the complete cleanup and integrate it

**Files:**
- Modify only files required to fix failures caused by Tasks 1–3.

**Interfaces:**
- Consumes: completed repository layout, Sandbox safety contract, and active docs.
- Produces: verified branch merged into `main` without touching unrelated user files.

- [ ] **Step 1: Format changed backend Python files**

Run: `cd backend && make format`

Expected: formatter completes successfully; review its diff and retain only relevant formatting.

- [ ] **Step 2: Run focused backend tests**

Run:

```bash
cd backend && uv run pytest \
  tests/test_application_runtime_layout.py \
  tests/test_app_config_access_paths.py \
  tests/test_aio_private_sandbox_lifecycle.py \
  tests/knowledge/test_knowledge_tokenizer.py -q
```

Expected: PASS.

- [ ] **Step 3: Validate shell/config/document references**

Run `bash -n scripts/serve.sh scripts/nginx.sh scripts/setup-sandbox.sh scripts/cleanup-containers.sh`, `git diff --check`, the Task 3 scoped `rg`, and `test ! -e docker`.

Expected: all syntax and whitespace checks pass; the stale-reference scan returns no application deployment references.

- [ ] **Step 4: Review the final diff and commit fixes**

Run: `git status --short`, `git diff --stat`, and `git diff --check`.

Expected: only approved cleanup files appear. Commit any verification fixes as `fix: complete application docker cleanup`.

- [ ] **Step 5: Merge into main and protect unrelated changes**

Confirm the main worktree still contains only the four pre-existing M11 modifications, merge `codex/remove-app-docker-deployment` into `main` without staging them, then verify `git status --short` and `git log -1 --oneline` in both worktrees.

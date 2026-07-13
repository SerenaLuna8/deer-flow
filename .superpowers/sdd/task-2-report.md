# M3 Task 2 Report: 共享资产授权与 domain contract

## STATUS

PASS — 已按 brief 建立共享资产不可变 domain contract、平台治理 context、项目 capability、稳定错误与最小治理事件 sink；未修改 Task 1 schema，未实现 repository、service、router 或 crypto。

## RED

首次运行 brief 指定命令时，`uv` 默认 cache 位于 sandbox 不可写目录，命令未进入 pytest：

```text
$ cd backend && uv run pytest tests/test_shared_asset_contexts.py tests/test_project_capabilities.py -q
error: Failed to initialize cache at `/Users/jiangfeng/.cache/uv`
Caused by: ... Operation not permitted
```

该环境错误不计作 RED。将 cache 切换到可写目录后，聚焦测试因缺少目标 contract 按预期失败：

```text
$ cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run pytest tests/test_shared_asset_contexts.py tests/test_project_capabilities.py -q
E   ModuleNotFoundError: No module named 'app.shared_assets'
1 error in 2.40s
```

失败原因是 Task 2 模块尚不存在，不是测试拼写或环境问题。

## GREEN

实现最小 contract 后运行相同聚焦测试：

```text
$ cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run pytest tests/test_shared_asset_contexts.py tests/test_project_capabilities.py -q
.............                                                            [100%]
13 passed in 0.25s
```

## Ruff 与基础校验

```text
$ cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run ruff check app/shared_assets app/projects/capabilities.py tests/test_shared_asset_contexts.py tests/test_project_capabilities.py
All checks passed!

$ cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run ruff format --check app/shared_assets app/projects/capabilities.py tests/test_shared_asset_contexts.py tests/test_project_capabilities.py
8 files already formatted

$ git diff --check
(no output, exit 0)
```

## Changed files

- `backend/app/shared_assets/__init__.py`
- `backend/app/shared_assets/models.py`
- `backend/app/shared_assets/errors.py`
- `backend/app/shared_assets/contexts.py`
- `backend/app/shared_assets/governance_events.py`
- `backend/app/projects/capabilities.py`
- `backend/tests/test_shared_asset_contexts.py`
- `backend/tests/test_project_capabilities.py`
- `backend/AGENTS.md`
- `.superpowers/sdd/task-2-report.md`

## Self-review

- `AssetScope`、`AssetKind`、`WorkflowStatus` 与 Task 1 schema 的字符串值一致；brief 指定的 dataclass 字段、顺序、默认值和冻结语义均已锁定。
- `resolve_asset_actor()` 只允许认证域 UUID `system_admin` 创建独立 `SystemAssetGovernanceContext`；普通用户无法借此伪造 `ProjectContext`。
- `shared_assets.manage_bindings` 和 `mcp.credentials.approve` 仅 Admin 持有；Editor 仍保留 `shared_assets.edit`，原 Runner/Viewer 能力未扩大。
- 五类错误固定映射 404/403/409/422/503，实例只保存 `request_id`；code、status 与 public message 是不含内部细节的类级公共 contract。
- 默认治理 sink 的 `write_override()` 只接收 actor/project/asset/version/action/request_id，structured event 精确包含这六个键；没有 payload、diff、credential metadata 或私有资源 ID 参数。
- `app.shared_assets` 只依赖 application 层的轻量类型，不反向破坏 harness → app import boundary，也未触碰 M3 ORM/migration。
- 文档已在 `backend/AGENTS.md` 的 M3 段用中文同步授权、错误和治理日志边界。

## Concerns

- 按任务要求只运行 brief 指定 focused tests 与 changed-file Ruff，未运行全量 backend suite。
- 当前 sink 是结构化日志默认实现；持久化 audit sink 明确留给 M6。

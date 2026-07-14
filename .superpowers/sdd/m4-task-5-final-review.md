# M4 Task 5 修复最终独立复审

## 范围与结论

复审范围严格限定为原实现 `90c3c58a` 到修复 HEAD `6d0e42a` 的差异，以及该
差异与 Task 5 frozen invariants 的集成。批准基线仍为 `b2c919cf`。未修改生产
代码，未提交。

**结论：APPROVED — 0 Critical，0 Important，1 Minor。**

原独立审查中的 1 Critical + 3 Important 均已关闭。剩余 Minor 是 source-of-
truth 文档中的一句旧描述，没有运行时或安全影响，不阻止 Task 5 修复批准。

## 原 finding 关闭验证

### C1 已关闭：`local-run:` 进入统一 Local 安全分支

- `is_local_sandbox()` 现在统一识别 `local`、`local:` 与 `local-run:`
  （`backend/packages/harness/deerflow/sandbox/tools.py:1255-1272`）。因此原
  九个公共调用点都会进入 Local confinement、路径解析、host-bash guard 与路径
  masking 分支。
- 中央分类器测试新增 `local-run:owner:alpha:run-1`，避免只修单个工具调用点。
- `test_sandbox_tools_security.py` 通过真实公共 tool function 验证 run-scoped Local
  的 read/write 虚拟路径解析、错误 host path masking，以及 host bash disabled 时
  不触达 sandbox executor。
- Local 风险全集同时覆盖 `ls`、glob/grep 的共用 Local read resolver、write/
  str_replace 的写路径 gate、provider mount 与 middleware lifecycle。静态追踪确认
  这些调用点仍共享中央分类器；没有残留的 `local-run:` 特判分叉。

### I1 已关闭：exact private Agent 排除全局 Skill/ACP 可选工具

- `get_available_tools()` 新增 keyword-only 的 `include_skill_manage` 与
  `include_acp`，默认均为 `True`，所以 legacy/system 调用语义不变
  （`backend/packages/harness/deerflow/tools/tools.py:44-68`）。
- private exact-agent path 明确传入 `include_mcp=False`、
  `include_skill_manage=False`、`include_acp=False`
  （`backend/packages/harness/deerflow/agents/lead_agent/agent.py:612-623`）。
- exact-agent 测试同时启用全局 Skill evolution 与 ACP，最终 tool set 只保留 exact
  Agent group tool 和 exact MCP tool；`skill_manage` / `invoke_acp_agent` 均缺席。
- legacy ACP、Skill-management 与 tool-deduplication 回归 **31 passed**，证明新增
  开关的默认值没有改变 legacy 行为。

### I2 已关闭：exact Skill read/list 不触碰全局 Skill storage/cache

- `_is_trusted_run_scoped_skill_path()` 只接受 typed
  `RunScopedReadOnlyMount`，并要求 mount `run_id` 与 server runtime `run_id` 相等、
  请求路径处于该 mount container root 内
  （`backend/packages/harness/deerflow/sandbox/tools.py:247-263`）。普通 dict 伪造和
  wrong-run mount 均不可信。
- `read_file` 与 `ls` 仅在该 typed/matching-run 条件成立时绕过 global Skill
  enablement/storage；Local exact read 保持虚拟路径交给当前 run-scoped sandbox
  mapping 解析，而不是调用全局 `_resolve_skills_path()`。
- 集成测试使用真实 `LocalSandboxProvider` 与 run mount，并把
  `get_or_new_user_skill_storage()` 替换为一旦调用即失败的 sentinel；exact
  `read_file` 和 `ls` 均成功，证明没有全局 storage/cache 访问，读取内容来自正确
  run temp tree。
- worker 只从 admitted private runtime 构造 typed mount，并覆盖运行时
  `thread_id`/`run_id`/owner；client-shaped dict 无法生成 trusted mount。

### I3 已关闭：cleanup 有界、失败可重试且路径不泄露

- `_remove_private_skill_tree()` 最多执行 3 次，`FileNotFoundError` 视为成功，
  persistent `OSError` 转换为 generic `PrivateRuntimeCleanupError`，不携带 host
  path（`backend/app/private_work/asset_runtime.py:52-70`）。
- `PrivateAgentRuntime.aclose()` 只在删除成功后设置 `_closed=True`；persistent
  failure 保持 `_closed=False`，后续调用可重试。
- transient-failure 测试证明第二次删除成功后才关闭；persistent-failure 测试证明
  多次有界尝试、未关闭以及异常文本无 temp path。
- materialization cleanup persistent failure 保留原
  `PrivateWorkAssetStale("Private work asset is stale.")`，日志只包含 generic cleanup
  信息；worker cleanup 也去掉 `exc_info`，不会把异常中的 host path 写入日志。
- `mkdtemp()` 失败映射为稳定 `PrivateWorkUnavailable(request_id)`，不泄露路径。

## Minor finding

### M1. `backend/AGENTS.md` 的 Detection 句仍遗漏 `local-run:`

同一文件第 375 行已正确声明所有 Local variants（包括 `local-run:`）共享中央
classifier，但第 388 行仍写成 `is_local_sandbox()` 只接受 `local` 与 `local:`。
这是 source-of-truth 文档内部矛盾。建议把第 388 行同步为三种 ID；运行时代码、
测试和安全边界本身均已正确。

## 独立验证证据

### PostgreSQL 安全边界

- 独立 cluster：`/tmp/deerflow-m4-task5-final-review-pg.XPX0qr/data`
- 监听：`127.0.0.1:55493`
- 所有 PostgreSQL pytest 均显式使用
  `POSTGRES_TEST_URL=postgresql://postgres@127.0.0.1:55493/postgres`。
- fixtures 只创建随机 `deerflow_test_*` 数据库；结束前只读查询确认没有残留
  `deerflow_test_*`。
- 未连接业务库；全部测试结束后已执行 `pg_ctl ... stop -m fast`，server 正常停止。

### Task 5 四套

```text
env POSTGRES_TEST_URL=postgresql://postgres@127.0.0.1:55493/postgres \
  uv run pytest \
  tests/test_private_run_admission.py \
  tests/test_private_asset_runtime.py \
  tests/test_private_runtime_context.py \
  tests/test_legacy_system_asset_runtime.py -q
```

结果：**98 passed，0 failed，0 skipped，5.81s**。

### Mandatory 六套

在上述四套基础上加入 `test_runtime_lifecycle_e2e.py` 与
`test_runtime_channel_config_merge.py`。

结果：**102 passed，6 failed，0 skipped，1 warning，9.12s**。

六个 failure 与修复前相同，全部在 legacy `POST /api/threads` 被 staged
`409 PRIVATE_WORK_CUTOVER` 拦截，是已声明的 Task 11 router-cutover cases；没有
进入 Task 5 admission、materialization、model、MCP、graph 或 worker。warning 是
既有 Starlette `httpx` deprecation。

### Local 风险套件

```text
uv run pytest \
  tests/test_local_sandbox_virtual_path_contract.py \
  tests/test_local_sandbox_provider_mounts.py \
  tests/test_sandbox_middleware.py \
  tests/test_sandbox_provider_lifecycle.py \
  tests/test_sandbox_tools_security.py \
  tests/test_user_scoped_skill_storage.py -q
```

结果：**243 passed，0 failed，0 skipped，2.14s**。

### 受影响 runtime 回归

RunManager、worker rollback/Langfuse/subagent persistence、lead-agent
model/prompt/skills、runtime channel、sandbox middleware/lifecycle、Local mount/
classifier 的组合命令显式带同一 disposable PostgreSQL URL。

结果：**277 passed，0 failed，0 skipped，3.19s**。

Legacy optional-tool 专项：

```text
uv run pytest \
  tests/test_invoke_acp_agent_tool.py \
  tests/test_skill_manage_tool.py \
  tests/test_tool_deduplication.py -q
```

结果：**31 passed，0 failed，0 skipped，0.47s**。

### 质量门禁

- `uv run ruff check <10 repair Python files>`：`All checks passed!`
- `uv run ruff format --check <10 repair Python files>`：
  `10 files already formatted`
- `uv run python -m compileall -q <5 repaired production modules>`：exit 0，无输出
- `git diff --check 90c3c58a..6d0e42a`：exit 0，无输出
- PostgreSQL tests、Local risk 与 affected regression 均 **0 skipped**；没有把 skip
  当作通过。

## 工作树状态

写本报告前 `git status --short` 无输出，HEAD 为
`6d0e42a74af95ffe2f1271e6ab4e0538df1a1a1e`。除本报告外未产生任何文件修改；
没有修改生产代码，没有 commit。

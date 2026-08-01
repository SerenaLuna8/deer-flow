# 13. Sandbox 模块：`main` 具体实现与 `dev` 对照

## 1. 分析边界与结论

本文分析以下代码，不把 Sandbox 与 Agent、Tool、MCP 混写：

- `main@e317f7b8` 的 Sandbox 抽象、Provider 生命周期、中间件、文件与命令工具；
- Local、AIO、E2B、BoxLite 四类实现；
- AIO/E2B 的容量、暖池、跨进程所有权与孤儿回收；
- Provisioner 的 Kubernetes 创建/销毁接口；
- `dev@8a91e957` 的项目/所有者/Run 私有权限边界；
- 哪些 `main` 修复可以移植，哪些必须按 `dev` 的 Worker-only 架构重写。

结论先行：

1. `main` 在“容器生命周期工程”上明显更成熟：Provider 单例竞态、异步阻塞隔离、暖池容量、所有权租约、跨实例回收、E2B 对账都有成体系实现。
2. `dev` 在“私有资源授权”上建立了另一条更严格的主线：Sandbox 不是只按 `user_id + thread_id` 复用，而是绑定 `project_id + owner_user_id + thread_id + run_id`，并在 Worker 的 Job/Run lease 下执行。
3. 因此不能把 `main` 的 AIO/E2B Provider 整体覆盖到 `dev`。可直接吸收的是局部正确性修复；容量、暖池、所有权和 reconcile 必须改写成 Project/Owner/Run/Job 语义。
4. `main` 的所有权租约解决的是“哪个 Gateway 进程管理哪个 Sandbox”，不是 `dev` 的业务授权，也不能替代 Worker lease。

## 2. `main` 源码地图

| 层次 | `main` 文件 | 主要职责 |
| --- | --- | --- |
| 抽象 | `backend/packages/harness/deerflow/sandbox/sandbox.py` | `Sandbox` 命令、文件、搜索接口与环境变量校验 |
| Provider 抽象 | `backend/packages/harness/deerflow/sandbox/sandbox_provider.py` | acquire/get/release、异步适配、进程内单例生命周期 |
| 图中间件 | `backend/packages/harness/deerflow/sandbox/middleware.py` | Agent 运行前取得 Sandbox、运行后释放、把惰性分配结果写回状态 |
| 工具 | `backend/packages/harness/deerflow/sandbox/tools.py` | bash、read/write/edit、ls、glob、grep 等模型工具 |
| 路径安全 | `backend/packages/harness/deerflow/sandbox/path_patterns.py` | 虚拟路径解析、正则匹配、Windows/Posix 兼容 |
| 输出覆盖 | `backend/packages/harness/deerflow/sandbox/overwrite.py` | ToolMessage 内容替换与外部化后的状态修正 |
| 本地实现 | `backend/packages/harness/deerflow/sandbox/local/` | Host/本地文件系统实现和命令超时 |
| AIO 实现 | `backend/packages/harness/deerflow/community/aio_sandbox/` | 本地 Docker 或远程 Provisioner 后端、暖池与所有权 |
| E2B 实现 | `backend/packages/harness/deerflow/community/e2b_sandbox/` | E2B VM、容量策略、远端发现与对账 |
| BoxLite 实现 | `backend/packages/harness/deerflow/community/boxlite/` | BoxLite VM 与暖复用 |
| 所有权 | `backend/packages/harness/deerflow/community/aio_sandbox/ownership/` | memory/Redis lease |
| Provisioner | `docker/provisioner/app.py` | Kubernetes Pod/Service、挂载验证、鉴权、CRUD API |

## 3. 抽象接口和调用链

### 3.1 `Sandbox`

`main` 的 `Sandbox` 是执行能力接口，核心方法包括：

- `execute_command(command, timeout, env)`；
- `read_file(path)`、`write_file(path, content, append)`、`update_file(path, bytes)`；
- `download_file(path)`；
- `list_dir(path, max_depth)`；
- `glob(path, pattern, include_dirs, max_results)`；
- `grep(path, pattern, include, max_results)`。

环境变量名先走 POSIX 名称检查；Provider 注入的环境变量和模型提供的临时环境变量最终在实现层合并。这个接口本身只表达“能执行什么”，没有表达 Project、Owner、Run 或 Job lease。

### 3.2 `SandboxProvider`

`main` 的基础协议是：

```text
acquire(thread_id, user_id) -> sandbox_id
acquire_async(...)          -> asyncio.to_thread(acquire)
get(sandbox_id)             -> Sandbox | None
release(sandbox_id)         -> None
reset()                     -> 可选清理
```

另外有两个 Provider 能力标志：

- `uses_thread_data_mounts`：线程数据是否已经通过共享挂载可见；
- `needs_upload_permission_adjustment`：上传后是否需要修正权限。

典型调用链为：

```text
Agent graph
  -> SandboxMiddleware.before_agent / abefore_agent
  -> get_sandbox_provider()
  -> provider.acquire(thread_id, user_id)
  -> provider.get(sandbox_id)
  -> bash/read/write 等工具
  -> SandboxMiddleware.after_agent / aafter_agent
  -> provider.release(sandbox_id)
```

### 3.3 Provider 单例的并发修复

`main:backend/packages/harness/deerflow/sandbox/sandbox_provider.py` 的
`_provider_lock` 不只是简单的双检锁：

1. 快路径在锁内读取 `_default_sandbox_provider`，避免 reset/shutdown 同时把引用置空。
2. 动态类解析和 Provider 构造在锁外完成，因为插件构造器可能慢、可能重入。
3. 两个线程同时构造时，在锁内只安装一个 winner。
4. loser 在锁外调用 `shutdown()`，避免构造器启动的后台线程泄漏。
5. `reset_sandbox_provider()` 和 `shutdown_sandbox_provider()` 先在锁内摘除全局引用，再在锁外执行插件回调，避免非重入死锁。

这是可独立移植到 `dev` 的并发正确性方案；它不改变业务授权模型。

### 3.4 `SandboxMiddleware`

`main` 的中间件同时支持 eager 和 lazy acquire：

- eager：进入 Agent 前取得 Sandbox，写入 graph state；
- lazy：首次工具调用时取得 Sandbox；
- 工具包装器比较调用前后的状态；
- 如果工具期间才创建 Sandbox，则返回 `Command(update={"sandbox": ...})`，保证后续节点看到同一个 ID；
- Agent 结束后释放；
- fork/恢复状态时先 `unwrap_sandbox`，避免把父运行恢复进来的 Sandbox 当成本次新资源释放。

这里的关键语义是“图状态中的 Sandbox 引用”，不是持久化的权限凭据。`dev` 若吸收中间件修复，必须保证状态恢复后仍重新校验 Run/Job authority。

## 4. Local Sandbox

### 4.1 命令执行

`LocalSandbox` 负责：

- 在允许 Host bash 时启动子进程；
- 为命令设置墙钟超时；
- 超时时终止进程组，而不只杀父进程；
- 截断 bash/read/ls 输出；
- 统一文本编码和二进制文件处理；
- 将虚拟路径解析到允许的本地根。

`main` 的 `bash_command_timeout` 默认 600 秒。前台启动服务器若没有放到后台，会在超时后结束；这解释了为什么开发命令必须由根编排器管理，而不应通过 Agent bash 长期占用。

### 4.2 路径与文本替换修复

`main` 累积了多项可单独回移的修复：

- Windows 路径 containment 使用规范化路径组件判断；
- 路径正则缓存和反向路径段解析；
- `read_file` 的单边行区间；
- `str_replace` 拒绝空 old string，防止每个字符间插入内容；
- grep 对普通文件路径生效，而不只目录；
- 输出遮罩按边界处理，避免截断标记与真实内容混淆；
- cwd/虚拟工作目录保持一致；
- Overwrite 中间件能解包被其他中间件封装过的消息。

这些修复大多不依赖 `main` 的 Gateway 执行架构，可按测试逐项移植。

## 5. AIO Sandbox：两种后端，一个生命周期控制器

### 5.1 组成

`AioSandboxProvider` 不是简单 Docker wrapper：

```text
AioSandboxProvider
  -> SandboxBackend
     -> LocalContainerBackend       # Docker/Podman，本机 daemon
     -> RemoteSandboxBackend        # 调 Provisioner HTTP API
  -> AioSandbox                     # 调 sandbox 容器内的 AIO HTTP API
  -> SandboxOwnershipStore          # memory 或 Redis
```

`SandboxInfo` 保存容器/远端实例标识、URL、时间和关联信息。Provider 内部维护：

- `(user_id, thread_id) -> sandbox_id`；
- active Sandbox；
- warm pool；
- 每个 `(user, thread)` 的互斥锁；
- 正在 teardown、acquire、remote operation 的过渡集合；
- 所有权 lease；
- idle cleanup 与 lease renewal 后台线程。

### 5.2 标识和挂载

`_effective_acquire_user_id` 取得有效用户；`_thread_key` 形成二元键；`_deterministic_sandbox_id` 用用户和线程导出稳定容器 ID。挂载来源由以下函数分层汇总：

- `_get_extra_mounts`；
- `_get_thread_mounts`；
- `_get_skills_mounts`；
- `_get_user_skill_mounts`；
- `_get_lark_cli_runtime_mounts`；
- `_dedupe_mounts_by_container_path`。

`main` 的边界仍是用户/线程。它没有 `project_id`、`owner_user_id`、`run_id`，也不知道当前 Worker 是否仍持有 Job lease。

### 5.3 acquire 状态机

`acquire()` 的实质流程是：

1. 为 `(user, thread)` 取得局部锁；
2. 检查进程内已登记 Sandbox；
3. 检查活性并处理不健康项；
4. 尝试从暖池 reclaim；
5. 尝试从后端 discover 稳定 ID；
6. 没有可复用项时创建；
7. 创建时注入挂载和环境；
8. 对远端实例做 readiness/bootstrap；
9. 发布 ownership；
10. 最后才提交到 active map 并返回。

同步和异步路径分别实现，避免 FastAPI/LangGraph 事件循环被 Docker/HTTP 阻塞。`backend/tests/blocking_io/test_aio_sandbox_get.py` 与 `test_sandbox_release.py` 专门验证事件循环边界。

### 5.4 release、暖池与销毁

`release()` 并不一定销毁：

- 正常释放可把实例移入 warm pool；
- 超过容量时淘汰最旧暖项；
- idle reaper 清理过期项；
- teardown 前取得 destroy ownership；
- 远端状态不确定时保留 tombstone/transition 状态，避免错误释放容量槽；
- `shutdown()` 停后台线程、等待远端操作并处理自己拥有的资源。

如果把这套逻辑用于 `dev` 私有 Run，必须改变一条原则：私有 Run lease 释放时应“精确销毁”，不能进入跨 Run 暖池。`dev` 已在 `release_private()` 明确这一点。

## 6. 跨进程 Sandbox 所有权

### 6.1 接口

`SandboxOwnershipStore` 的核心操作是：

- `take(sandbox_id)`：首次占有；
- `claim(sandbox_id, for_destroy=False)`：接管过期或可接管项；
- `renew(sandbox_id) -> RenewOutcome`；
- `release(sandbox_id)`；
- `owner(sandbox_id)`。

`RenewOutcome` 区分续租成功、所有权已丢失和后端异常，使 Provider 能选择继续、忘记本地对象或推迟破坏性回收。

### 6.2 Memory 与 Redis

- `MemoryOwnershipStore` 只在进程内共享，适合单实例；
- `RedisOwnershipStore` 用带 TTL 的 key 和原子脚本保护 claim/renew/release；
- owner id 唯一标识一个 Gateway/Provider 实例；
- lease TTL 从 renewal interval 与 multiplier 推导，而不是复用 sandbox idle timeout；
- live owner 即使 Sandbox 空闲，也靠 renewal 保持所有权。

`SandboxOwnershipConfig` 默认 `type: memory`。多 Gateway/多 worker 且共享容器后端时，`main` 会警告必须用 Redis，否则一个实例可能把另一个实例正在使用的容器当孤儿接管并销毁。

### 6.3 与 `dev` Job lease 的区别

| 机制 | 保护对象 | 身份维度 | 能否授权业务副作用 |
| --- | --- | --- | --- |
| `main` Sandbox ownership | 容器生命周期 | owner process + sandbox id | 不能 |
| `dev` Job/Run lease | 一次 Run 的执行权 | project + owner + run + job + lease | 能 |
| `dev` capability revalidation | 每个私有副作用 | account + membership + capability | 能 |

正确的移植方式是让容器 ownership 成为 Worker lease 之下的资源协调层，而不是拿 Redis ownership 代替授权。

## 7. E2B：容量控制与远端对账

### 7.1 容量字段

`E2BSandboxProvider` 维护：

- `_sandboxes`：active；
- `_warm_pool`：LRU 暖 VM；
- `_thread_sandboxes`：用户/线程映射；
- `_reserved_slots`：已预留、尚未完成创建；
- `_transitioning_slots`：销毁/重连等过渡槽；
- `_remote_ops_in_progress`；
- `_owned_sandbox_ids`；
- `_acquire_inflight`；
- `_orphan_first_seen`；
- `_capacity_cond`。

容量计算必须把 active、warm、reserved 和 transitioning 一起计入，否则并发创建会越过副本上限。

### 7.2 overflow 策略

配置包含：

- `replicas`：基础容量；
- `overflow_policy`: `wait | reject | burst`；
- `acquire_timeout`；
- `burst_limit`。

行为：

- `wait`：在 condition 上等释放，超时失败；
- `reject`：立即抛 `SandboxCapacityExceededError`；
- `burst`：允许到 `replicas + burst_limit`；
- burst_limit 为 0 时退化为 reject，并记录警告。

创建流程先原子预留 slot，再做 E2B 远端调用；失败时释放 reservation 并通知 waiter。Sandbox metadata 写入 provider、Gateway owner、user、thread、created_at，供跨进程发现。

### 7.3 reconcile

后台 reconcile 有明确预算：

- interval；
- grace period；
- orphan TTL；
- max pages；
- max items；
- max seconds。

它把远端项按 `(user_id, thread_id)` 分组，选择 canonical 实例，尝试：

1. 识别本地已跟踪项；
2. 检查 ownership；
3. 在容量允许时 adopt；
4. 延迟处理处在 grace 内的新实例；
5. 对 duplicate/orphan 先取得 destroy claim；
6. 超过 TTL 且仍无人拥有才 kill；
7. 达到页数、条数或时间预算即停止并记录 `ReconciliationStats.budget_exhausted`。

这比“启动时列举并删除未知 VM”安全得多。`backend/tests/test_sandbox_orphan_reconciliation.py` 和 `test_sandbox_orphan_reconciliation_e2e.py` 覆盖了并发、孤儿、重复项与预算。

### 7.4 输出同步

E2B 没有共享 Host 文件系统，所以 `uses_thread_data_mounts=False`，上传和产物需要显式同步。`main` 的修复不只比较文件大小，还能识别“大小相同但内容改变”，并限制同步范围，避免把整个远端目录无界复制回 Host。

在 `dev` 中，同步目标不能再是用户/线程 Host 目录；必须写入 `PrivateSandboxFileProjection`/数据库私有文件存储，并在每页读取、每次写入前复核 capability。

## 8. BoxLite

BoxLite Provider 与 AIO/E2B 一样支持容量与暖复用，但 VM 生命周期由 BoxLite 管理。`main` 的关键改动是：

- tenant/user/thread 身份进入稳定 hash，降低跨租户碰撞；
- 暖 VM reclaim 可配置 health-check skip window；
- 默认 `0` 表示总是重新验证；
- Provider 仍以用户/线程为资源边界。

身份 hash 的实现思路可用，但 `dev` 必须把 project 和 owner 纳入输入，并为每个私有 Run 重新决定是否允许暖复用。

## 9. Provisioner 具体边界

`docker/provisioner/app.py` 提供：

- `POST /api/sandboxes` 创建；
- `DELETE /api/sandboxes/{sandbox_id}` 销毁；
- `GET /api/sandboxes/{sandbox_id}` 查询；
- `GET /api/sandboxes` 列举；
- health/capabilities。

创建请求经过：

```text
verify_api_key
  -> CreateSandboxRequest 校验
  -> _validated_extra_mounts
  -> _build_volumes / _build_volume_mounts
  -> _build_pod
  -> _build_service
  -> 等待 Pod/Service
  -> 返回 SandboxResponse
```

安全要点：

- `/api/*` 通过 `X-API-Key` 与 `PROVISIONER_API_KEY` 比较；
- key 未配置或不匹配时拒绝请求；
- extra mount 必须落在允许 Host 根下；
- container path 必须规范化；
- Kubernetes volume/subPath 分开构造；
- Service 类型由部署配置决定，`main` 的 Helm 默认已收紧为 `ClusterIP`。

`dev@8a91e957` 的历史基线没有纳入当前这套 `docker/` 实现；本轮开始前用户已恢复
并重新纳管该目录。因此本轮可以修改和验证 Docker/Nginx/Provisioner 文件，但不能把
当前工作树倒推成旧基线本来就具备这些边界。

## 10. `dev` 的私有 Sandbox 权限模型

### 10.1 新数据类型

`dev:backend/packages/harness/deerflow/sandbox/sandbox_provider.py` 新增：

- `RunScopedReadOnlyMount(run_id, container_path, host_path)`；
- `PrivateSandboxLease(sandbox_id, run_id, relative_root)`；
- `private_sandbox_relative_root(scope, thread_id)`。

私有根固定为：

```text
projects/{project_id}/users/{owner_user_id}/threads/{thread_id}
```

构造函数拒绝非绝对 Host path、非规范 container path、`..`、非法 thread/run 标识。

### 10.2 `acquire_private`

`acquire_private()` 默认 fail closed：

1. Provider 必须显式声明 `_supports_isolated_private_file_authority`；
2. `scope` 必须是精确的 `PrivateResourceScope`，不接受鸭子类型；
3. `user_id == scope.owner_user_id`；
4. 每个 mount 的 `run_id` 必须与本次 Run 相同；
5. `_acquire_private_fresh()` 必须返回全新 Sandbox；
6. `get()` 后实际调用 `list_secure_files()` 探测安全文件原语；
7. 同一 Sandbox 或同一 Run 不能已有 lease；
8. 任一步失败都销毁已创建实例。

`release_private()` 验证 lease 与注册值完全相等，并精确销毁，不进入 warm pool。

### 10.3 取消安全

`acquire_private_async()`/`release_private_async()` 使用 `_await_joined_thread()`：

- 调用者被取消时，不立即遗弃正在执行的阻塞线程；
- 等线程结束；
- 如果 acquire 已成功，则先释放刚取得的私有 Sandbox；
- 清理完成后再重新抛 `CancelledError`。

这防止“HTTP/Worker task 取消，但 Docker 创建线程稍后成功，从而泄漏一个无人登记的私有容器”。

### 10.4 文件 authority

`dev:backend/app/private_work/sandbox_files.py` 的 `PrivateSandboxFileProjection` 负责：

- 将 Project/Owner/Thread 映射到私有文件命名空间；
- 分块读取数据库文件；
- 每页读取和每次写入前重新验证 capability；
- 校验 chunk index、总大小和 hash；
- begin/append/publish 原子发布；
- 失败时清理未发布对象；
- 生成 `AuthorityManifest`。

`dev:backend/packages/harness/deerflow/sandbox/sandbox.py` 还要求
no-link-following 的 secure file info/reader/writer，并通过
`AuthorizationBoundary` 在 model、tool、MCP、sandbox
write/exec/restore 等边界复核权限。

## 11. `main` 与 `dev` 的精确差异

| 项目 | `main@e317f7b8` | `dev@8a91e957` | 判断 |
| --- | --- | --- | --- |
| 执行进程 | Gateway 内图执行 | Worker-only 图执行 | 生命周期代码不能原样搬 |
| 资源键 | user + thread | project + owner + thread + run | `main` 隔离维度不足 |
| 执行权 | 进程/容器 ownership | PostgreSQL Job/Run lease | 两者应分层组合 |
| 私有副作用 | 无统一 capability boundary | 每个副作用点重校验 | 必须保留 `dev` |
| 暖池 | AIO/E2B/BoxLite 完整 | 私有 Run 强制精确销毁 | 不能跨私有 Run 复用 |
| 跨实例 ownership | memory/Redis | 未采用 `main` 包 | 可重写后吸收 |
| E2B 容量/reconcile | 完整 | 相对简单 | 值得移植设计 |
| Provisioner | Docker/Helm 正式跟踪 | Git 树不含 docker | 需独立纳入版本管理决策 |
| 文件同步 | Host 用户/线程目录 | DB 私有文件投影 | 需重写数据通道 |

## 12. 可移植项分级

### A. 可以按小补丁移植

- Provider singleton 构造竞态与 loser shutdown；
- async 路径把阻塞 IO 放入线程；
- Windows containment、path regex cache、单边 read range；
- `str_replace` 空字符串保护；
- grep 普通文件；
- cwd、输出边界、Overwrite unwrap；
- 环境变量敏感名过滤；
- E2B 同大小内容变化检测；
- Provisioner API key 和 mount 规范化。

每项都应连同对应 `main` 测试移植，不能只复制实现。

### B. 可以借鉴算法，但必须按 `dev` 重写

- capacity reservation；
- wait/reject/burst；
- transition/tombstone 计数；
- Redis ownership；
- orphan reconciliation；
- warm pool；
-远端输出同步。

重写后的 key 至少包含：

```text
project_id + owner_user_id + thread_id + run_id
```

所有破坏性操作还要验证当前 Worker 的 Job/Run lease；所有文件写回要走私有 FileRepository 和 quota/audit。

### C. 不应移植

- 按 Gateway owner 作为最终执行权；
- 仅按 user/thread 发现和复用私有 Sandbox；
- 私有 Run 释放后进入跨 Run 暖池；
- 把远端文件直接同步到旧 Host thread 目录；
- 因 `main` 有 Redis ownership 就省略 capability revalidation。

## 13. 测试证据与建议验证

`main` 已有的重点测试：

- `backend/tests/test_sandbox_provider_lifecycle.py`
- `backend/tests/test_sandbox_middleware.py`
- `backend/tests/blocking_io/test_aio_sandbox_get.py`
- `backend/tests/blocking_io/test_sandbox_release.py`
- `backend/tests/test_aio_sandbox_provider.py`
- `backend/tests/test_e2b_sandbox_provider.py`
- `backend/tests/test_sandbox_ownership_store.py`
- `backend/tests/test_sandbox_orphan_reconciliation.py`
- `backend/tests/test_sandbox_orphan_reconciliation_e2e.py`
- `backend/tests/test_sandbox_windows_path_normalization.py`
- `backend/tests/test_sandbox_path_patterns.py`
- `backend/tests/test_sandbox_tools_security.py`
- `backend/tests/test_provisioner_mount_contract.py`
- `backend/tests/test_provisioner_pvc_volumes.py`

移植到 `dev` 后还必须新增或保留：

- Project A/Owner A 不能发现 Project B/Owner B 的 Sandbox；
- lease 丢失后禁止 exec/write/reconcile destroy；
- acquire 线程在取消后成功时仍能精确销毁；
- 私有 Run 不进入暖池；
- duplicate/orphan 删除前同时满足 ownership 与 Job lease 条件；
- 文件同步每个分页都重验 capability；
- mount 的 `run_id`、Project、Owner 任一不一致都 fail closed；
- Worker 重启后的孤儿对账不泄露 locator、Host path 或原始错误。

## 14. 相关 `main` 演进

以下提交代表 Sandbox 模块的主要实现演进，阅读顺序比只看最终 diff 更容易理解设计：

- `04a85b30`：过滤密码类环境变量；
- `08fd218b`：Windows 路径 containment；
- `d2ab5bb8`、`ae510cb2`：`str_replace` 空值保护；
- `6e6c0785`、`8eb3be59`：Overwrite 解包；
- `8cc4b3ab`：Provisioner API key；
- `90d511f3` 至 `b22f85c6`：E2B 生命周期、容量、所有权与 reconcile；
- `5d073991`：BoxLite/AIO tenant 身份；
- `2e5c8da2`：本地 AIO 代理绕过；
- `d455a181`：grep 普通文件；
- `9c7cd4ca`：thread data mount。

提交号只能解释演进；是否可用仍以 `main@e317f7b8` 的最终源码和测试为准。

## 15. 本轮移植计划与执行顺序

本轮没有整体覆盖 Provider，而是严格按可移植落点执行：

1. 先固定 `dev` 的 Project/Owner/Run/Worker authority，不允许 `main` 的
   user/thread 或 Gateway owner 语义覆盖它；
2. 先写红测，再移植不改变授权模型的路径、文本、搜索和中间件修复；
3. 再处理 Provider 生命周期、取消安全和 blocking-I/O；
4. 然后处理 AIO/E2B/BoxLite 的局部正确性，不移植私有暖池和旧 Host 同步；
5. 最后收紧 Provisioner/Nginx 边界；
6. 聚焦测试、完整后端、真实 PostgreSQL 门禁全部通过后，才进行三轮真实浏览器模型调用；
7. 将不能安全落入 Worker-only 边界的 Helm、ownership、reconcile 和同步 manifest
   单独列为 deferred，不混入“已完成”。

## 16. 实际落点

### 16.1 已完成或确认已存在

| 落点 | 状态 | 实际实现 |
| --- | --- | --- |
| Provider singleton 竞态 | 已存在，未重复改 | `sandbox_provider.py` 已具备锁外构造、winner 安装、loser shutdown 与锁外 teardown |
| blocking-I/O | 完成 | 新增严格测试，确认 AIO `get()` 是纯内存、异步 release 在线程执行，并用 Blockbuster 验证探针有效 |
| 路径遮罩与边界 | 完成 | 新增共享 `path_patterns.py`；Local 输出与工具错误共用缓存规则；Windows 反向 containment 和 prefix sibling 不再误匹配 |
| 命令虚拟路径 | 完成 | `/mnt/user-data-backup`、`.bak`、`_old`、数字后缀不再被当成合法根；根路径、子路径与标点边界有直接回归 |
| 单边行读取 | 完成 | `start_line` 单独表示读到 EOF，`end_line` 单独表示从首行读取，并保留越界/逆序错误 |
| 空 `str_replace` | 完成 | 空 `old_str` 明确 no-op，不会在字符间插入新内容；空文件与非空旧值仍报告未找到 |
| grep 普通文件 | 完成 | Local/AIO/E2B/BoxLite 均支持单文件 path；模型 schema 明确“文件或目录”，错误文案不再假定目录 |
| AIO grep 分页 | 完成 | 使用 SDK `offset` 有界翻页，过滤 `.git`/`node_modules`/glob 后不会静默漏掉下一页有效结果；空结果但预算截断时不再声称无匹配 |
| 远端 cwd | 完成 | 非 Local 命令以 `cd -- /mnt/user-data/workspace &&` fail closed 前缀执行；身份 export/unset 和请求级 Skill secret 仍保持隔离 |
| Overwrite 解包 | 完成 | 初始化、local 判断和 middleware release 都先解包 Overwrite；fork 恢复的父 Sandbox 不会被本次运行错误释放 |
| 环境变量策略 | 完成 | 扩展 `*PASS*` 与 `PGSERVICEFILE` 过滤；保留 `PWD`/`OLDPWD` 与精确请求级 secret 注入 |
| 私有 release 取消安全 | 完成 | 外部 task 取消后等待 destroy 线程结束并清空已释放状态；内部 `CancelledError` 保留状态供重试 |
| AIO 私有 orphan | 完成 fail closed | 启动 reconcile 跳过 `private-*`，既不销毁也不吸入 warm pool，等待 lease-aware Worker reaper |
| E2B 同大小变化 | 部分完成 | 列表加入 remote mtime；size 相同但 mtime 变化会下载并更新 host mtime |
| Provisioner 控制 API | 本地/Docker 完成 | `/api/*` 要求 `X-API-Key`，未配置/缺失/错误均 401；Worker 侧 Remote client 的 list/create/get/delete/确认请求携带同一 key |
| Nginx 公网边界 | 完成 | 删除 `/api/sandboxes` 到 Provisioner 的公网直通；公网 `/api/*` 只进入 Gateway |

主要生产落点包括：

- `backend/packages/harness/deerflow/sandbox/{path_patterns.py,overwrite.py,search.py,tools.py,middleware.py,env_policy.py}`；
- `backend/packages/harness/deerflow/sandbox/local/local_sandbox.py`；
- `backend/packages/harness/deerflow/community/{aio_sandbox,boxlite,e2b_sandbox}/`；
- `backend/app/private_work/sandbox_files.py`；
- `docker/provisioner/app.py`、`docker/nginx/nginx.conf`；
- `backend/packages/harness/deerflow/config/sandbox_config.py`。

### 16.2 明确未移植

以下内容没有被包装成“完成”：

1. **Helm Provisioner**：当前 Chart 没有 Worker Deployment。Provisioner 已 fail closed，
   但 Chart 无法把密钥只交给 Worker 与 Provisioner；把密钥交给 Gateway 会违反 M7
   Worker-only 边界。因此当前 Helm Provisioner 必然不可作为发布通过项，需在 Module 16
   补 Worker 和最小权限 Secret 注入。
2. **私有 capacity/wait/reject/burst、Redis ownership、warm pool 和 orphan destroy**：
   必须与 PostgreSQL Job/Run lease、capability revalidation 组合后重写。
3. **E2B sandbox-scoped sync manifest**：本轮只有 size + mtime 局部检测，没有
   `main` 的 sandbox-ID 绑定 manifest，也没有把旧 Host tree 同步当成私有文件 authority。
4. **远端私有文件回写**：仍须进入 `PrivateSandboxFileProjection`、quota 和 audit，
   不能直接回写旧用户/线程 Host 目录。
5. **Provisioner 通用代理 hardening**：默认 Docker 配置已用 `NO_PROXY` 覆盖
   `provisioner`；通用部署仍应改为不继承环境代理的专用 HTTP Session，避免 operator
   漏配时把控制密钥交给代理。
6. **Provisioner extra mount 整体回移**：没有移植会扩大 HostPath 面的旧 mount
   规范化；必须先有 Project/Owner/Run mount authority。

## 17. TDD 与自动化证据

本轮按红绿顺序覆盖了：

- 路径共享模块缺失、Windows 反向路径、prefix sibling；
- 单边读取和空字符串替换；
- Local/AIO/BoxLite/E2B 单文件 grep；
- 远端 cwd；
- Overwrite 解包；
- 环境变量过滤；
- E2B 同大小变化；
- 私有 release 取消清理；
- 私有 orphan 不进入暖池；
- Provisioner API key、Remote client header 与 Nginx 边界；
- AIO grep 跨过滤页和“空但截断”结果；
- 模型可见的单文件 grep 契约。

收口结果：

- Module 13 聚焦 Sandbox/Provider 套件：`664 passed, 7 skipped`；
- blocking-I/O 完整门禁：`26 passed`；
- Provisioner 六文件组合：`110 passed`；
- 完整后端：`7675 passed, 1014 skipped, 0 failed`；
- M1–M7 真实 PostgreSQL 门禁：`270 passed, 0 skipped`；
- Helm `lint`/`template` 只能证明语法可渲染，**不**证明缺 Worker 的 Provisioner
  运行边界可用。

完整后端首次运行曾有 5 个失败，全部是远端 cwd 新前缀已经生效、旧测试仍断言原命令；
更新测试后完整后端归零失败。真实 PostgreSQL 门禁只创建随机
`deerflow_test_*` 数据库。

## 18. 三轮真实浏览器验收

验收使用同一项目、同一 Thread、系统 `Main` Agent 和 DeepSeek V4 Pro；没有模拟模型，
也没有临时打开 Host bash。页面最终显示累计 `136.9K` Tokens。

1. **R1**：真实调用 `write_file` 创建五行文件，再调用只带 `start_line=3` 的
   `read_file`，准确返回第 3–5 行。
2. **R2**：先完整读取满足 read-before-write；空 `old_str` 调用不注入内容；实际替换
   `target-old` 为 `target-new`；对精确单文件 path 调用 grep，返回虚拟路径和第 3 行。
3. **R3**：刷新页面后仍在同一 Thread；重新读取和两次单文件 grep 证明替换持久、
   `SHOULD-NOT-APPEAR-M13` 不存在；`bash pwd` 被
   `LocalSandboxProvider + allow_host_bash=false` 明确拒绝，命令未执行。

截图和逐轮说明：

- [R1：写入与单边读取](evidence/13-sandbox/01-write-one-sided-read.png)
- [R2：替换与单文件 grep](evidence/13-sandbox/02-empty-replace-single-file-grep.png)
- [R3：刷新持久性与空字符串保护](evidence/13-sandbox/03-refresh-persistence-empty-guard.png)
- [R3：Host bash fail closed](evidence/13-sandbox/04-bash-fail-closed.png)
- [完整验收记录](evidence/13-sandbox/README.md)

## 19. 本轮结论

Module 13 的本地 Sandbox、工具正确性、异步阻塞隔离、私有取消安全和 Docker
Provisioner 公网边界已经落地并完成真实浏览器验收。Kubernetes Helm Provisioner、
lease-aware 私有 reconcile、私有 capacity/ownership/warm pool 与远端私有文件同步
仍属于明确 deferred；在这些边界补齐前，不宣称 Module 13 的所有目标环境已经完成。

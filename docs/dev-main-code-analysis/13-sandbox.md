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

`dev` 当前 Git 树不跟踪 `docker/`；工作区里的 `docker/` 是用户恢复的未跟踪目录，不能把它当成 `dev` 已正式采用的实现证据。

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

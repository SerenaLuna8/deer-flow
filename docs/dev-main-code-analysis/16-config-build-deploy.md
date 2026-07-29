# 16. Config / Build / Deploy 模块：`main` 具体实现与 `dev` 对照

## 1. 范围与结论

本文覆盖：

- `AppConfig` 的加载、环境变量解析、缓存和热更新；
- database/checkpointer/stream 等基础设施配置；
- 根 Makefile、服务启动脚本、pnpm runner；
- Nginx；
- Docker/Provisioner；
- Helm 和 CI/release gates。

不重复分析 Gateway 路由、Worker 执行、数据库 schema 业务语义，分别见对应模块。

核心结论：

1. `main@e317f7b8` 的配置项更多，但这是旧 Gateway-runtime、多存储后端、Redis stream bridge 等历史架构的结果；不能以字段数量判断它比 `dev` 新。
2. `dev@8a91e957` 已明确收敛为一个 `DATABASE_URL`、PostgreSQL-only、独立 Gateway/Worker/Scheduler 和 full-schema 初始化。`main` 的 `database.postgres_url + checkpointer.connection_string + stream_bridge` 不应恢复。
3. `main` 可直接吸收的部署修复包括 Corepack fallback、Nginx 长 prompt/Upgrade 头、Provisioner API key、Helm Sandbox `ClusterIP` 默认值和对应渲染测试。
4. `dev` 当前存在两个可验证的交付缺口：
   - Git 树中没有 `docker/`，但 `scripts/serve.sh` 和 `scripts/nginx.sh` 依赖 `docker/nginx/nginx.local.conf`；工作区恢复出的 `docker/` 仍是未跟踪文件，不等于已纳入 `dev`。
   - Helm chart 没有 Worker/Scheduler Deployment，且内嵌 `config_version: 26` 落后于根 `config.example.yaml: 28`。对 Worker-only 架构而言这是部署级阻塞项。

## 2. 两个分支的配置模型不是同一条版本线

### 2.1 `main` 的配置组成

`main:backend/packages/harness/deerflow/config/app_config.py` 包含：

- 模型、工具、Skills、Memory、Subagent、Guardrails；
- Auth、Authorization、Channels；
- `database`；
- `run_events`；
- `agent_storage`；
- `scheduler`；
- 可选 `checkpointer`；
- 可选 `stream_bridge`；
- `run_ownership`；
- `dedupe_storage`；
- Sandbox ownership/capacity；
- LLM process-wide concurrency。

`config_version` 最终为 31。

这套模型服务于 `main` 的执行方式：Gateway 内运行 graph，必要时多个 Gateway 通过 Redis/PostgreSQL 协调 Run/SSE/Sandbox。

### 2.2 `dev` 的配置组成

`dev` 删除了：

- `run_events`；
- `stream_bridge`；
- `extensions` / `extensions_config`；
- `agents_api`；
- `skill_scan` / `skill_evolution`；
- 独立 `checkpointer`；
- 旧 recovery/store 配置。

新增或强化：

- PostgreSQL-only `database`；
- 独立 `worker`；
- 独立 `scheduler`；
- `quotas`；
- `mcp_security`；
- Project/Owner 私有资源和数据库 catalog 对应的运行约束。

根 `config.example.yaml` 为 version 28。数字 28 小于 31，但它代表分叉后重构的 schema，不是 `main` 31 的旧版本。直接运行 `main` 的 config upgrade 会把已经移除的字段重新加回来。

## 3. 配置加载实现

### 3.1 `main` 加载链

`main:AppConfig.from_file()` 的逻辑可以概括为：

```text
resolve_config_path
  -> YAML safe_load
  -> 检查 config_version
  -> 递归解析 $ENV
  -> 应用 database 默认值
  -> 合并/加载 extensions_config.json
  -> Pydantic model_validate
  -> 校验 model/tool/tool-group 名称唯一
  -> 构建 O(1) 名称索引
  -> 同步各模块 singleton config
```

路径优先级：

1. 显式 `config_path`；
2. `DEER_FLOW_CONFIG_PATH`；
3. caller project root；
4. monorepo 的 backend/root legacy 位置。

`_drop_null_config_sections` 会删除有默认值的 `None` section，使“只保留注释的 YAML key”回到 default，而不是报 Pydantic list/dict 错误。必填 `sandbox` 仍然报错。

### 3.2 `dev` 加载链

`dev` 收紧为：

1. 显式路径；
2. `DEER_FLOW_CONFIG_PATH`；
3. 仓库根 `config.yaml`；
4. 不再搜索 backend legacy config。

加载后调用 `_apply_singleton_configs` 同步 title、summarization、memory、subagents、tool search、guardrails、ACP。

`dev` 的 `LEGACY_CONFIG_TOMBSTONES` 在 model validation 前显式拒绝移除项。例如：

```text
agents_api
run_events
stream_bridge
extensions / extensions_config
mcp_config / mcp_config_path
legacy_run_store / legacy_event_store
recovery
skill_evolution / skill_scan
```

还有路径级 tombstone，例如旧 uploads 限额、`scheduler.lease_seconds`、`worker.default_max_attempts` 和旧 quota limit 命名。独立 `checkpointer` 也单独报错，要求只配置 `database.url`。

这种 fail-fast 很重要：如果只是 `extra="allow"` 或静默忽略，运维人员会误以为旧参数仍生效。

### 3.3 环境变量

两边都递归解析以 `$` 开头的完整字符串：

```yaml
api_key: $DEEPSEEK_API_KEY
database:
  url: $DATABASE_URL
```

环境变量不存在时配置加载失败。它不是 shell 插值器：

- 不处理字符串中间的 `${VAR}` 拼接；
- 不应把 secret literal 写回 `config.yaml`；
- 日志和 `repr` 应避免打印数据库 URL。

`dev:DatabaseConfig.url` 使用 `repr=False`，并且当 `database.url` 缺失时只从 `DATABASE_URL` 取默认值。

## 4. 数据库配置：为什么 `dev` 只要一个 URL

### 4.1 `main`

`main:DatabaseConfig` 支持：

- `memory`；
- `sqlite`；
- `postgres`。

主要字段：

- `backend`；
- `sqlite_dir`；
- `postgres_url`；
- `pool_size`；
- `pool_recycle`；
- `command_timeout`；
- checkpoint channel full/delta；
- delta snapshot frequency；
- compiled graph cache size。

同一 PostgreSQL URL可生成 async SQLAlchemy、sync psycopg 和 checkpointer URL，但配置里仍可能再出现独立 `checkpointer.connection_string`。Helm 示例明确说 LangGraph Store 还读取 legacy `checkpointer` section，所以同一物理数据库被写两次。

此外 Redis stream bridge、run ownership、dedupe storage 都可能再引入连接 URL。

### 4.2 `dev`

`dev:DatabaseConfig` 只有：

```text
url
pool_size
max_overflow
pool_timeout_seconds
statement_timeout_seconds
```

只接受：

- `postgresql://`
- `postgresql+asyncpg://`

派生属性：

- `sqlalchemy_url`：统一为 `postgresql+asyncpg://`；
- `checkpointer_url`：统一为 driver-neutral `postgresql://`。

因此应用持久化、LangGraph checkpoint、Store、Job、durable stream、quota、audit 全部从同一个 `DATABASE_URL` 派生。没有必要再配置 `POSTGRES_ADMIN_URL` 或 `POSTGRES_ADMIN_USER` 作为应用运行变量。

管理权限 URL 只应在测试/显式建库场景出现，例如 `POSTGRES_TEST_URL` 用于创建随机 `deerflow_test_*` 数据库；它不是业务进程配置。

### 4.3 不应从 `main` 恢复的内容

- `database.backend: memory|sqlite`；
- `database.postgres_url` 旧字段；
- 独立 `checkpointer.connection_string`；
- `stream_bridge.redis_url`；
- 用 Gateway `run_ownership` 替代 Worker Job lease；
- 运行时初始化或迁移 schema。

`dev` 的唯一 schema 入口是 `make setup-db`，只接受空 PostgreSQL，执行完整 `full_schema.sql`、写入 `full_schema_v1` marker、seed catalog、初始化 LangGraph schema 和默认 Project。运行时和 `make check-db` 只读。

## 5. 热更新边界

两边都有 `reload_boundary.py`，但冻结对象不同。

### 5.1 `main` startup-only

包括：

- database；
- checkpointer；
- run_events；
- agent_storage；
- stream_bridge；
- sandbox；
- logging；
- channels/channel_connections；
- scheduler；
- run_ownership；
- dedupe_storage。

这是因为这些对象在 Gateway lifespan 或 `langgraph_runtime()` 启动时创建并缓存。

### 5.2 `dev` startup-only

包括：

- database；
- sandbox；
- logging；
- mcp_security；
- channels/channel_connections；
- scheduler；
- worker；
- quotas。

`mcp_security` 要求 Gateway、Scheduler、Worker 一起重启，避免 admission 与执行采用不同 endpoint allowlist/timeout。`worker` 捕获 polling/lease/concurrency/retry，不能靠 Gateway hot reload 改变。

文档和 schema description 都通过 `format_field_description()` 使用同一 registry；测试应保证“registry 中的字段必有 startup-only 描述，反之亦然”。

## 6. 配置缓存和 runtime override

`AppConfig` 构建三张私有索引：

- `_models_by_name`；
- `_tools_by_name`；
- `_tool_groups_by_name`。

这把每次模型/工具解析从线性扫描变为 O(1)，同时在 reload 后重建，避免索引指向旧对象。

全局加载器还维护：

- cached config；
- config path；
- mtime/signature；
- 是否为测试注入 custom config；
- `ContextVar` runtime-scoped override stack。

`push_current_app_config`/`pop_current_app_config` 允许一次 Run 使用 admission 时冻结的配置视图。对 `dev` 而言，这一机制只能承载非秘密、已准入配置；Agent/Skill/MCP 的最终运行定义仍应来自数据库 snapshot，不能用热重载全局对象替代。

## 7. 根命令和本地服务

### 7.1 `dev` 的真实拓扑

`dev:Makefile`：

```text
make dev
  -> make check
  -> scripts/serve.sh --dev
```

`serve.sh` 依次启动：

1. Gateway：`uvicorn app.gateway.app:app`，8001；
2. Worker：`python -m app.worker.app`，无端口，必需；
3. Scheduler：`python -m app.scheduler.app`，仅 enabled 时；
4. Frontend：`pnpm run dev`，3000；
5. Nginx：统一入口 2026。

它还：

- 只回收属于 DeerFlow worktree 的进程；
- 同时识别 linked worktree，避免误杀其他项目；
- 预检 8001/3000/2026；
- 生成 gateway/worker/scheduler/frontend/nginx 分离日志；
- 生产模式用 frontend preview；
- stop 时清理 sandbox 容器。

所以“本地开发 Node 能不能直接访问”答案是：能访问 3000，但完整产品需要同源 cookie、REST/SSE、上传、代理头和 Gateway，所以官方入口仍是 Nginx 2026。Nginx 不是 Worker 的替代品。

### 7.2 当前 `dev` 的 Git 缺口

`scripts/serve.sh` 和 `scripts/nginx.sh` 都硬引用：

```text
docker/nginx/nginx.local.conf
```

但 `git ls-tree -r dev docker` 为空。当前工作区 `docker/` 是未跟踪恢复内容。结果：

- 本机因为文件在工作区可能能启动；
- 新 clone、CI checkout 或别的开发者会缺文件；
- `make dev` 的源代码契约不自洽。

这应作为独立提交把经过 `dev` 路由审查的 Docker/Nginx 文件纳入 Git，而不是默认整目录 `main` 覆盖。尤其 `main` Nginx 仍有 `/api/langgraph/*` 旧路由和 browser session 路由。

## 8. `main` 的 pnpm runner

`main:scripts/pnpm.py` 的规则：

1. 优先 `pnpm`；
2. Windows 尝试 `pnpm.cmd`；
3. 否则 `corepack pnpm` / `corepack.cmd pnpm`；
4. 都没有时返回 127；
5. `subprocess.run(..., shell=False, cwd=frontend)`；
6. OSError 返回 126；
7. 负 signal return code 规范为 `128 - returncode`；
8. 非零状态打印明确错误。

这修复了“Node 自带 Corepack、但 pnpm 没有单独装进 PATH”时 check/install/serve 行为不一致。

`dev` 当前 `serve.sh` 和 Makefile 仍直接调用 `pnpm`。该 runner 可以完整移植，并让 `check.py`、install 和 serve 统一使用。

## 9. Nginx 实现

### 9.1 `main` 的两个具体修复

长 prompt：

- `/api/langgraph/` 设置 `client_max_body_size 20M`；
- `proxy_request_buffering off`；
- 避免超过默认 1 MiB 后落临时文件，非 root 本地 Nginx 因临时目录无权写而返回裸 500。

Frontend Upgrade：

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      '';
}
```

普通请求不再被强制 `Connection: upgrade`，Next.js HMR 的长连接不会被误判为非法 upgrade handshake。

### 9.2 其他关键规则

- Gateway upstream 8001；
- Frontend upstream 3000；
- 对 SSE 关闭 buffering/cache；
- upload 限额 100M 且关闭 request buffering；
- browser WebSocket location 必须排在泛化 thread route 前；
- `/api/*` catch-all 透传 auth cookies；
- frontend response buffering 关闭，避免非 root temp path permission；
- 本地只在 2026 对外提供统一入口。

### 9.3 `dev` 移植要求

可以吸收 body-size 和 conditional upgrade，但必须：

- 用 `dev` 的 `/api/*` Project REST/SSE 路由重写；
- 保留可信 proxy token / `X-Real-IP` 约束；
- 删除旧 `/api/langgraph` 兼容路径，除非明确需要；
- 不带入进程内 browser WebSocket 路由；
- 测试 cookie、CSRF origin、SSE reconnect 和大 prompt。

## 10. Provisioner 与 Helm Sandbox 暴露

`main` 把每个 Sandbox Service 默认从 NodePort 收紧到 `ClusterIP`：

- in-cluster Gateway 通过 Service DNS 访问；
- 不在每个 node interface 暴露代码执行面；
- 只有混合部署才显式选择 NodePort；
- NodePort 模式可显式 `nodeHost`，否则用 downward API node IP。

`scripts/check_chart_sandbox_service.sh` 真实 render 三组 chart：

1. 默认：`SANDBOX_SERVICE_TYPE=ClusterIP` 且无 `NODE_HOST`；
2. NodePort：有 NodePort 与 `NODE_HOST`；
3. NodePort + literal host：使用指定地址。

这类渲染测试应随修复一起移植。仅改 values 注释不足以证明 deployment env 正确。

Provisioner 还要求 `X-API-Key` 与 `PROVISIONER_API_KEY` 一致。Helm/Compose 必须从 Secret 给 Gateway 和 Provisioner 注入同一个值；不能写在 ConfigMap。

## 11. Helm 的分支差异

### 11.1 `main`

Chart 部署：

- Gateway；
- Frontend；
- Nginx；
- Provisioner；
- PostgreSQL；
- Redis；
- PVC；
- Ingress。

它仍以 Gateway graph execution 为中心，所以没有独立 Worker/Scheduler Deployment。

### 11.2 `dev`

`dev` 虽把运行时改为 Worker-only，但 chart template 列表仍没有：

- `worker-deployment.yaml`；
- `scheduler-deployment.yaml`。

Gateway Deployment 只运行 Uvicorn。这样装出的 Helm release 会：

- Gateway 接受并持久化 Run；
- 没有 Worker claim Job；
- Run 永远不执行；
- Automation 即使存在也没有独立 Scheduler 进程。

这是不能从 `main` 直接 cherry-pick 的问题，因为 `main` 本来就不需要这些 Deployment。必须在 `dev` 新建：

- Worker Deployment，带 Worker startup-only config、DB Secret、provider secrets、Sandbox/Provisioner access、termination grace；
- Scheduler Deployment（按 `scheduler.enabled`）；
- readiness/metrics/安全上下文；
- 各进程最小化 RBAC 和 Secret；
- Worker rolling update 的 lease/fencing 验证。

### 11.3 config version drift

当前可验证状态：

- `dev:config.example.yaml`：28；
- `dev:deploy/helm/deer-flow/values.yaml` 内嵌配置：26。

`main` 有 `scripts/check_config_version.sh`，会解析两处版本并在 chart 落后时失败。`dev` 删除了该脚本，因而漂移没有被阻断。

建议移植检查思想，并在修正 chart 字段后把 version 同步为 28；不能只改数字，因为 chart 还缺 `worker`、`scheduler`、`quotas`、`mcp_security` 等运行必需配置。

## 12. CI 与发布

### 12.1 `main`

新增 nightly workflow：

- 构建 backend/frontend/provisioner 镜像；
- 使用日期和 commit SHA tag；
- 发布 Helm OCI chart；
- 校验 chart；
- 仅 upstream repo 推 GHCR；
- 不覆盖稳定 `latest`。

另有分散 backend/frontend/E2E/skill review workflows。

### 12.2 `dev`

`dev:.github/workflows/project-saas-release-gates.yml` 是合并后的确定性发布门禁：

- PostgreSQL 17 service；
- 固定 M1-M7 20 文件 PostgreSQL gate，0 skip；
- 完整 backend pytest；
- ruff format/check；
- backend dependency security；
- frontend unit/check；
- deterministic Chromium E2E；
- production/static build；
- pnpm prod audit；
- tracked tree、review diff 和 git history security。

根 `AGENTS.md` 明确禁止再为这些命令新增重复 workflow。因此：

- 不应把 `main` 的分散 unit/E2E workflows直接恢复；
- nightly/container/chart publishing 可作为专用 workflow保留；
- chart validation 应并入现有 chart workflow；
- Worker/Scheduler 镜像与部署应进入现有发布契约。

## 13. Release gate 和数据库测试

`dev:Makefile` 的单一有序来源：

- `PROJECT_FOUNDATION_POSTGRES_TESTS`：20 个 M1-M7 文件；
- `make test` 运行该 M1-M7 PostgreSQL gate。

测试使用 `POSTGRES_TEST_URL` 创建随机 `deerflow_test_*` 库。该 URL需要建库权限，但业务 `.env` 仍只需要 `DATABASE_URL`，两者用途不能混淆。

## 14. 可移植项分级

### 直接移植

- `scripts/pnpm.py` 和调用方统一；
- `_drop_null_config_sections` 的通用处理；
- O(1) model/tool/group 索引及 reload 重建；
- PostgreSQL `postgres://` scheme 规范化思路；
- async engine pool recycle/command timeout 思路；
- Nginx conditional Upgrade；
- Nginx 大 prompt buffering；
- Provisioner API key；
- Helm Sandbox `ClusterIP` 默认值和 render test；
- chart config-version drift test。

### 需按 `dev` 重写

- LLM concurrency：集群范围应按 Worker 进程和 provider 配额设计；
- graceful shutdown：Gateway、Worker、Scheduler 分开设置；
- Helm topology；
- Docker Compose topology；
- Sandbox ownership/Redis；
- nightly 的镜像集合；
- Nginx 路由。

### 不应移植

- memory/sqlite persistence；
- 独立 `checkpointer`；
- `run_events`/`stream_bridge`；
- Gateway run ownership；
- `extensions_config.json` runtime source；
- 多个应用数据库角色/URL；
- runtime migration/repair；
- 重复 CI workflows。

## 15. 当前 `dev` 的具体行动清单

按优先级：

1. 把经审查的 `docker/` 文件正式纳入 `dev`，解决 fresh clone 无法 `make dev`。
2. 为 Helm 增加 Worker Deployment；这是 Run 能否执行的 P0。
3. 为 Helm 增加可选 Scheduler Deployment。
4. 把 chart 内嵌配置从旧字段完整更新到 schema 28，再同步 version。
5. 恢复 chart version 和 Sandbox service render checks。
6. 移植 Corepack pnpm runner并统一 check/install/serve。
7. 对 Nginx 只移植独立修复，按 Project REST/SSE 路由重写。
8. 为 Gateway/Worker/Scheduler 做跨进程 config compatibility/readiness 检查。

## 16. 建议验证

- fresh Git checkout 不依赖未跟踪文件即可 `make dev`；
- `make dev` 同时启动 Gateway、Worker、Frontend、Nginx，Scheduler按配置；
- 2026 同源登录、cookie、CSRF、SSE replay、大 prompt、upload；
- `helm template` 出现 Worker，并在 enabled 时出现 Scheduler；
- chart config 通过 `AppConfig.model_validate`；
- chart version 等于根 example version；
- Sandbox Service 默认 ClusterIP；
- Provisioner 未设置/错误 API key 均拒绝；
- Gateway-only Pod 不执行 graph；
- Worker shutdown 先停止 claim、续持已有 lease、在 grace 内完成或安全重试；
- CI PostgreSQL 20 文件 M1-M7 gate 0 skip；
- release source-absence gate 不把旧 config key带回 runtime roots。

## 17. 关键 `main` 演进

- `3bc3af25`：Gateway run ownership；
- `7d1a8fb7`：nightly images/chart；
- `8cc4b3ab`：Provisioner API key；
- `1300c6d3`：Authorization 配置脚手架；
- `d57f6957`：Sandbox Service 默认 ClusterIP；
- `7757e38b`：长 prompt Nginx 限额/关闭 buffering；
- `5994fdf3`：条件 Upgrade header；
- `dd2f73c1`：`postgres://` async ORM 规范化；
- `39493406`：PostgreSQL pool recycle/command timeout；
- `ac5fd462`：LLM 并发与 burst retry；
- `42baed8c`：delta checkpoint；
- `83803718`：Webhook dedupe PostgreSQL；
- `4e449385`：pnpm/Corepack fallback。

以上提交解释 `main` 的演进，但所有移植判断以最终源码、`dev` 的 Worker-only 边界和当前发布契约为准。

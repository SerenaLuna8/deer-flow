# 16. Config / Build / Deploy 移植计划与落地记录

## 0. 文档状态

本文是 Module 16 的计划、实现落点和验收记录，不是历史快照的简单复述。

当前核对基线：

- 当前分支：`dev`；
- 当前提交：`785be51341c1`；
- 对照分支：`main@e317f7b8d9b2`；
- Module 16 修改仍在当前工作树中，尚未提交；
- 完整后端、前端、固定 PostgreSQL gate、Nginx、Compose render、Helm
  lint/template 与确定性浏览器 E2E 已执行通过；
- 真实本地 Gateway、Worker、Scheduler、Frontend、Nginx 已使用隔离数据库启动，
  并完成多轮模型、大文件上传、Gateway 重启重放和截图验收；
- 精确隔离库 `deerflow_test_m16_acceptance_20260731_0125` 已在验收后删除，
  业务数据库未被连接或修改；
- 真实 Docker 容器启动和目标 Kubernetes 集群仍属于目标环境发布验证，不能由
  本地 render 和截图替代。

旧文档中的以下状态已经失效，不能继续作为结论：

1. “`docker/` 没有被 Git 跟踪”不成立。当前 `dev` 提交已经跟踪
   `docker/docker-compose*.yaml`、`docker/nginx/*.conf`、
   `docker/provisioner/*` 和 `docker/dev-entrypoint.sh`。
2. “Helm 没有 Worker/Scheduler”只描述了移植前状态。当前工作树已经新增
   `worker-deployment.yaml` 和 `scheduler-deployment.yaml`。
3. “Helm config 仍为 26”已经失效。当前工作树的 Helm 内嵌配置为 32，
   与根 `config.example.yaml` 一致。
4. 旧文档引用的 `dev@8a91e957` 不是当前核对基线；本文以
   `dev@785be51341c1` 加当前未提交工作树为准。

状态标记：

- **已落地**：代码已经存在于当前工作树；
- **聚焦验证通过**：已经执行对应的局部测试或静态检查；
- **本地验收通过**：完整自动化、本地真实进程、真实模型和浏览器证据均已完成；
- **目标环境待验收**：真实 Docker/Kubernetes 环境仍需发布前验证；
- **禁止**：与最终 Worker-only、PostgreSQL-only 架构冲突，不能从 `main`
  恢复。

## 1. 范围与最终边界

本文覆盖：

- `AppConfig` 加载、版本、环境变量、缓存和 startup-only 边界；
- 一个 `DATABASE_URL` 的 PostgreSQL-only 配置；
- 根 Makefile、Frontend Makefile、pnpm/Corepack runner 和本地启动脚本；
- 本地、Docker、Helm 三套 Nginx 配置；
- Docker Compose、Provisioner 和 Worker-only 敏感挂载；
- Helm 的 Gateway、Worker、可选 Scheduler、Secret 和数据库契约；
- chart、container、`dev` release gate 和 source-absence 检查；
- 完成移植后的真实服务与浏览器截图验收。

最终运行边界保持不变：

```text
Browser
  -> Nginx :2026
     -> Frontend :3000
     -> Gateway :8001
        - authentication
        - project/account/admin REST API
        - Run admission and query
        - durable SSE replay
     -> PostgreSQL

Worker
  - claims durable jobs
  - is the only Agent graph executor
  - writes durable stream frames
  - calls model/tools/sandbox

Scheduler (optional)
  - finds due Automations
  - admits occurrence/Run/job
  - never executes an Agent graph
```

数据库边界：

- 应用配置只有 `DATABASE_URL`；
- Gateway、Worker、Scheduler Runtime 不配置 `POSTGRES_ADMIN_URL`；
- 显式 `make setup-db` 仍临时要求一个指向 `postgres` maintenance database
  的 `POSTGRES_ADMIN_URL`；它可以使用同一个 `postgres` role，且不会注入
  Runtime；
- 不为 Gateway、Worker、Scheduler 分配多套应用数据库 URL；
- `make setup-db` 是唯一 schema 初始化入口；
- Runtime 只验证 `full_schema_v1`，不创建、升级、修补或删除 schema；
- 本轮没有连接、迁移或修改业务数据库。

## 2. `main` 的具体实现与移植判断

`main` 是实现来源之一，但不是架构真相。可复用的是独立修复，不可复用的是旧
Gateway graph runtime 和多存储配置。

| `main` 实现 | `main` 具体行为 | `dev` 决策与落点 |
| --- | --- | --- |
| `scripts/pnpm.py` | 先找 `pnpm`/`pnpm.cmd`，再找 `corepack`/`corepack.cmd`，固定从 Frontend 目录执行，透传退出码 | 直接吸收 runner 思路，统一到 `scripts/pnpm.py`，并覆盖所有本地入口 |
| `AppConfig._drop_null_config_sections` | 把只含注释而被 YAML 解析成 `None` 的 section 回退到默认值 | 当前 `dev` 已具备；保留必填 section 的失败语义 |
| model/tool/group 索引 | validation 后构建名称索引，reload 后重建 | 当前 `dev` 已具备 `_models_by_name`、`_tools_by_name`、`_tool_groups_by_name` |
| Nginx conditional Upgrade | 用 `$http_upgrade -> $connection_upgrade`，普通请求不强制 Upgrade | 移植到本地、Docker 和 Helm 三套 Nginx |
| Nginx 大请求修复 | 提高 body limit 并关闭 request buffering | 按 `dev` 的 `/api/`、Skill authoring 和 SSE 路由重写，不恢复 `/api/langgraph/*` |
| Provisioner API key | `/api/*` 要求 `X-API-Key`，缺失配置时 fail-closed | 移植到 `docker/provisioner/app.py`、Compose、Helm Secret 和 Worker sandbox client |
| Sandbox `ClusterIP` 默认 | K8s 内部访问默认不暴露 NodePort，并有 render test | Helm/Provisioner 默认 `ClusterIP`；Compose 的混合拓扑显式选择 `NodePort` |
| chart config/version scripts | 校验 chart config、sandbox exposure 和 render | 移植并强化为精确版本相等、Worker-only topology 和 Secret wiring |
| `main` 多存储配置 | `memory`/`sqlite`/`postgres`、独立 checkpointer、Redis stream bridge、Gateway run ownership | **禁止移植** |
| `main` Gateway graph runtime | Gateway 内构建和执行 Agent graph | **禁止移植**；Helm 必须新增独立 Worker |

相关 `main` 演进提交保留作追踪入口：

- `4e449385`：pnpm/Corepack fallback；
- `5994fdf3`：conditional Upgrade header；
- `7757e38b`：Nginx 大 prompt/request buffering；
- `8cc4b3ab`：Provisioner API key；
- `d57f6957`：Sandbox Service 默认 `ClusterIP`。

这些提交只能解释具体修复，不能覆盖 `dev` 的 Project/Owner 权限模型、
Worker-only 执行边界和 full-schema 生命周期。

## 3. 按可移植落点执行的计划

### 3.1 阶段 0：先固定架构和文件清单

目标：

- 不切换分支；
- 使用 `git show main:<path>` 读取 `main`；
- 先确认当前 `dev` 已有能力，避免重复移植；
- 只把独立修复迁入最终架构。

验收条件：

- Gateway、Worker、Scheduler 职责在代码、Compose、Helm、启动文案中一致；
- 只有一个应用 `DATABASE_URL`；
- `docker/` 必需文件可由 `git ls-files` 找到；
- 不出现旧 `/api/langgraph`、public Provisioner 或 Gateway graph executor。

当前状态：**已落地，聚焦验证通过**。

### 3.2 阶段 1：统一 pnpm/Corepack

移植来源：

- `main:scripts/pnpm.py`；
- `main` 中 check、doctor、support bundle、Makefile 和 serve 的 runner 调用。

`dev` 落点：

- `scripts/pnpm.py`；
- `scripts/check.py`；
- `scripts/doctor.py`；
- `scripts/support_bundle.py`；
- `scripts/serve.sh`；
- 根 `Makefile`；
- `frontend/Makefile`；
- `frontend/package.json`；
- `backend/tests/test_pnpm_runner.py`；
- `backend/tests/test_check_script.py`；
- `backend/tests/test_doctor.py`；
- `backend/tests/test_support_bundle.py`。

验收条件：

1. 优先使用直接 `pnpm`/`pnpm.cmd`；
2. 直接命令不存在时使用 `corepack`/`corepack.cmd pnpm`；
3. 所有命令固定在 `frontend/` 执行；
4. 不使用 `shell=True`；
5. 缺少 runner 返回 127，无法执行返回 126；
6. 正常非零退出码和信号退出码不被吞掉；
7. `make -n` 的根/Frontend 入口不出现裸 `pnpm`；
8. `preview` 内部不再递归调用裸 `pnpm`；
9. serve 文案明确 Gateway admission/query/SSE 和 Worker Agent graph execution。

当前状态：**已落地，聚焦验证通过**。

### 3.3 阶段 2：统一 Nginx 行为

移植来源：

- `main` conditional Upgrade；
- `main` 大请求和 request buffering 修复。

`dev` 落点：

- `docker/nginx/nginx.local.conf`；
- `docker/nginx/nginx.conf`；
- `deploy/helm/deer-flow/templates/configmap-nginx.yaml`；
- `backend/tests/test_module16_deploy_contract.py`。

验收条件：

1. 三套配置都有：

   ```nginx
   map $http_upgrade $connection_upgrade {
       default upgrade;
       ''      '';
   }
   ```

2. Frontend location 使用
   `proxy_set_header Connection $connection_upgrade`；
3. 普通请求不再固定发送 `Connection: upgrade`；
4. Skill archive authoring 保持 160 MiB 上限；
5. `/api/` 当前统一为 100 MiB，并设置
   `proxy_request_buffering off`；
6. Helm `/api/` 保留 SSE 所需的无缓冲、长 timeout 和
   `X-Accel-Buffering: no`；
7. Docker/Helm 继续注入 `X-DeerFlow-Proxy-Token`；
8. 不恢复 `/api/langgraph/*`；
9. 不公开代理 `/api/sandboxes/*`。

当前状态：**已落地，Nginx 语法和聚焦契约已验证**。

说明：100 MiB 当前落在 `/api/` catch-all，而不是只落在 Run/upload 的窄
location。这是当前实现事实。最终安全边界仍依赖 Gateway 的请求模型和文件上限；
如果后续收窄 Nginx route，必须同时验证登录、SSE、Run、上传和 Skill archive，
不能只改一套配置。

### 3.4 阶段 3：Provisioner、Compose 和敏感挂载

移植来源：

- `main` Provisioner API key；
- `main` `ClusterIP` 默认和 render 检查。

`dev` 落点：

- `docker/provisioner/app.py`；
- `docker/provisioner/README.md`；
- `docker/docker-compose.yaml`；
- `docker/docker-compose-dev.yaml`；
- `docker/docker-compose.dood.yaml`；
- `docker/docker-compose.cli-auth.yaml`；
- `backend/tests/test_module16_deploy_contract.py`；
- 现有 Provisioner、remote backend 和 sandbox tests。

验收条件：

1. `/health` 保持公开；
2. Provisioner `/api/*` 缺少、为空或错误 API key 时拒绝；
3. 比较使用 constant-time `secrets.compare_digest`；
4. 默认 `SANDBOX_SERVICE_TYPE=ClusterIP`；
5. `NodePort` 只有显式选择时生效；
6. Compose 的 Worker 在 Kubernetes Provisioner 混合拓扑中显式使用同一
   `PROVISIONER_API_KEY`；
7. Compose 明确选择 `NodePort`，因为 Worker 在 K8s 集群外；
8. Docker socket 只通过 `docker-compose.dood.yaml` 挂到 Worker；
9. `~/.claude`、`~/.codex` 只通过
   `docker-compose.cli-auth.yaml` 只读挂到 Worker；
10. Gateway 不获得上述 root-equivalent 或长效 CLI credential 目录挂载。

当前状态：**已落地，聚焦测试和 Compose 展开检查已通过**。

这里的“Worker-only”特指 Docker socket 和 CLI auth 目录等敏感挂载。共享
`env_file` 与共享 `AppConfig` 的环境变量解析是另一层契约，不能据此宣称每个
环境变量都已经做到进程级最小暴露。

### 3.5 阶段 4：重写 Helm runtime topology

该阶段不能直接复制 `main` chart，因为 `main` 仍以旧 Gateway graph runtime
为中心。

`dev` 落点：

- `deploy/helm/deer-flow/templates/gateway-deployment.yaml`；
- `deploy/helm/deer-flow/templates/worker-deployment.yaml`；
- `deploy/helm/deer-flow/templates/scheduler-deployment.yaml`；
- `deploy/helm/deer-flow/templates/configmap-config.yaml`；
- `deploy/helm/deer-flow/templates/_helpers.tpl`；
- `deploy/helm/deer-flow/templates/secret-app.yaml`；
- `deploy/helm/deer-flow/templates/provisioner-deployment.yaml`；
- `deploy/helm/deer-flow/templates/provisioner-rbac.yaml`；
- `deploy/helm/deer-flow/values.yaml`；
- `deploy/helm/deer-flow/README.md`；
- `deploy/helm/deer-flow/templates/NOTES.txt`；
- `frontend/src/core/i18n/locales/types.ts`；
- `frontend/src/core/i18n/locales/en-US.ts`；
- `frontend/src/core/i18n/locales/zh-CN.ts`；
- `frontend/tests/unit/m6-admin-operations.test.tsx`。

验收条件：

1. Gateway Deployment 只运行 `app.gateway.app:app`；
2. Worker Deployment 运行 `python -m app.worker.app`，是唯一 graph executor；
3. Worker 使用 backend 镜像、config checksum、非 root security context、
   `automountServiceAccountToken: false` 和 75 秒 termination grace；
4. Scheduler 默认不渲染；
5. `scheduler.enabled=true` 时只渲染一个 Scheduler 副本，并运行
   `python -m app.scheduler.app`；
6. Helm 顶层 `scheduler.enabled` 被投影进 AppConfig，避免 Deployment 与
   runtime 配置漂移；
7. Helm 强制 `worker.enabled=true`；
8. Gateway、Worker、Scheduler 都获得数据库和共享 AppConfig 加载所需环境；
9. Provisioner 使用专用 ServiceAccount；其他进程不自动挂 ServiceAccount
   token；
10. chart config 通过当前 `AppConfig` validation。
11. Admin Operations 能准确显示 Scheduler 的 `owned`、`unowned` 和
    `ownership_lost`，不能把后端真实所有权状态降级显示为 `Unknown`。

当前状态：**已落地；Helm render/lint、Python 契约和真实运行拓扑页面均已验证**。

### 3.6 阶段 5：Secret 分域和 Sandbox 暴露

Helm Secret 分为三个资源域：

1. Application Secret：
   `AUTH_JWT_SECRET`、`BETTER_AUTH_SECRET`、audit keyring、credential
   keyring、internal auth token、proxy attestation token 和
   `PROVISIONER_API_KEY`；
2. Database Secret：只提供 `database-url`，bundled evaluation 模式另含
   PostgreSQL password；
3. Provider Secret：模型、Channel 和外部 provider 的 `$ENV`。

实现约束：

- application Secret 使用 Helm `lookup` 在 upgrade 时复用已有值；
- audit 和 credential keyring 分别生成 32-byte key，不复用 key material；
- `existingAppSecret` 必须包含模板要求的全部 key，缺失时 Pod 启动失败；
- `config.yaml` 只保存 `$PROVISIONER_API_KEY` 引用，不保存明文；
- Provisioner Deployment 从 application Secret 注入相同 key；
- `ClusterIP` 为默认；
- `NodePort` 才注入 `NODE_HOST`。

当前状态：**已落地，聚焦 render/Secret 结构测试已通过**。

注意：三个 backend 角色都会先加载共享 `AppConfig`，因此 Helm helper 给
Gateway、Worker、Scheduler 注入完整 application Secret 域，并在配置引用
provider `$ENV` 时注入 Provider Secret。本文不把这描述成完全的进程级
least-privilege；它是为了避免同一 config 在不同角色启动时出现环境解析漂移。

### 3.7 阶段 6：数据库默认和 fail-fast

`dev` 的 schema lifecycle 不允许 Helm runtime 自动迁移。

落地点：

- `deploy/helm/deer-flow/values.yaml`；
- `deploy/helm/deer-flow/templates/configmap-config.yaml`；
- `deploy/helm/deer-flow/templates/postgres-secret.yaml`；
- `deploy/helm/deer-flow/templates/postgres-statefulset.yaml`；
- `deploy/helm/deer-flow/README.md`；
- `deploy/helm/deer-flow/templates/NOTES.txt`；
- `backend/tests/test_module16_deploy_contract.py`。

当前契约：

1. `postgresql.enabled` 默认 `false`；
2. 默认推荐外部、已经显式初始化的 PostgreSQL；
3. 没有 external URL/Secret 时，`helm template` 直接 fail；
4. external URL 与 external Secret 不能同时设置；
5. external 模式不能误用 bundled `postgresql.existingSecret`；
6. bundled PostgreSQL 只是显式 opt-in 的空实例；
7. chart 不包含 schema setup Job；
8. chart 不执行 Alembic、`create_all`、stamp、DDL repair 或 incremental
   migration；
9. 用户必须从对应 checkout 对空库执行：

   ```bash
   POSTGRES_ADMIN_URL=postgresql://postgres:...@host:5432/postgres \
   DATABASE_URL=postgresql://postgres:...@host:5432/deerflow \
     make setup-db
   DATABASE_URL=postgresql://... make check-db
   ```

10. Runtime 看到旧 marker、未知 marker、非空未纳管 schema 或 catalog drift
    时 fail-closed。

当前状态：**已落地，默认 fail-fast 和带 external DSN render 已聚焦验证**。

没有执行任何业务库初始化。测试使用的 Helm DSN 是无真实连接的 render 占位值。

### 3.8 阶段 7：CI、发布和 source-absence

落地点：

- `.github/workflows/project-saas-release-gates.yml`；
- `.github/workflows/container.yaml`；
- `.github/workflows/chart.yaml`；
- `scripts/check_config_version.sh`；
- `scripts/check_chart_sandbox_service.sh`；
- `scripts/check_chart_runtime_topology.sh`；
- `backend/tests/test_m7_source_absence.py`；
- `backend/tests/test_module16_deploy_contract.py`。

已落地内容：

1. Project SaaS release gate 的 push branches 加入 `dev`；
2. container workflow 删除已经不存在的 `UV_EXTRAS=postgres`；
3. chart workflow 不再只做“版本不能落后”的弱检查；
4. `check_config_version.sh` 要求 chart version 与 root example **精确相等**；
5. `check_chart_sandbox_service.sh` 验证：
   - 默认 `ClusterIP`；
   - 默认没有 `NODE_HOST`；
   - 显式 `NodePort` 时才出现 `NODE_HOST`；
6. `check_chart_runtime_topology.sh` 验证：
   - Gateway 和 Worker 必须存在；
   - Scheduler 默认不存在、enabled 时存在；
   - Worker command 和关键 Secret wiring 存在；
7. source-absence 的生产文件 inventory 从仅 Nginx 扩展到：
   - Docker Compose；
   - Provisioner Python；
   - Docker shell/YAML/conf/Dockerfile；
   - 根 `scripts/*.py` 和 `scripts/*.sh`；
   - Helm；
8. Module16 deployment contract 单独检查 CI workflow、Compose、Nginx、
   Helm、Secret 和 tracked-tree。

当前状态：**代码已落地；完整后端、前端、固定 PostgreSQL gate、确定性
Chromium E2E 和 source-absence 契约已在当前工作树实际执行通过**。

仓库已有 consolidated release workflow。不得为同一 backend/frontend/E2E
命令恢复 `main` 的重复 workflow。

## 4. 已落地结果

### 4.1 Config 基线

当前 `dev` 已经具备：

- 显式参数 > `DEER_FLOW_CONFIG_PATH` > 根 `config.yaml` 的确定路径；
- 递归完整 `$ENV` 引用解析，缺失环境变量 fail-fast；
- PostgreSQL-only `DatabaseConfig` 和 `repr=False` URL；
- legacy config tombstone；
- `_drop_null_config_sections`；
- model/tool/group O(1) 名称索引；
- mtime/size/digest config cache；
- `ContextVar` runtime override stack；
- database、sandbox、logging、MCP security、channels、scheduler、worker、
  quotas 的 startup-only registry；
- `config-upgrade.sh` 的显式路径、备份和 v32 合并。

本轮没有恢复 `main` 的独立 checkpointer、extensions config 或多存储配置。

### 4.2 pnpm/Corepack

当前所有受支持本地入口都通过 `scripts/pnpm.py`：

```text
make install
frontend/Makefile: install/build/dev/test/test-e2e/lint/format/build-static
scripts/check.py
scripts/doctor.py
scripts/support_bundle.py
scripts/serve.sh: install/dev/preview
```

`frontend/package.json` 的 preview 为：

```json
"preview": "next build --webpack && next start"
```

这样 Corepack-only 主机不会在进入 package script 后再次寻找裸 `pnpm`。

### 4.3 Nginx

三套 Nginx 已统一：

- conditional Upgrade；
- `/api/` 100 MiB；
- request buffering off；
- SSE/长连接；
- Skill archive 160 MiB；
- Docker/Helm proxy attestation；
- 无 legacy LangGraph rewrite；
- 无 public Provisioner route。

### 4.4 Docker 与 Provisioner

已落地：

- Provisioner `/api/*` API key fail-closed；
- Provisioner 默认 `ClusterIP`；
- Compose 混合拓扑显式 `NodePort`；
- Worker 和 Provisioner 的 key wiring；
- DooD socket 只挂 Worker；
- Claude/Codex CLI auth 目录只读挂 Worker；
- Gateway 注释和命令明确只负责 API/admission；
- Compose 仍有必需 Worker 和可选 Scheduler。

### 4.5 Helm

已落地：

- Gateway；
- 必需 Worker；
- 可选单副本 Scheduler；
- config checksum；
- v32 AppConfig；
- application/database/provider Secret 分域；
- independent audit/credential key material；
- Provisioner API key；
- Sandbox 默认 `ClusterIP`；
- external pre-initialized PostgreSQL 默认；
- 无数据库值 fail-fast；
- 无 runtime migration；
- Admin Operations 对 Scheduler ownership 的 `owned`、`unowned`、
  `ownership_lost` 状态有明确中英文显示。

### 4.6 CI 与发布

已落地：

- `dev` push release gate；
- container workflow 移除旧 postgres extra；
- chart lint/render；
- topology、sandbox exposure 和精确 config version scripts；
- source-absence 扩展到 Docker 与 root scripts；
- Module16 deployment contract。

## 5. 明确禁止的移植

以下内容不能从 `main` 恢复：

1. Gateway 内执行 Agent graph；
2. Gateway run ownership；
3. `/api/langgraph/*` 或旧 thread/run API；
4. public `/api/sandboxes/*`；
5. `memory`/`sqlite` persistence backend；
6. 独立 `checkpointer.connection_string`；
7. Redis `stream_bridge`；
8. `extensions_config.json` runtime source；
9. 多套应用数据库 URL，或把仅供 setup 的 `POSTGRES_ADMIN_URL` 注入
    Runtime；
10. runtime Alembic、`create_all`、schema stamp、repair 或增量 migration；
11. 用旧 marker 或现有业务库“原地升级”；
12. 把 Docker socket 挂到 Gateway；
13. 把 Claude/Codex 整个认证目录默认挂到 Gateway；
14. 把 Secret literal 写进 Helm ConfigMap、`values.yaml` 或
    `config.yaml`；
15. 重复 backend/frontend/E2E CI workflow；
16. 为了“验证”而连接、清空或重建业务数据库。

`run_events` 不在禁止列表中：它是当前 PostgreSQL durable stream/replay 的正式
持久化表。本轮浏览器验收的隔离库实际写入了 3,768 条 `run_events`。

## 6. 验证矩阵

### 6.1 已执行的完整自动化与部署契约

以下结果均来自当前工作树的实际命令：

| 验证 | 结果 | 覆盖 |
| --- | --- | --- |
| backend 完整 pytest | 0 failure | backend 全量单元、集成和 source-absence；外部数据库用例另由固定 PostgreSQL gate 执行 |
| M1-M7 固定 PostgreSQL gate | 276 passed，0 skipped | 随机 `deerflow_test_*` 数据库、Gateway/Scheduler/Worker、lease、replay、隔离 |
| `test_module16_deploy_contract.py` | 11 passed | Helm topology、RBAC、DB fail-fast、Secret、Nginx、Compose、workflow、tracked Docker |
| backend Ruff check/format | passed | 1,153 个 Python 文件格式与 lint |
| frontend unit | 188 files，1,350 passed，0 skipped | 包含 Scheduler ownership 三种状态的显示回归 |
| frontend `check` | passed | ESLint + TypeScript |
| frontend production build | passed，78 routes | Next.js webpack 生产构建 |
| frontend static build | passed | 静态站点构建 |
| deterministic Chromium E2E | 109 passed | 当前确定性浏览器 E2E |
| `nginx -t` | local 和 production substitution passed | 本地与容器 Nginx 语法 |
| Compose config | prod/dev 及 DooD/CLI overlay passed | service、volume、environment 合并结果 |
| Helm scripts/lint/template | passed | config v32、ClusterIP/NodePort、Gateway/Worker/Scheduler、Secret wiring |

普通 Frontend 请求通过 Nginx 实测为 `Connection: keep-alive`，没有被固定为
`upgrade`。

当前状态：**完整本地自动化通过**。

### 6.2 已执行：真实本地服务、模型与浏览器

本轮没有直接执行会被 `.env` 再次覆盖数据库 URL 的 `make dev`，而是以相同进程
命令分别启动五个角色，并在 shell 中先加载 `.env`、再显式覆盖为隔离数据库：

```text
Nginx     :2026
Frontend  :3000
Gateway   :8001
Worker    python -m app.worker.app
Scheduler python -m app.scheduler.app
```

隔离库：

```text
deerflow_test_m16_acceptance_20260731_0125
```

真实验收结果：

1. 空库完整 `make setup-db` 后创建指定验收账号并进入 `default-project`；
2. `deepseek-v4` 最终干净会话完成两轮调用，第 2 轮准确复述第 1 轮标记；
3. Nginx 接收 3.25 MB 文本附件，上传接口返回 `201`；
4. Worker 用一次文件读取步骤返回附件真实第一行；
5. Gateway 停止并以新进程启动，Worker/Scheduler 保持运行；
6. 刷新后历史、附件、token 和完成状态从 PostgreSQL 恢复；
7. 重启后的新模型调用不重新读文件，仍准确复述上一轮标记；
8. Admin Operations 显示全局 `Ready`、Worker 1/容量 4、Scheduler
   `Ready` 且 ownership 为 `Owned`；
9. Worker 日志记录所有 7 个 Run 的注册和技术成功终态；隔离库共有
   3,768 条 `run_events`。

截图和逐 Run 记录：

```text
docs/dev-main-code-analysis/evidence/16-config-build-deploy/README.md
docs/dev-main-code-analysis/evidence/16-config-build-deploy/01-isolated-workspace.jpg
docs/dev-main-code-analysis/evidence/16-config-build-deploy/02-real-model-two-rounds.jpg
docs/dev-main-code-analysis/evidence/16-config-build-deploy/03-large-upload-real-model.jpg
docs/dev-main-code-analysis/evidence/16-config-build-deploy/04-gateway-restart-replay.jpg
docs/dev-main-code-analysis/evidence/16-config-build-deploy/05-runtime-readiness.jpg
```

第一次大文件压力尝试要求同时寻找第 1 行和第 50,000 行，模型执行 20 次调用、
累计 441,064 tokens 后发生超长上下文压缩并答非所问。该次虽然 Run 技术状态为
`success`，语义验收明确判定失败，没有冒充通过证据。随后使用同一个 3.25 MB
文件把“部署上传边界”和“文件随机访问能力”拆开，首行读取与 Gateway 重启重放
均通过。完整说明见证据 README。

当前状态：**真实本地进程、模型和浏览器截图验收通过；压力失败已如实保留**。

### 6.3 已执行：隔离环境清理

- 五个本地进程均已停止；
- `2026`、`3000`、`8001` 端口均已释放；
- 精确测试库 `deerflow_test_m16_acceptance_20260731_0125` 已删除并确认不存在；
- 业务数据库未被连接、初始化、清空或修改；
- 截图不包含验收账号或密码。

当前状态：**清理完成**。

### 6.4 待执行：Docker 与真实 Kubernetes

还需要目标环境验证：

- Compose prod/dev 实际启动；
- Worker claim 并完成 Run；
- Provisioner 缺 key、错 key、正确 key 三种真实请求；
- Docker NodePort 混合网络；
- Kubernetes `ClusterIP` sandbox 连通；
- Scheduler enabled/disabled；
- Worker rolling termination 和 lease/fencing；
- Gateway restart 后 SSE cursor replay；
- external pre-initialized PostgreSQL；
- bundled empty PostgreSQL 未初始化时 runtime fail-closed。

当前状态：**待执行**。

## 7. 已知剩余风险

1. Worker/Scheduler 的 Helm exec probe 当前只验证 PID 1 存活，不等价于数据库
   registration、lease 或业务 readiness。本地验收已经结合 Admin Operations 和
   PostgreSQL process readiness；目标集群仍必须重复这项检查。
2. Gateway、Worker、Scheduler 尚无显式 config compatibility fingerprint。
   startup-only 配置变更依靠 ConfigMap checksum 滚动重启；滚动窗口内仍需验证
   MCP security、checkpoint mode、quota 等配置不会跨版本漂移。
3. `/api/` 100 MiB 是宽 catch-all。后续如按 Run/upload 收窄，必须同步三套
   Nginx 并重跑登录、SSE、上传和 Skill archive。
4. bundled PostgreSQL 是单实例、显式 opt-in 的空库，只适合受控评估；不能把它
   描述成生产 HA 或自动初始化方案。
5. `backend/packages/harness/deerflow/config/reload_boundary.py` 中部分历史说明仍需
   与独立 Scheduler/Worker 进程措辞持续校对，不能让 operator 误以为重启 Gateway
   就能应用所有 startup-only 配置。
6. 3.25 MB 文件首尾随机访问压力尝试暴露了工具逐段查找效率和超长上下文压缩风险。
   该问题不影响本轮已验证的上传、首行读取和 Gateway replay，但后续工具/上下文
   模块应提供尾部或随机行访问能力，并为语义终态建立独立验收。

## 8. 完成定义

Module 16 只有在以下条件同时满足时才算完成：

- 当前工作树的 Module16 文件已完整落地并通过提交前检查；Git 提交/推送只在用户
  明确要求时执行；
- pnpm/Corepack 所有入口统一；
- 三套 Nginx 行为一致；
- Provisioner API key 和 Sandbox exposure 验证通过；
- Compose 的敏感挂载保持 Worker-only；
- Helm 默认渲染 Gateway + Worker，Scheduler 仅在 enabled 时渲染；
- chart config 精确为 v32 且通过 `AppConfig` validation；
- 外部预初始化数据库默认和无配置 fail-fast 生效；
- 没有 runtime migration；
- `dev` CI、container、chart 和 source-absence gates 生效；
- 完整 backend/frontend/PostgreSQL gate 通过；
- 真实模型多轮调用通过；
- 浏览器截图证据已保存并可追踪；
- 没有恢复任何“明确禁止”的旧架构。

截至本文本次更新：

- **Module 16 的代码移植、完整本地自动化、真实服务、真实模型、Gateway 重启
  replay 和浏览器截图验收已经完成；**
- **本轮没有执行 Git commit/push；**
- **真实 Docker 容器和目标 Kubernetes 集群仍是发布环境验证项，不能把本地
  render 结果写成生产部署认证。**

因此可以把 Module 16 标记为**本地移植与验收完成**，但不能标记为“目标集群
发布认证完成”。

# M7 最终 Legacy 清理与发布前基线重置设计

- 日期：2026-07-18
- 状态：待实施
- 总体规格：`docs/superpowers/specs/2026-07-12-project-first-saas-design.md`
- 前置里程碑：M1、M2、M3、M4、M5、M6 已正式完成
- 当前总体进度：6/8（75%）
- 里程碑：M7 — 最终 legacy source/API 清理与回滚窗口收口
- 后续里程碑：M8 完整发布验收

## 1. 文档目的

本文档定义 DeerFlow M7 的产品边界、代码删除范围、数据库基线、配置收口、运维语义和验收门禁。
M7 不增加新的业务能力，而是把 M1–M6 已经切换完成但仍为迁移、兼容或回滚保留的旧路径彻底移除，
让 M8 面对一个只有最终产品路径的候选版本。

项目截至 2026-07-18 尚未上线，没有生产用户、生产数据库或必须兼容的历史安装。用户已确认 M7
采用发布前重置，而不是保留旧版本升级能力。因此 M7 可以删除旧数据库迁移工具、历史兼容 API、
旧前端页面和旧运行时来源，并把数据库迁移链重置为当前最终结构的单一基线。

该决定不授权程序自动删除任何本地数据库或目录。检测到旧开发数据库时，命令必须停止并明确要求
开发者自行重建；M7 只删除仓库中的兼容实现，不猜测或清理未明确指定的本地数据。

## 2. 决策优先级

发生冲突时按以下顺序处理：

1. 用户于 2026-07-18 确认的“未上线、无用户、无需旧版本兼容”边界；
2. 本 M7 专项规格；
3. 总体规格中仍适用于最终产品的冻结决策；
4. M1–M6 专项规格中仍适用于最终产品的授权、隔离、可靠性和恢复契约；
5. 实施计划与代码注释。

M1–M6 中“保留到 M7”“观察期内保留”“兼容旧安装”的要求在 M7 完成后失效。M1–M6 的项目、
owner、权限、配额、审计、Worker、Scheduler、持久化 SSE、备份恢复和删除墓碑约束继续有效。

## 3. 已冻结决策

1. M7 是发布前重置，不支持从 M1–M6 任一开发中间版本原地升级到 M7。
2. M7 只支持两种数据库来源：M7 空库初始化，以及由 M7 格式备份恢复出的新空数据库。
3. Alembic 历史链重置为 `0001_project_saas_baseline`，表示最终 SaaS schema；旧 `0001`–`0015`
   开发迁移文件不进入 M7 运行树。
4. 通用、M4、M5、M6 migration run/ledger/cutover marker 表不进入 M7 最终 schema；运行时不再通过
   marker 在 legacy 与 final 路径之间选择。
5. 检测到旧 Alembic revision、旧 marker 表或非空未知 schema 时，setup/migrate 命令 fail closed，
   不自动 drop、truncate、stamp 或搬迁数据。
6. 删除旧 SQLite、shared asset、private-work、Automation 和 reliability staged migration CLI；不保留
   隐藏参数、兼容别名或仅文档化入口。
7. 删除 legacy HTTP router 后，旧 URL 统一成为普通 `404`；不保留 `409 *_CUTOVER` 提示 router、
   redirect、代理或兼容响应。
8. 删除 legacy workspace 页面后，旧 URL 统一进入 Next.js not-found；不保留迁移完成提示页。
9. 浏览器只使用 `/workspace` 多项目入口、`/projects/{project_slug}` 项目壳层和 `/admin` 平台治理区。
10. Gateway 只保留认证、项目与平台 API、project-private admission/query/SSE、项目连接入口和必要的
    server-to-server channel/webhook 入口；不再挂载 ownerless/global private-work runtime。
11. Worker 继续是 Agent graph 的唯一执行者；Scheduler 继续只负责 Automation admission。Gateway 不再
    构造 legacy `RunManager`、可配置 in-memory run-event store 或 legacy stream bridge。
12. PostgreSQL 继续是 runtime、checkpoint、store、job、stream、quota、audit 和 recovery 的唯一权威。
13. `config.yaml` 只从仓库根目录或显式 `DEER_FLOW_CONFIG_PATH` 读取；删除 backend/repository 猜测回退。
14. 删除 `run_events`、`stream_bridge`、`agents_api` 和 file-backed shared-asset runtime 配置；旧 key
    必须触发明确配置错误，不能静默忽略。
15. 删除 `extensions_config.json`/`mcp_config.json` 运行时 authority。系统 Agent、Skill 和 MCP 使用
    PostgreSQL catalog；项目运行只消费持久化 asset snapshot 与 credential grant。
16. 仓库提交的内置 Agent/Skill 内容可以作为构建期输入保留，但生产运行时不能扫描其目录。空库初始化
    通过一个版本化、确定性的 bootstrap catalog 把必需系统资产写入 PostgreSQL。
17. bootstrap 只在显式空库 setup 中运行，必须事务化、幂等并校验内容 digest；Gateway/Worker/Scheduler
    启动不得隐式导入或修补系统资产。
18. M6 加密备份、新库恢复、external tombstone journal、restore proof 和 drill 保留，但只接受 M7 baseline
    archive；pre-M7 archive 返回稳定的不支持错误。
19. 历史设计和实施计划保留在 `docs/superpowers/` 作为开发记录；已经失效的迁移 runbook 不再作为
    活跃运维文档或 Make help 入口。
20. M7 完成后总体进度只更新为 7/8（87.5%）；M8 未完成时不得宣称完整多用户 SaaS 可发布。

## 4. 目标与非目标

### 4.1 目标

- 建立只包含当前最终表、约束、索引、trigger 和 seed contract 的单一 PostgreSQL baseline。
- 删除所有 staged migration、cutover marker、legacy read/write adapter 和 runtime fallback。
- 删除全局 Thread/run/file/artifact/Memory/connection/Automation/shared-asset 兼容 API。
- 删除旧 workspace 页面、旧 API client、旧 URL fallback 和旧 query key。
- 把项目页面复用的 Thread、Memory、schedule、message 和 artifact 代码收敛为可注入的 project-scoped
  纯组件/类型，不再携带 global URL 默认值。
- 删除只服务 legacy runtime 的配置、依赖构造、内存后端、Nginx rewrite 和启动迁移。
- 保持 M1–M6 最终业务能力、隔离、Worker/Scheduler 拓扑、配额、审计和恢复能力不退化。
- 建立“旧 surface 不存在”的静态和运行时门禁，为 M8 提供干净发布候选。

### 4.2 非目标

- 兼容或迁移任何开发中间版本数据库。
- 为被删除 URL 返回迁移说明、redirect、`410` 或专用兼容错误。
- 新增业务功能、角色、配额维度、Automation 类型或共享模型。
- 自动删除开发者本地数据库、备份、`.deer-flow` 目录或自定义文件。
- 执行 M8 的完整安全、容量、渗透、浏览器矩阵和正式发布验收。
- 删除 M1–M6 的历史规格、实施计划、Git 提交或测试证据记录。

## 5. 最终架构

### 5.1 支持的产品路径

```text
Browser
  -> /workspace
  -> /projects/{project_slug}/...
  -> /admin/...

Gateway
  -> auth/account/project/admin APIs
  -> project assets/automations/private-work/memory/connections
  -> project run admission/query/durable SSE
  -> required channel/webhook inbound adapters

Scheduler
  -> due Automation
  -> atomic occurrence + Run + job admission

Worker
  -> durable job claim
  -> project/owner authorization recheck
  -> run_agent()
  -> PostgreSQL checkpoint/store/stream/quota/audit
```

不存在从请求、配置、marker、文件或异常分支回到 global workspace runtime 的路径。

### 5.2 删除的产品路径

以下 live HTTP surface 必须从 OpenAPI 和 router tree 消失：

- global `/api/threads*`、`/api/runs*`、`/api/assistants*`；
- global Thread uploads、artifacts、feedback、suggestions、input-polish 和 token usage；
- global `/api/memory*`；
- global ownerless `/api/channels/connections*`；
- global `/api/channels` status/restart 和 `/api/console*` owner-wide 运营统计；频道进程状态只进入现有
  system-admin operations readiness 聚合；
- legacy `/api/scheduled-tasks*` 和 Thread scheduled-task links；
- file-backed `/api/agents*`、`/api/skills*`、legacy `/api/mcp/config*`；
- 只返回 `agents_api` 的 `/api/features`；
- global `/api/input-polish`；项目 composer 改用
  `/api/projects/{project_id}/private-work/input-polish`，并在调用模型前验证当前 project/owner capability；
- Nginx `/api/langgraph/*` 到 global runtime 的兼容 rewrite。

项目和平台 API 的相似名路径不受影响。Channel provider metadata、OAuth callback、签名校验后的 inbound
webhook/IM endpoint 只有在最终 project/owner authority 链仍消费它们时保留；全局 connection CRUD 必须删除。

以下 live frontend route 必须消失：

- `/workspace/chats*`；
- `/workspace/agents*`；
- `/workspace/memory`；
- `/workspace/skills`；
- `/workspace/tools`；
- `/workspace/scheduled-tasks`；
- `/workspace/projects` 兼容 redirect。

静态演示可以保留不联网的展示组件和 fixture，但只能在静态构建的 `/workspace` 渲染，不能注册上述
live route，也不能请求被删除 API。

### 5.3 共享前端代码边界

项目 Chats、Memory、Automation 和 artifact UI 当前复用了部分 `workspace`/`core` 代码。M7 不按目录名
盲删，而是先拆分职责：

- 保留 message rendering、Thread types、schedule parser/recipes、Memory view model、artifact viewer 等纯代码；
- project adapter 必须显式注入 account、project、capability、client 和 URL builder；
- 删除 default global client、global query key、`/workspace/*` fallback 和 `/api/threads*` URL builder；
- live project 组件缺少 scoped provider 时 fail closed，不能回退 global client；
- static demo 使用单独的 mock adapter，不进入生产 API client registry。

## 6. PostgreSQL 基线重置

### 6.1 单一 baseline

M7 用 `0001_project_saas_baseline` 创建 M1–M6 最终业务结构。它必须与最终 ORM metadata、约束、索引和
trigger 一致，并在空库上一次升级到 head。历史 `0001`–`0015` revision 及其 expand/finalize 分支从运行树
删除，由 Git 历史保存。

最终 schema 不包含：

- `migration_ledger`；
- `private_work_migration_runs`、`private_work_migration_ledger`、`private_work_cutover_state`；
- `automation_migration_runs`、`automation_migration_ledger`、`automation_cutover_state`；
- `reliability_migration_runs`、`reliability_migration_ledger`、`reliability_cutover_state`；
- 只为 staged backfill、legacy owner mapping 或兼容读取存在的列、constraint 和 index。

业务表继续包含 final project/owner scope、复合外键、quota trigger、append-only audit trigger、durable job、
stream、backup/recovery 所需结构。

### 6.2 setup 与拒绝语义

`make setup-db` 只允许显式创建或初始化空目标数据库，并完成：

1. 校验数据库名和连接角色；
2. 确认没有业务表、未知 schema 或旧 Alembic revision；
3. 应用 M7 baseline；
4. 事务化写入 bootstrap system assets；
5. 运行 final schema、asset、role 和 process readiness probes。

`make migrate-db` 在 M7 首个候选版本中只验证/应用从 M7 baseline 之后产生的未来 revision。遇到旧
`0001`–`0015` revision 或 legacy marker 时返回 `M7_RECREATE_REQUIRED`，不做 DDL。命令输出可以包含公共
revision 名和重建指引，但不能打印数据库 URL、credential、owner map 或私有内容。

## 7. 系统资产 bootstrap 与 source 清理

M7 把当前发布所需的 system Agent、Skill 和 MCP 定义转换为
`backend/app/shared_assets/bootstrap/catalog.json` 及同目录的版本化内容快照。Catalog 每项含稳定 asset key、
kind、版本、内容 digest 和发布状态；secret、credential ciphertext、project binding 和用户自定义内容不能
进入 manifest。

空库 bootstrap 必须：

- 只消费打包进应用的 canonical manifest，不遍历用户目录；
- 使用正式 shared-asset repository 和 caller-owned transaction；
- 为相同 manifest digest 幂等，为不同内容冲突 fail closed；
- 创建 published system versions，但不创建 project binding 或 credential；项目创建后的默认绑定继续由
  final project/shared-asset service 显式建立；
- 输出数量和 digest 摘要，不输出 prompt/Skill 正文或 MCP 参数值。

完成后删除或停止运行时消费：

- shared/per-user Agent filesystem fallback；
- `skills/custom`、legacy skill category 和用户目录 scan；
- `extensions_config.json`、`mcp_config.json` server/skill enablement；
- asset catalog cutover provider 的 pre-cutover 分支；
- file mutation router 和 `ASSET_CATALOG_CUTOVER` 兼容错误。

仓库中的源素材若继续用于构建 manifest，必须有测试证明 Gateway、Worker 和 Scheduler 运行期间不会读取
这些路径。

## 8. Runtime 与配置收口

### 8.1 Gateway

Gateway lifespan 不再创建或暴露：

- legacy `RunManager`；
- configurable memory/JSONL run-event store；
- legacy stream bridge；
- legacy scheduled-task read repository；
- orphan Thread startup migration；
- private-work/Automation legacy-open dependencies；
- asset/private-work/Automation cutover compatibility router。

Gateway 仍为 project API 创建 PostgreSQL session factory、checkpointer/store access、durable stream reader、
quota/audit/recovery dependencies，以及项目连接和 server-to-server channel 所需依赖。Worker 自己构造唯一
Agent execution runtime，不能重新借用 Gateway legacy state。

现有 composer 输入润色保留，但 router 移入 project private-work namespace，使用认证 account 与不可变
`ProjectContext`，要求 `private_work.create` 和 `shared_assets.execute`，并丢弃客户端提供的 owner/project
authority。旧 global permission decorator 不再作为项目授权依据。

### 8.2 配置

最终 `config.yaml` 删除：

- `agents_api`；
- `run_events`；
- `stream_bridge`；
- 仅 legacy connection backend 使用的配置；
- legacy backend/root path discovery 和旧 key alias。

`scheduler`、`worker`、`database`、`quotas`、`recovery`、`channels`、sandbox、models 和最终 runtime 配置保留。
删除字段必须同步更新 example、schema、reload boundary、setup wizard、doctor、support bundle、Docker env 和文档。
任何旧字段都应由顶层精确 tombstone validator 拒绝，而不是因 AppConfig 允许其他扩展字段而被丢弃；
模型、工具和 sandbox 当前允许的非 legacy 扩展字段不因此被整体禁止。

## 9. Automation、Private Work 与 Channel 清理

### 9.1 Automation

保留 project/owner-scoped definition、occurrence、Scheduler、manual trigger、job relation 和 history。删除：

- `ScheduledTaskService` 的 global user-scoped API adapter；
- `LegacyAutomationReadAdapter`；
- `/api/scheduled-tasks*` router；
- `require_legacy_automation_*` guard；
- legacy workspace Automation React page、hooks、API client 和 query keys；
- `AUTOMATION_CUTOVER` 浏览器文案和错误映射。

schedule parser、cron validation、recipes 和表单可以保留为 project Automation 的纯依赖，不能携带 legacy URL
或 global client。

### 9.2 Private work

保留 `/api/projects/{project_id}/private-work*`、project Memory、project Connections、file/artifact authority、
durable run admission/SSE 和 Worker execution。删除所有 global/private compatibility router、global client、
legacy filesystem/Memory/connection source、shared `start_run()` HTTP 路径和 `PRIVATE_WORK_CUTOVER` 兼容错误。

Harness 中仍被 Worker 使用的 Thread state、checkpoint、sandbox 和 agent graph 不是 legacy source，必须保留；
只删除 global ownerless adapter 与 fallback。

project artifact UI 中任何调用 global `/api/skills/install` 的动作必须删除。若用户具备 project Skill 创建能力，
只能通过现有 project shared-asset API 创建明确的项目 Skill 版本；否则只保留 artifact 下载/查看，不猜测
system 或 project scope。

### 9.3 Channels

保留能从认证 connection 或签名 inbound identity 精确解析 account/project/owner/Agent 的 channel 执行链。
删除默认用户、最近项目、唯一项目和 global connection repository fallback。任何无法解析最终 authority 的
inbound event 必须拒绝，不创建 Thread、Run 或 job。

全局 channel status/restart router 同时删除。最终 channel 状态只作为 system-admin operations overview 的
安全 readiness component 暴露，不返回 connection secret、owner、Thread、Run 或 provider payload。

## 10. 备份、恢复与回滚窗口

M7 关闭的是 pre-M7 代码和数据库回滚窗口，不删除 M6 的灾难恢复能力。

- M7 backup manifest 必须绑定新的 baseline revision、schema digest 和 source identity；
- restore 只写入新的空数据库，重放连续 tombstone journal，运行 M1–M7 probes 后写 proof；
- pre-M7 archive 返回 `UNSUPPORTED_ARCHIVE_SCHEMA`，不能自动迁移或 stamp；
- restore/drill 仍不得切换 `DATABASE_URL`、覆盖现有数据库或删除不属于本次随机 target 的数据库；
- 失败恢复只允许修复 M7 代码、从 M7 archive 恢复新库，或重建未上线开发库；不存在重新启用 legacy writer。

## 11. 错误与安全语义

- 被删除 HTTP route 返回普通 `404`，不泄露过去存在的资源或迁移状态。
- 被删除 frontend route 使用 not-found，不发任何 API 请求。
- 旧数据库、旧 archive 和旧 config 分别返回稳定的 operator-facing code；不得包含 SQL、URL 或 secret。
- 项目外与跨 owner 私有访问继续返回 `404`；项目内能力不足继续返回 `403`。
- 所有项目 mutation、job、quota、audit 和 retention 锁序继续遵守 M1–M6 最终契约。
- system_admin 继续不能读取 prompt、消息、Memory、文件、artifact 或 run output 正文。
- 删除兼容分支后，测试 fake 必须位于 tests/support，不得把 production memory/fallback backend 留作测试捷径。

## 12. 实施切片

M7 实施计划按以下独立审查边界拆分：

1. 建立 M7 baseline、空库 setup、旧库拒绝和 final schema readiness。
2. 建立 deterministic system asset bootstrap，移除 file-backed asset authority 和兼容 API。
3. 移除 Gateway legacy runtime、global private-work API、startup migration 和相关配置。
4. 移除 legacy Automation API/read adapter，同时保持 project Scheduler/Worker contract。
5. 收敛 channel authority，删除 global connection fallback。
6. 拆分前端纯组件与 project adapter，删除旧 workspace route/client/fallback。
7. 删除 staged migration CLI、旧 Make/doctor/wizard/runbook 入口，更新 M7 backup/restore schema contract。
8. 建立 M1–M7 PostgreSQL/Frontend/process/recovery gate，运行全量门禁并完成独立关闭审查。

每个切片使用测试驱动开发，先写会失败的 contract，再删除或改造实现。每个切片必须有独立提交和独立审查；
只有最后一项通过后才更新里程碑状态。

## 13. 验收门禁

### 13.1 Fresh PostgreSQL

- 随机空数据库从零应用单一 M7 baseline，0 skip；
- ORM metadata、Alembic schema、constraint、index 和 trigger 精确一致；
- migration/cutover 表和 staged-only 列全部不存在；
- bootstrap system assets 数量、版本、digest、published state 和幂等行为正确；
- 旧 revision/旧 marker/未知非空 schema 在任何 DDL 前被拒绝；
- `make check-db` 只检查最终表、baseline 和 readiness，不引用旧 ledger/marker。

### 13.2 API 与 runtime surface

- OpenAPI 只包含已记录的最终 auth/model/project/admin/channel/webhook surface；旧 URL 全部 `404`；
- Gateway 进程不构造 `RunManager`、legacy stream bridge、legacy scheduler repo 或 memory run-event backend；
- Worker/Scheduler/Gateway 真实多进程 admission、execution、SSE replay、takeover 和 shutdown gate继续通过；
- project/owner isolation、capability、quota、audit、retention 和 channel identity gate继续通过；
- production import graph 和 static scan 不包含 legacy router、guard、source adapter 或被删除配置。

### 13.3 Frontend

- production build 只生成 `/workspace`、project 和 admin 最终页面；旧 workspace route均 not-found；
- project Chats、Memory、Connections、Automation、assets 和 governance 功能通过现有授权与 cache 隔离测试；
- account/project transition 继续先 cancel 再 clear，迟到响应不能污染新 scope；
- bundle/static scan 不包含旧 API URL、旧 workspace fallback、`*_CUTOVER` 文案或 global query key；
- static demo 不注册旧 live route，不请求 live API。

### 13.4 Recovery

- M7 archive 创建、认证、tamper rejection、new-DB restore、tombstone replay、proof 和 drill 全通过；
- pre-M7 archive 被稳定拒绝；
- 随机恢复数据库和敏感临时文件按 M6 authority/inode/fsync 规则清理；
- 测试结束无残留随机数据库、Gateway、Worker 或 Scheduler 进程。

### 13.5 关闭门禁

- Backend 全量测试、Ruff、format、blocking-I/O gate 全绿；
- Frontend 全量测试、lint、type check、production/static build 全绿；
- 固定 M1–M7 PostgreSQL gate 使用真实 PostgreSQL、0 skip；
- `git diff --check`、文档链接、Make help、doctor 和 support bundle smoke 全绿；
- 独立审查最终为 0 Critical/Important；Minor 必须显式记录并证明不影响 M8；
- 状态文档更新为 7/8（87.5%），并明确 M8 未完成、系统仍不可发布。

## 14. 文档与发布状态

M7 实施时同步更新：

- 根、backend、frontend `AGENTS.md`；
- `README.md`、`README_zh.md`、`CHANGELOG.md`；
- `config.example.yaml`、Make help、setup/doctor/support-bundle 文案；
- M6 backup/recovery runbook，使其只描述 M7 baseline archive；
- 总体规格第 19 节和当前完成度。

M4/M5/M6 staged migration runbook 从活跃运维文档移除。历史专项规格和计划保留原始完成证据，但需要在
索引或状态说明中明确它们是 pre-release implementation history，不再是当前操作说明。

## 15. 最终验收摘要

M7 完成时，仓库只有 project-first SaaS 的最终运行路径：一个 PostgreSQL baseline、一套 project/admin API、
一个 project-scoped frontend、独立 Worker/Scheduler、PostgreSQL durable stream/quota/audit 和 M7 格式恢复链。
不存在 legacy runtime、source、route、UI、config、marker 或 rollback switch。

此时总体进度为 7/8（87.5%）。只有 M8 完成完整隔离矩阵、安全审查、容量与运维演练、浏览器验收和发布
关闭审查后，系统才能被描述为可发布的完整多用户 SaaS。

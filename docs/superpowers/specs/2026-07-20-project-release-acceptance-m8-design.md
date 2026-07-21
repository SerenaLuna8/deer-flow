# M8 项目优先 SaaS 宿主机发布验收设计

- 日期：2026-07-20
- 状态：已完成
- 前置里程碑：M1–M7 已完成
- 发布目标：宿主机部署
- 认证浏览器：桌面版 Chromium
- 真实模型：DeepSeek `deepseek-v4-pro`

## 1. 文档目的

本文档定义项目优先、多用户 SaaS 的最后一个里程碑 M8。M8 不新增业务能力，也不建立第二套运行路径；
它在 M1–M7 最终 PostgreSQL baseline、project/admin API、project-scoped frontend、独立
Gateway/Worker/Scheduler、durable SSE、quota、audit 和 version-7 recovery 之上建立可重复的完整发布
验收层。

M8 只有在完整隔离矩阵、安全审查、有限容量正确性、宿主机生产栈、真实网络模型、Chromium、灾难恢复
切换和独立关闭审查全部通过后才能关闭。M8 关闭只证明本文档定义的宿主机部署范围可发布，不证明
Docker Compose、Kubernetes、Helm、Firefox、Safari/WebKit 或第三方模型供应商已通过生产认证。

## 2. 已冻结决策

1. M8 是发布验收里程碑；只有验收失败证明现有实现不满足 M1–M7 契约时才修改产品代码。
2. M8 不增加新的产品功能、角色、资源类型、部署方式、性能子系统或外部基础设施。
3. 唯一认证安装和启动路径是：新 PostgreSQL 数据库 → `make setup-db` → `make start`。
4. Docker Compose、Kubernetes 和 Helm 不参与 M8 实现、演练或阻断门禁，必须标记为未认证部署方式。
5. 桌面版 Chromium 是 V1 唯一认证浏览器；Firefox 和 Safari/WebKit 明确为未认证。
6. 本机真实模型门禁固定使用 `ModelConfig.model == "deepseek-v4-pro"` 的 DeepSeek 配置；逻辑
   `ModelConfig.name` 可以不同，不扩展为多供应商兼容矩阵。
7. 真实模型门禁必须覆盖一次真实对话、持久化流式输出和至少一次真实工具调用。
8. `DEEPSEEK_API_KEY` 只从本机环境或现有 gitignored secret source 读取，不进入代码、YAML、测试参数、
   命令输出、日志、证据、文档或 Git。
9. 普通 CI 不持有模型密钥；真实模型和完整灾备切换只在本机最终验收中阻断发布。
10. M8 不定义吞吐量或延迟 SLO，只验证默认配额和有限并发下的事务正确性。
11. 容量运行时间、RTO 和 RPO 作为事实记录，不构成 V1 性能承诺。
12. 灾备演练必须执行真实备份、备份后墓碑、恢复到新库、服务切换、浏览器复验和回切，不能只运行
    schema probe。
13. 所有演练数据库必须由本次调用创建，并使用受限随机 `deerflow_test_*` 或
    `deerflow_restore_*` 名称；不得连接、覆盖、迁移或删除业务数据库。
14. 所有临时进程、端口、数据库、目录和文件必须具有 invocation ownership；结束时必须证明清理完成。
15. 完整发布验收只接受干净、非 detached 的精确 Git 提交；dirty tree 或提交变化使证据失效。
16. 最终证据必须来自 fresh state 的完整运行；局部重试只能用于诊断，不能拼接为通过结果。
17. PostgreSQL release gate 必须使用真实 PostgreSQL 并保持 0 skip；M8 不允许通过新增 skip、xfail 或
    缩小固定测试清单绕过失败。
18. Python 和 pnpm 依赖漏洞扫描、当前树/发布差异/Git 历史密钥扫描都是阻断门禁。
19. 安全扫描只允许精确、可复现且有测试证明的假阳性排除；不得接受未修复风险作为 M8 关闭条件。
20. 最终独立审查必须达到 0 Critical、0 Important、0 Minor。
21. 证据不得包含 prompt、message、Memory、Run output、用户私有文件名/路径、宿主机敏感绝对路径、附件、
    artifact、credential、数据库 URL、资源 UUID、账户 UUID、原始错误或模型响应正文。
22. M8 不自动 bump version、创建 Git tag、push、发布镜像、发布 Helm chart 或创建 GitHub Release。
23. 每个被认证的精确提交都采用同一入口的一对 fresh 运行：第一次生成 candidate-ready 证据供审查；
    0/0/0 报告绑定精确提交后，第二次携带该报告重跑全部 stage 并生成 final-pass 证据。任何代码、文档或
    stage manifest 变化都使旧报告失效；关闭状态文档形成新提交时，该关闭提交必须重新执行同一对运行。

## 3. 目标与非目标

### 3.1 目标

- 建立一个固定顺序、fail-closed、可重复的 M8 完整发布验收入口。
- 用机器可验证的矩阵证明每一类共享、治理和私有资源的授权边界。
- 证明跨账户、跨项目、跨所有者、角色降级、成员移除和 project lifecycle 变化不会泄漏或产生副作用。
- 证明默认配额边界、有限并发、重复投递、进程崩溃和重启恢复保持事务正确性。
- 证明 fresh setup 后的宿主机 production stack 可以由真实用户通过 Chromium 使用。
- 证明 `deepseek-v4-pro` 可以完成真实 admission、Worker execution、durable stream 和工具调用闭环。
- 证明 M7 archive、external tombstone journal、新数据库 restore、人工 traffic switch 和回切可以完整执行。
- 对依赖、密钥、权限、错误、日志、审计、support bundle 和证据输出完成发布级安全审查。
- 生成绑定精确提交、配置摘要和门禁结果的脱敏证据，并以独立审查关闭 M8。

### 3.2 非目标

- 新增业务页面、API、数据模型、角色、共享方式、计费或性能仪表板。
- 建立 Redis、Kafka、对象存储、外部向量库或新的部署控制面。
- Docker Compose、Kubernetes、Helm、容器镜像或集群容灾认证。
- Firefox、Safari/WebKit、移动浏览器或无障碍认证扩展。
- 多模型供应商、多区域、横向扩容或长时间 soak test。
- 第三方渗透测试、SOC 2、ISO 27001、等保或其他合规认证。
- 自动版本发布、tag、push、制品上传或流量切换到真实业务数据库。

## 4. 发布验收总体架构

M8 在最终产品路径外增加四层验收，不允许创建 production fallback：

```text
Static contracts and security
            |
Deterministic M1-M8 gates
            |
Host production + Chromium + DeepSeek
            |
Backup/restore traffic switch + independent review
```

### 4.1 静态契约与安全层

该层验证 final API、repository、database constraint、frontend query ownership、configuration、active docs
和 source absence，并执行依赖与密钥扫描。它不启动模型调用，不读取 secret value，也不改变数据库。

### 4.2 确定性测试层

该层运行固定的 M1–M7 release gate 和新增 M8 isolation、capacity、fault、security contract。测试使用真实
随机 PostgreSQL 数据库，但外部模型采用测试 double；每一项必须可在 CI 和本机重复运行。

### 4.3 本机真实发布层

该层从新数据库执行最终 setup/start 路径，启动真实 Nginx、Gateway、Worker、可选 Scheduler 和 Frontend，
再由 Chromium 的多个独立 browser context 验证认证、项目边界、私有工作和真实 DeepSeek 运行。模型响应只
做结构、状态、游标和持久化断言，正文在内存中检查后立即丢弃。

### 4.4 灾备与独立审查层

该层对测试数据执行 archive、post-backup tombstone、new-database restore、temporary traffic switch、
revalidation 和回切。完整运行通过后冻结精确提交，独立审查 M8 spec、实现、门禁、证据和完整分支差异。

## 5. 统一入口与运行身份

根 `Makefile` 提供 `make release-acceptance` 作为唯一完整 M8 关闭入口。它调用一个 repo-owned orchestrator，
按固定 stage manifest 执行预检、确定性门禁、真实发布、灾备、清理、证据和审查前检查。单项命令可以用于
开发诊断，但不能生成最终通过证明。

每次运行在开始时固定：

- `acceptance_run_id`：随机、非业务 UUID；
- exact Git commit 和 clean-tree proof；
- M8 stage manifest digest；
- public configuration digest，只包含非敏感字段和 secret-presence boolean；
- Python、Node.js、pnpm、uv、PostgreSQL、Nginx 和 Chromium 版本；
- operating system、CPU architecture 和开始时间；
- invocation-owned database、process、port 和 temporary-path ledger。

完整入口要求显式 `M8_LIVE_ACCEPTANCE=1`，避免普通 `make test` 意外调用付费模型或执行灾备。缺少该开关、
`DEEPSEEK_API_KEY`、能够唯一解析到 `ModelConfig.model == "deepseek-v4-pro"` 的 DeepSeek 配置、数据库管理
权限或 Chromium 时必须在创建数据库和启动进程前失败，并只返回缺失项名称。

当前用户配置必须升级到 repository current config version 且可被 final M7 schema 加载。已删除字段、旧
revision、未知非空数据库或不安全 runtime role 都必须在任何验收副作用前失败。

第一次不携带 review report 的完整运行成功后只产生 `candidate_ready` 状态，不能关闭 M8。独立审查报告
使用 closed schema，绑定 candidate commit、stage manifest digest、candidate evidence digest、review base、
review range 和 0/0/0 verdict。随后设置 `M8_REVIEW_REPORT` 再次执行同一 `make release-acceptance`；预检先
验证报告、当前提交和 manifest 完全匹配，然后从 fresh state 重跑全部 stage。只有第二次运行成功才能产生
`final_pass`。存在 finding、提交变化、报告字段缺失或 digest 不匹配时都必须拒绝 final run。

首次 final-pass 允许维护者把 active status 和 closure summary 更新为 M8 完成；该文档提交会改变 Git identity，
因此旧 final-pass 不能证明新的 HEAD。关闭提交必须再次生成 candidate-ready、接受覆盖完整范围的新 0/0/0
报告，并从 fresh state 生成最终 final-pass。最终运行后不得再修改 tracked file。

## 6. 完整隔离矩阵

### 6.1 权威清单

`contracts/m8_isolation_matrix.json` 是 M8 隔离 coverage 的机器可读权威清单。每个 case 至少包含：

- stable `case_id`；
- actor class、account relationship、project relationship、membership state 和 platform role；
- resource family、scope 和 ownership；
- operation；
- expected public status/error code；
- expected database side-effect count；
- one or more executable evidence selectors；
- backend/API/frontend/database coverage layer。

contract test 必须证明 schema 合法、case ID 唯一、所有 frozen actor/resource/operation family 都已覆盖、
每个 evidence selector 可收集且不是 skip/xfail。新增或删除 project/private/admin route、repository public
method 或 scoped frontend client operation 时，matrix drift gate 必须失败，直到清单和证据同步更新。

### 6.2 主体维度

- 未认证请求；
- 当前账户的项目外用户；
- 同项目 Admin、Editor、Runner 和 Viewer；
- 同项目不同 owner；
- 同一用户的另一个项目；
- 另一个 account；
- 被移除、已退出或 membership version 过期的用户；
- active、pending-deletion、suspended project 下的成员；
- `system_admin` 无项目成员关系、同时有成员关系以及普通 `user` 平台角色。

### 6.3 资源维度

- account session、workspace project discovery 和 invitation redemption；
- project、membership、invite、role、lifecycle 和 deletion recovery；
- system/project Agent、Skill、MCP、version、binding、Credential status/grant；
- Thread、Message、Run、RunEvent、checkpoint、upload、file、artifact；
- Memory、Connection、Automation、occurrence 和 result；
- Job、attempt、dead projection、quota、usage ledger、audit 和 retention metadata；
- admin asset/operation readiness 和 channel/webhook admitted authority；
- archive、journal、restore proof 和 public recovery status。

### 6.4 操作维度

创建、列表、搜索、分页、读取、导出、更新、删除、发布、绑定、审批、执行、停止、流式读取、
`Last-Event-ID` 续传、手动/自动 admission、重试、requeue、恢复和 purge 都必须覆盖。矩阵同时验证：

- 客户端伪造 `project_id`、`owner_user_id`、role、capability、membership version、snapshot、Credential、
  grant、job、lease 或 internal runtime field 不形成 authority；
- 跨项目、跨 owner、项目外和不存在资源统一返回 public 404；
- 当前项目内能力不足返回 403；
- 版本、最后一名 Admin 和并发冲突返回稳定 409；
- 配额返回 429；临时 database/Worker unavailable 返回 503；
- repository predicate 和 child relationship 同时绑定完整 scope；复合外键拒绝错误关联；
- update/delete/export/search/pagination 与 get/create 使用同一隔离规则；
- account/project transition 先 cancel、invalidate generation、clear cache，再建立新 client；旧响应不能写入
  新 scope；
- `system_admin` 只能读取 bounded governance metadata，不能读取任意 private content。

## 7. 有限容量与并发正确性

M8 不进行 benchmark，也不以吞吐或 p95 latency 关闭里程碑。它验证总体设计冻结的默认上限和最小并发：

| 维度 | 验收方式 | 预期结果 |
| --- | --- | --- |
| 并发 Run | 同项目保留 3 个 active reservation，再提交第 4 个 | 前 3 个成功，第 4 个稳定 429；释放后可以再次 admission |
| 项目成员 | 创建 20 个 active membership，再兑换第 21 个 invite | 第 20 个有效，第 21 个无副作用拒绝 |
| 单文件 | 在 100 MiB 边界执行真实 streaming upload；再提交超出 1 byte 的输入 | 边界值成功，超限输入在持久化前拒绝 |
| 项目存储 | 通过受控 ledger/counter 状态定位 5 GiB 边界 | 不实际写入 5 GiB；超限写入无 file/chunk/usage 残留 |
| MCP 日配额 | 预置到 9,999/10,000 的 authoritative counter | 第 10,000 次结算正确，下一次无外部调用并返回 429 |
| Job 至少一次 | 重复 delivery、lease expiry、Worker crash/takeover | 副作用幂等、旧 lease 不能 append/settle、恰好一个 terminal outcome |
| SSE | Gateway restart、重复 cursor、跨 scope cursor | 单调续传、去重、无跨账户/项目/owner frame |
| 治理竞争 | invite 竞争、last Admin 降级/退出、project lifecycle 竞争 | 一个权威结果、稳定冲突语义、无 partial commit |

所有容量用例有明确 wall-clock timeout 防止挂死；测得持续时间写入证据，但不存在通过时长阈值。

## 8. 安全验收

### 8.1 威胁模型复核

M8 更新 M1 threat inventory，使其只描述 final M7 架构。每个威胁必须映射到 prevention control、detective
control、executable test 和 evidence case，至少覆盖：

- identifier guessing、mass assignment、IDOR、跨 scope search/page/export；
- stale membership、TOCTOU、lease theft、duplicate delivery 和 runtime snapshot substitution；
- Credential envelope、grant/slot confusion、secret in prompt/checkpoint/event/log/audit/cache；
- CSRF、cookie flags、open redirect、unauthenticated admin/project route 和 error detail；
- file path traversal、symlink、archive traversal、temporary file identity race 和 sandbox escape boundary；
- SSE cursor injection、cross-scope replay、terminal duplication 和 Gateway restart；
- Scheduler singleton ownership、manual/automatic admission race 和 revoked automation principal；
- backup tamper、wrong key、journal gap/reorder/source mismatch、restore proof substitution 和 deleted-data revival；
- system-admin privilege confusion、support bundle leakage 和 readiness metadata enumeration。

### 8.2 依赖漏洞

Backend 使用由 `uv.lock` 固定的 Python dependency auditor；Frontend 使用 lockfile-frozen production
dependency audit。扫描器版本本身必须由 repository lockfile 或 checksum 固定。漏洞数据库可以在验收时
联网更新，但结果必须记录 advisory identifier、affected locked version 和 scan database timestamp，不能记录
private registry credential。

任何实际影响 release dependency graph 的 advisory 都阻断发布。排除只允许证明 package/version 不在
resolved production graph 或 advisory 不适用于实际平台/代码路径；排除必须是精确 advisory ID、带回归测试
和删除条件，不能使用 wildcard、severity downgrade 或永久 accepted-risk 标记。

### 8.3 密钥与敏感信息扫描

由 lockfile/checksum 固定的 scanner 覆盖：

- 当前 tracked tree；
- M8 review base 到候选提交的完整 diff；
- 可达 Git 历史；
- generated release evidence、support bundle 和测试日志。

允许项仅限已登记的固定测试假值或文档占位符，并按 exact path、rule 和 digest 匹配。发现真实或无法证明为
假的 credential 时立即失败、停止外部调用、轮换 credential、清除生成物并重新从 fresh commit 运行。
Git 历史命中在完成轮换和独立处置前持续阻断 M8；acceptance orchestrator 不得自动重写 Git 历史。

### 8.4 独立审查

独立审查使用冻结的 base、candidate commit 和完整 diff，按 Critical、Important、Minor 分类。每个有效发现
先加入失败测试，再最小修复并重跑 affected gate；随后必须为新提交重跑 candidate acceptance 并重新审查。
最终 candidate 必须获得 0/0/0，且 review report 记录精确范围、修复提交和最终 verdict。0/0/0 报告生成后，
必须携带该报告再从 fresh state 运行完整 `make release-acceptance`，才能封存 final-pass evidence。

## 9. 宿主机真实发布验收

### 9.1 Fresh install

orchestrator 使用 maintenance PostgreSQL authority 创建 invocation-owned source database，运行
`make setup-db` 和 `make check-db`，再通过 `make start` 的最终 production 路径启动 Nginx、Gateway、Worker、
Frontend 和按配置启用的 Scheduler。不得调用开发服务器、内存 backend、旧 route 或 test-only execution
fallback。

预检和启动必须证明：

- application role 非 superuser；
- schema 是唯一 `0001_project_saas_baseline`；
- packaged system catalog digest 正确；
- Gateway、Worker 和 Scheduler 边界与 readiness 正确；
- Nginx `2026` 是浏览器唯一入口，浏览器 API 使用 same-origin `/api/*`；
- config version 是仓库 current version，removed key 被拒绝而不是忽略。

### 9.2 Chromium 用户旅程

多个隔离 browser context 至少覆盖：

- 注册/登录、workspace、多项目发现和 project enter；
- Admin、Editor、Runner、Viewer 的 server-issued capability navigation；
- 同项目不同 owner 和跨项目私有 Thread/File/Memory/Automation 不可枚举；
- account/project 切换时缓存、mutation 和 reconnect state 清理；
- shared Agent/Skill/MCP 可见性、固定 version 和 Credential safe status；
- Viewer 现有私有数据读取/导出/允许删除与 create/run denial；
- system-admin admin route 与普通用户 not-found；
- Gateway restart 后 `Last-Event-ID` durable stream continuation。

浏览器只保存用于断言的 bounded public state；trace、screenshot、video 和 failure dump 在写盘前经过 M8
redaction policy。包含模型正文、私有名称、token、cookie 或 URL query secret 的原始浏览器 artifact 不能进入
最终证据。

### 9.3 DeepSeek 实时闭环

live case 选择唯一一个 `ModelConfig.model == "deepseek-v4-pro"` 的 DeepSeek 配置，并把它的逻辑
`ModelConfig.name` 固定到测试 Agent snapshot，再完成：

1. 从 Chromium 在 project Thread 提交一个不含真实隐私数据的固定 synthetic prompt；
2. Gateway admission 后由 Worker 执行，浏览器收到多个持久化 stream frame；
3. 模型调用一个无 secret、无外部副作用、结果有界的已批准测试工具；
4. Run 产生唯一 terminal outcome，刷新页面和 Gateway restart 后仍可续传/读取；
5. 另一 account/project/owner 不能读取该 Thread、Run、frame 或 tool result。

验收只记录 provider、logical model name、provider model ID、HTTP/run outcome、frame count、tool-call count、
cursor 和耗时的脱敏摘要。
prompt、completion、reasoning、tool arguments/result body 和 provider raw response 均不得持久化到 M8 证据。

## 10. 完整灾难恢复切换演练

演练使用本次 invocation 的 source database、restore database、archive directory、journal 和 proof directory：

1. 在 source 创建两个 account、两个 project、同项目两个 owner、共享资产、private work、Automation 和
   已完成 live Run，记录只含 domain-separated digest 的 expected inventory。
2. 创建 authenticated encrypted version-7 archive，固定 revision、schema digest、source identity、archive
   high-watermark 和 journal anchor。
3. archive 后执行一次真实 private retention purge；journal append、fsync 和 database deletion 使用现有
   authoritative order。另写一项非删除的 post-backup synthetic row，明确证明 RPO 停留在 archive point。
4. 停止测试 production stack，记录恢复计时起点；source database 保留但不再被测试服务访问。
5. 将 archive 恢复到 distinct nonexistent `deerflow_restore_*` database，重放连续 tombstone suffix，运行
   exact schema、catalog、LangGraph 和 private/project probes，并写 restore proof。
6. 仅通过 invocation-owned 临时环境覆盖把测试 production stack 指向 restore database；不得改写用户
   `config.yaml`、`.env` 或 shell profile。
7. 启动恢复栈，通过 Chromium 验证登录、project、shared asset、pre-backup private work 和 live Run 可用；
   已 purge 数据不复活，post-backup 非删除 row 不出现，跨 scope 隔离继续成立。
8. 停止恢复栈，切回 source database 并再次通过 health、login 和 project probe，证明回切路径可用。
9. 运行 cleanup verifier 后，只删除 invocation-owned source/restore database、archive、journal、proof、临时
   config、进程和端口。任何无法证明 ownership 的对象保持原状并使验收失败。

RTO 从 source stack 停止完成计时到 restore stack 通过 browser recovery probe；RPO 记录 archive
high-watermark 和 post-backup row 的预期缺失。M8 不设置时间阈值，但缺少计时、恢复点证据或 cleanup proof
都视为失败。

## 11. 证据模型与脱敏

本地原始输出位于新增的 gitignored `.release-evidence/<acceptance_run_id>/`。candidate 和 final manifest 使用
同一 closed schema，包含：

- run identity、exact commit、manifest/config/toolchain digest；
- 每个 stage 的 command identifier、开始/结束时间、status、pass/fail/skip count 和 bounded summary；
- isolation matrix coverage count 和 uncovered count；
- dependency/secret scan database timestamp、有效 finding count 和 exact exclusion IDs；
- live model provider/model、frame/tool/terminal count 和 duration；
- archive/recovery public proof digest、RTO、RPO 和 cleanup result；
- review status；`candidate_ready` 为 `awaiting_review`，`final_pass` 必须包含 independent review range、
  candidate evidence digest 和 Critical/Important/Minor counts；
- 每个 evidence file 的 SHA-256 和 top-level manifest SHA-256。

schema 拒绝任意额外字段，尤其是 `prompt`、`message`、`memory`、`output`、`content`、`payload`、`exception`、
`database_url`、`owner_id`、`thread_id`、任何业务 Run identifier、`credential`、`token`、`cookie`、`nonce`、
`ciphertext`、`locator` 或 raw path。失败阶段也必须使用同一脱敏 writer，不能直接复制 stdout/stderr。

仓库只提交人工可读的 M8 closure summary 和审查结论，不提交本地证据目录。summary 引用允许状态更新的
pre-closure certified commit、manifest digest、命令、计数和 verdict，但不包含 invocation locator 或 private
data。关闭提交本身的 exact commit 和 evidence digest 由 post-closure final-pass manifest 记录；tracked Git
内容不能包含自身尚未形成的 commit hash。该 manifest 生成后不得再修改 tracked file。

## 12. 失败、取消与清理语义

- 任一 stage 失败即把完整运行标记为 failed；后续业务 stage 不再执行，只进入清理和脱敏失败报告。
- SIGINT、SIGTERM、测试 timeout 和子进程异常使用相同 cleanup path；orchestrator 等待 bounded graceful
  shutdown，再记录未退出资源并失败。
- cleanup 以启动时 ledger 的 exact PID、process start identity、port、database name/owner 和 inode 为准；
  不使用 broad process match、glob、repo-root deletion 或 unresolved environment variable。
- database cleanup 必须重新验证随机名称、当前连接、database owner 和 invocation marker；验证失败时不得
  `DROP`，而是报告 quarantine-required failure。
- secret 或 private content 检测发生后，证据 writer 只记录 rule ID 和发现位置的 domain-separated digest；
  不复述命中内容。
- 修复必须遵循 TDD：先保留会失败的 focused case，再做最小 final-path change，运行 affected gate，最后从
  fresh state 重跑完整验收。
- final run 不允许 `--resume`、复用旧数据库、复用旧浏览器 trace、复用旧 archive 或合并多个 commit 的
  stage result。

## 13. CI 与本机职责

普通 CI 增加 M8 deterministic subset，至少包含：

- matrix schema/coverage/drift contract；
- M1–M8 fixed PostgreSQL gate，真实 PostgreSQL、0 skip；
- backend full/unit/blocking-I/O、Ruff 和 format；
- frontend unit/check、production/static build 和 deterministic Chromium E2E；
- threat/control mapping、dependency audit、tracked-tree/review-diff secret scan；
- evidence schema/redaction、orchestrator preflight/failure/cleanup tests。

Git 历史扫描可以在完整 checkout 的 CI 或本机运行，但最终 M8 本机验收必须再次执行。CI 不运行
`deepseek-v4-pro`、不持有 `DEEPSEEK_API_KEY`、不执行完整 traffic switch，也不能单独关闭 M8。

本机完整验收运行所有 CI subset，并额外执行：

- fresh host production setup/start；
- multiple-context Chromium journey；
- real DeepSeek stream/tool case；
- complete archive/tombstone/restore/switch/back-switch drill；
- residual resource audit 和完整 evidence manifest。

## 14. 实施边界

M8 实施按以下责任边界拆分，详细文件和 RED/GREEN 命令由后续实施计划固定：

1. acceptance contracts、matrix schema、stage manifest 和 evidence schema；
2. isolation matrix 的缺口测试与仅由失败驱动的修复；
3. capacity/concurrency/fault gates；
4. dependency、secret、threat-control 和 redaction security gates；
5. host orchestrator、preflight、ownership ledger、cleanup 和 failure semantics；
6. Chromium 多账户 deterministic/live journeys；
7. DeepSeek `deepseek-v4-pro` live case；
8. full recovery switch/back-switch drill；
9. CI deterministic subset、root Make target 和 operator runbook；
10. fresh full gates、independent review、repairs 和 closure docs。

每个切片必须先建立失败 contract，再实现最小行为、运行 affected regression、独立审查并提交。产品修复不能
扩大冻结范围；发现新功能需求时保留为 M8 后 backlog，不能混入发布验收。

## 15. 完整验收门禁

M8 关闭前必须同时满足：

- 实现提交先由不带 review report 的 `make release-acceptance` 从 fresh state 产生 `candidate_ready`，独立审查
  0/0/0 后由带匹配 `M8_REVIEW_REPORT` 的第二次 fresh 运行产生 `final_pass`；状态文档提交后，必须在精确
  clean closure commit 上重新执行同一 candidate/review/final 对，最终运行后不得有 tracked change；
- fixed M1–M8 PostgreSQL gate 真实运行且 0 skip；
- backend full、blocking-I/O、Ruff、format 全绿；
- frontend full unit、check、production/static build、deterministic/live Chromium 全绿；
- isolation matrix uncovered count 为 0，所有 evidence selector 已实际收集；
- 默认 quota、有限并发、Worker crash、Gateway restart、Scheduler ownership 和治理竞争全绿；
- Python/pnpm dependency audit 无有效 finding；secret scan 无未解释命中；
- `deepseek-v4-pro` real conversation、durable stream、tool call 和 cross-scope denial 全绿；
- version-7 archive、post-backup tombstone、new-DB restore、traffic switch、browser probe、back-switch 全绿；
- cleanup verifier 报告 0 residual process、port、database 和 sensitive file；
- doctor、check-db、support bundle、active-doc link/source consistency、Make help 和 `git diff --check` 全绿；
- independent final review 为 0 Critical、0 Important、0 Minor；
- closure summary、root/backend/frontend `AGENTS.md`、README、CHANGELOG、operation runbook 和总体设计一致。

任何门禁缺失、跳过、失败、证据与 commit 不一致、清理不完整或 review finding 未关闭时，M8 保持未完成，
系统不得描述为可发布。

## 16. 文档与发布状态

M8 实施完成时同步更新：

- 根、backend、frontend `AGENTS.md`；
- `README.md` 和 `CHANGELOG.md`；
- `docs/superpowers/specs/2026-07-12-project-first-saas-design.md`；
- 本专项规格和后续 M8 实施计划；
- 新的宿主机发布验收与灾备切换 operator runbook；
- CI、Make help 和 release command reference。

状态只能在所有门禁和最终独立审查通过后更新为 M1–M8 `8/8（100%）`。发布文字必须使用精确范围：

> DeerFlow 项目优先、多用户 SaaS V1 已通过宿主机部署发布验收；认证路径为新 PostgreSQL 数据库、
> `make setup-db`、`make start` 和桌面版 Chromium。

同一处必须说明 Docker Compose、Kubernetes/Helm、Firefox、Safari/WebKit 和其他模型供应商没有经过 M8
生产认证。M8 完成不代表已经创建版本 tag 或对外发布制品。

## 17. 最终验收摘要

M8 完成时，DeerFlow 必须以可重复证据证明：项目和 owner 隔离矩阵完整；角色、平台治理、Credential、
运行、任务、流、配额、审计和恢复边界在有限并发与故障下保持正确；fresh PostgreSQL 宿主机生产栈可以
由 Chromium 使用 `deepseek-v4-pro` 完成真实端到端工作；authenticated archive 与 external tombstone 可以
恢复到新数据库并安全切换；依赖、密钥和独立安全审查无有效发现。

只有满足上述全部条件，项目优先、多用户 SaaS 的总体里程碑才从 7/8 更新为 8/8，并在限定的宿主机部署
范围内标记为可发布。

## 18. 关闭记录

2026-07-21，关闭前实现提交 `896fe62ec4265a343ab6a6d209453d11508d81a0` 完成 fresh
candidate、完整 `3f574b89..HEAD` 审查和 fresh final。固定 stage manifest digest 为
`dcda2974d83e9c3ed336e8099e2fc74b219f8fb000044c47a1430327fabe8312`，审查结论为
0 Critical / 0 Important / 0 Minor。

关闭前 final 的脱敏事实如下：

- M1–M8 PostgreSQL gate：326 passed、0 failed、0 skipped；
- backend full：6867 passed、0 failed、940 个由专用 live/PostgreSQL 阶段覆盖的 expected skip；
- frontend unit：893 passed；完整 Playwright：79 passed、0 failed、0 skipped、0 flaky；
- isolation matrix：142 个 case、29 个 selector、0 uncovered；
- Python/pnpm dependency audit：分别扫描 202/694 个 package，0 effective finding；
- DeepSeek `deepseek-v4-pro`：真实 durable stream 完成，2 次工具调用、1 个 terminal；
- version-7 recovery：恢复 4 项、重放 1 个 tombstone，RPO 为 `archive_point_confirmed`，事实 RTO 为
  30154 ms；
- cleanup：process、port、database、path residual 均为 0。

因此 M1–M8 总体进度更新为 8/8（100%）。认证范围仍严格限定为全新 PostgreSQL、
`make setup-db`、`make start`、桌面版 Chromium 和 DeepSeek `deepseek-v4-pro`。Docker Compose、
Kubernetes/Helm、Firefox、Safari/WebKit 和其他模型供应商未经过 M8 生产认证。本次关闭没有创建
版本 tag、推送远端或发布镜像/Chart；关闭文档提交仍必须按第 15 节重新执行 authoritative
candidate/review/final。

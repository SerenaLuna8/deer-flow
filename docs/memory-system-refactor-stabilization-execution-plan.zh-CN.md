# Memory v2 稳定化与个性化执行计划

- 日期：2026-08-05
- 状态：执行中（PR9 代码与离线测试完成；当前阶段 PR10；真实环境终验待完成）
- 前置提交：3024d664
- 基线分支：codex/memory-system-refactor
- 上游计划：[记忆系统重构执行计划](./memory-system-refactor-execution-plan.zh-CN.md)

## 1. 目标和固定顺序

PR1 至 PR8 已完成 Memory v2 主链路。真实环境长时间对话证明提取、定时整理、
跨 Thread 召回和页面管理可以运行，同时暴露了必须收口的质量、冷启动和 checkpoint 问题。

后续固定拆为五个独立 PR：

| PR | 目标 | 是否改 Schema |
|---|---|---|
| PR9 | 修复自动整理质量与输出兼容 | 否 |
| PR10 | 修复首次 v2 召回超时 | 否 |
| PR11 | 修复 /compact 后沿用旧 checkpoint | 否 |
| PR12 | 增加真正的 /Dream 手动立即整理 | 否 |
| PR13 | 增加个性化记忆启用与重置 | 是，full_schema_v3 |

依赖顺序：

~~~text
PR9 → PR10 → PR11 → PR12 → PR13 → 最终真实环境复验
~~~

不得把多个阶段长期堆在同一个未提交工作区中。

## 2. 已确认的问题

以下结果来自真实 Gateway、Worker、Scheduler、PostgreSQL 和浏览器链路。

| 问题 | 真实证据 | 影响 |
|---|---|---|
| 自动整理质量 | 成功批次把 ephemeral 临时故障、含糊的“按这个版本冻结”和角色扮演中的 CTO 临时采购权限写成正式 Fact | 错误事实进入召回 |
| 整理输出稳定性 | 一批 10 条 Candidate 连续三次 MEMORY_CONSOLIDATE_OUTPUT_INVALID 后 dead，且无半写 | 整批 backlog 无法处理 |
| 首次召回 | 冷进程首次加载 cl100k_base 时尝试联网下载 BPE，外层 5 秒超时终止 Run | 第一次跨 Thread 使用可能失败 |
| /compact | API 返回新 checkpoint 后，不刷新页面立即发送仍携带 SDK 内部旧 checkpoint | 新摘要未被下一条消息使用 |
| /Dream | 当前没有手动长期记忆整理命令 | 用户无法立即处理 pending Candidate |
| 个性化设置 | 当前只有平台全局 Memory Policy，没有账号级启用开关和全部重置入口 | 用户无法控制自己的记忆 |

## 3. 执行纪律和边界

### 3.1 每个 PR 的步骤

1. 先增加能稳定复现问题的失败测试；
2. 只实现本 PR 列出的最小改动；
3. 跑聚焦测试和受影响模块完整门禁；
4. 检查 diff 和真实环境退出标准；
5. 独立提交后再进入下一 PR。

### 3.2 当前工作区先单独收口

当前工作区已有一项真实测试期间发现的独立前端修复：

- frontend/src/core/shared-assets/hooks.ts
- frontend/tests/unit/core/shared-assets/hooks.test.ts

开始 PR9 前先完成验证并单独提交。它不能混入 PR9。

### 3.3 不擅自扩展

- PR9 至 PR12 不增加表、迁移、服务或 Job 类型；
- 不增加向量库、Redis、Kafka、角色状态机或语义规则引擎；
- 不把 Thread Context、Thread 摘要或 assistant/tool 内容喂给 Extractor；
- 不重做 LangGraph checkpoint、摘要算法或后端 compact CAS；
- 不增加 tiktoken 网络预热线程；
- /Dream 不同步运行模型、不重新扫描对话、不创建 private Run；
- PR13 只增加保存个人记忆开关所需的最小 Schema，不扩展为通用偏好平台；
- 基本权限和并发正确性必须保留，但不为假设性威胁建设新安全体系。

## 4. PR9：自动整理质量与输出兼容

### 4.1 目标

1. ephemeral、角色扮演、模拟、假设和仅当前任务有效的信息不能自动改变正式记忆；
2. 脱离原句后无法确定主体、范围或值的 Candidate 不能自动生效；
3. 常见但语义等价的 JSON 外层格式不能导致整批 Candidate 三次失败。

### 4.2 先写失败测试

扩展：

- backend/tests/fixtures/memory_extractor_checkpoint_a.jsonl
- backend/tests/test_memory_checkpoint_a_eval.py
- backend/tests/test_memory_pr5_consolidator.py
- backend/tests/test_memory_pr5_worker.py

固定样例必须包含：

- 角色扮演中的 CTO 临时采购授权，期望不提取；
- 假设客户画像和临时故障，期望不提取；
- “技术范围先按这个版本冻结”，期望不提取；
- “这是长期约定，不仅限于本次”，期望保留；
- ephemeral + create、confirm、revise 均不能正式写入；
- 低 Extractor confidence 不能被高 Consolidator confidence 覆盖；
- nullable 字段缺省和单层 JSON code fence 能被安全解析。

### 4.3 最小实现

#### Extractor

修改：

- backend/packages/harness/deerflow/agents/memory/extractor.py
- backend/app/private_work/memory_source_admission.py

规则：

- 角色扮演、模拟、假设、示例人物、第三方属性和本轮授权不生成长期 Candidate；
- Candidate 必须脱离当前对话仍能确定主体、范围和值；
- 明确说明长期有效的信息仍允许提取；
- prompt version 从 memory-extract-prompt-v2 升为 v3；
- 不增加中英文关键词硬拦截。

#### Consolidator

修改：

- backend/packages/harness/deerflow/agents/memory/consolidator.py
- backend/app/worker/memory_consolidate.py

实现：

- MemoryConsolidationCandidateInput 增加并序列化现有 retention_class；
- create、confirm、revise 必须是可独立解释的稳定事实；
- 角色扮演、假设、当前任务限定或含糊内容只能 pending；
- retention_class 为 ephemeral 时，create、confirm、revise 一律降为
  pending / insufficient_evidence；
- create、confirm、revise 都要求 Candidate confidence 达到现有 policy threshold；
- create、revise 还要求 decision confidence 达到同一 threshold。

#### 输出兼容

继续保留 extra-forbid、动作字段组合、完整 Candidate 集和 target Fact ID 校验，只增加：

- nullable 字段默认值 None；
- 使用 deerflow.utils.llm_text.strip_markdown_code_fence 去除单层 JSON fence；
- prompt 使用真正的 JSON null 和分 action 示例；
- prompt、consolidator implementation、output contract 统一升为 v2。

不增加第二次 repair LLM，不记录模型原文，不吞掉真正的语义错误。

### 4.4 退出标准

- 角色扮演、临时、假设和含糊样例产生 0 个 active Fact；
- ephemeral + confirm 也不能刷新 Fact 或新增 Evidence；
- 明确 durable/permanent 正例仍能 create、confirm、revise；
- 单层 fence 和缺省 nullable 字段可正常解析；
- 缺少 Candidate、非法动作组合、额外字段、未知 Fact 继续严格失败；
- 固定 Extractor 数据集仍达到既有 precision/recall 门槛；
- 真实环境至少完成 20 条和 10 条两批整理，OUTPUT_INVALID 为 0，dead Job 为 0。

### 4.5 验证

~~~bash
cd backend
uv run pytest \
  tests/test_memory_checkpoint_a_eval.py \
  tests/test_memory_pr5_consolidator.py \
  tests/test_memory_pr5_worker.py
~~~

聚焦测试通过后运行后端完整核心门禁和随机 deerflow_test_* PostgreSQL 用例，必须 0 skip。

现有开发库中的错误 Fact 和 dead v1 generation 只作为证据，不写兼容迁移。PR9 完成后按
仓库支持的空库生命周期重建本地开发库，再走真实 Gateway → Worker → Scheduler 重放样例。

## 5. PR10：首次 v2 召回不依赖网络

### 5.1 目标

冷 Worker 的第一个新 Thread 必须一次成功读取 Memory。即使 authority 读取超过五秒，
Run 也要安全降级继续，不能直接进入 dead。

### 5.2 先写失败测试

修改或新增：

- backend/tests/test_memory_pr2_contract.py
- backend/tests/test_memory_pr7_runtime.py
- backend/tests/test_memory_prompt.py

覆盖：

- MemoryConfig 和数据库 Runtime Policy 默认均为 char；
- char 模式完全不调用 tiktoken.get_encoding；
- v2 load_snapshot 超时不向外抛 TimeoutError；
- 超时时旧 checkpoint Memory 被移除，本轮以无长期记忆继续；
- AuthorizationRevoked 和 CancelledError 仍传播。

### 5.3 最小实现

修改：

- backend/packages/harness/deerflow/config/memory_config.py
- backend/app/system_runtime_settings/models.py
- backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py

实现：

1. MemoryConfig 和 Runtime Policy 的默认 token_counting 改为 char；
2. 当前开发库已落库的 policy 也显式改为 char；
3. v2 abefore_model 只捕获 TimeoutError，记录不含正文的 warning，并调用
   _reconcile_v2_snapshot(state, None)；
4. 保留现有五秒上限。

不增加 tiktoken 后台预热，也不增大超时掩盖问题。

### 5.4 退出标准

- 隔离 tiktoken cache 且网络不可用时，第一个新 Run 一次成功；
- 正常 char 路径生成一条 run_memory_context_snapshots；
- rendered_content 含预期 Fact；
- 同 Run retry 的 snapshot ID 和 digest 不变；
- 人为慢 authority 时 Run 继续且不带旧 Memory；
- v1、hard forget overlay 和 memory_search 语义不变。

## 6. PR11：/compact 后从服务端最新 checkpoint 继续

### 6.1 根因

后端 manual compaction 已正确提交摘要和新 checkpoint。问题是 React Query invalidate
不会更新 LangGraph SDK 内部 branchContext.threadHead.checkpoint，下一次普通 submit
仍隐式携带旧值。

PR11 不修改后端 compact API、摘要 prompt、CAS 或数据库。

### 6.2 先写失败测试

修改或新增：

- frontend/tests/unit/core/threads/send-message.test.ts
- frontend/tests/unit/components/workspace/input-box-helpers.test.ts
- frontend/tests/e2e/project-routes.spec.ts

覆盖：

- 普通发送不显式覆盖 checkpoint；
- compacted=true 后下一次普通发送传 checkpoint: null；
- 发送成功后一次性状态被消费；
- 发送失败时状态保留；
- Thread 切换时清除旧状态；
- compact 成功或 skipped 后清空输入和草稿，失败时保留命令。

### 6.3 最小实现

修改：

- frontend/src/components/workspace/input-box.tsx
- frontend/src/components/workspace/input-box-helpers.ts
- frontend/src/core/threads/hooks.ts

实现：

1. InputBox 增加 Thread-scoped 一次性 continueFromLatestCheckpoint；
2. 只有 compacted=true 设置状态；
3. HTTP 成功后显式清空 text input、session draft、draft timer 和 followups；
4. 下一次普通发送把状态传入 threads/hooks.ts；
5. 状态存在时 thread.submit 传 checkpoint: null，让 Gateway 从权威当前 head 继续；
6. 发送成功后清除，失败保留，Thread 切换清除；
7. 现有 Query invalidate 改为 await，但只负责页面查询刷新。

regenerate、edit、branch 的显式 checkpoint 语义不修改。

### 6.4 退出标准

- /compact 后输入框立即为空；
- 不刷新页面立即发送，请求不携带 compact 前 checkpoint；
- 模型能依据摘要和保留消息回答压缩前的关键决定；
- 发送失败重试、Thread 切换和页面刷新不产生错误分支；
- 已有 409 CAS 语义保持不变。

## 7. PR12：/Dream 手动立即整理

### 7.1 产品语义

/Dream 表示：

> 立即把当前 Project + Owner + namespace 中已经提取完成、仍为 pending 且尚未绑定任务的
> Candidate，按当前 Memory Policy 冻结为现有 memory_consolidate Job。

它不重新扫描对话，不等待尚未完成的 memory_extract，不同步运行模型，也不创建 private Run。

### 7.2 后端 API

新增：

~~~text
POST /api/projects/{project_id}/memory/v2/consolidate?namespace=default
~~~

响应：

~~~json
{
  "namespace": "default",
  "disposition": "queued | already_running | no_candidates",
  "jobId": "uuid | null",
  "candidateCount": 0
}
~~~

- queued 返回 202；
- already_running 和 no_candidates 返回 200；
- pipeline 为 off/shadow 返回 409；
- 当前 Memory 模型不可用返回 503；
- 复用现有 PRIVATE_WORK_CREATE 和 SHARED_ASSETS_EXECUTE capability；
- 不增加 capability、表或 Job 类型。

### 7.3 最小后端实现

修改：

- backend/app/gateway/routers/project_memory.py
- backend/app/private_work/memory_service.py
- backend/app/scheduler/memory.py
- backend/packages/harness/deerflow/persistence/private_work/memory_v2_repository.py

实现：

1. 提取定时和手动整理共用的 current policy、精确 model 和 contract 构造 helper；
2. Repository 增加精确 project + owner + namespace 的立即准入入口；
3. Scheduler 使用 now - interval，/Dream 使用 now，只跳过年龄等待；
4. 共用现有 scope lock、active Job 检查、最多 20 条 Candidate 冻结和 generation 创建；
5. 并发两次只能创建一个 active Job，第二次返回 already_running；
6. 手动入口不能调用全局 admit_next_consolidation 或全局 dead recovery；
7. Worker 继续执行现有 MemoryConsolidateJobHandler。

### 7.4 前端必须拦截为内置命令

修改：

- frontend/src/components/workspace/input-box-helpers.ts
- frontend/src/components/workspace/input-box.tsx
- frontend/src/core/private-work/memory.ts
- frontend/src/core/skills/slash.ts
- backend/packages/harness/deerflow/skills/slash.py
- frontend/src/core/i18n/locales/types.ts
- frontend/src/core/i18n/locales/zh-CN.ts
- frontend/src/core/i18n/locales/en-US.ts

规则：

- /dream 和 /Dream 大小写不敏感；
- 只接受无参数、无附件的严格命令；
- 参数或附件显示明确错误，绝不能退化为普通消息；
- dream 加入前后端 reserved slash names；
- handler 调 API 后立即 return，不调用 sendMessage；
- queued、already_running、no_candidates 显示准确反馈；
- 成功后清空输入，失败保留命令。

### 7.5 后台结果刷新

在 frontend/src/components/projects/private-work/project-memory-page.tsx 中只给 factsQuery 和
candidatesQuery 增加 15 秒前台轮询，refetchIntervalInBackground 为 false。

不增加 SSE/WebSocket，不修改全局 QueryClient。

### 7.6 退出标准

- 未到 interval 的当前 Owner Candidate 可立即入队；
- 其他 Owner 和 namespace 不动；
- 并发两次只有一个 active Job；
- private_runs 行数不增加；
- Worker 通过原 memory_consolidate 完成 Fact 和 Candidate 结算；
- /Dream 从未显示为聊天消息或 Skill；
- Memory 页面一个轮询周期内显示结果。

## 8. PR13：个性化记忆启用与重置

### 8.1 产品位置

新增在普通登录用户的“设置”对话框：

~~~text
设置
  └─ 个性化
       └─ 记忆
            ├─ 启用记忆  [开关]
            └─ 重置记忆  [危险操作]
~~~

它不属于 /admin/settings/system。管理员页面继续管理全平台 Memory Policy；个人页面只管理
当前账号的选择。

界面沿用 Codex 截图的信息层级，并适配现有 ActWeave 组件：

- 一张紧凑的分隔列表，不堆叠多个通用卡片；
- “启用记忆”右侧使用 Switch；
- “重置记忆”右侧使用 destructive 小按钮；
- 重置必须打开确认 Dialog，明确说明不会删除聊天；
- 提供加载、保存中、错误、禁用和成功状态；
- 移动端保持文字与操作上下排列，不压缩成不可点击的小控件。

### 8.2 启用记忆的准确语义

平台开关和个人开关共同决定最终状态：

~~~text
effective_memory_enabled =
  platform_memory_enabled AND user_memory_enabled
~~~

个人关闭后：

- 下一次模型边界不再注入长期记忆；
- memory_search 不返回长期记忆；
- 新成功 Run 不再创建 Source Batch 或 memory_extract Job；
- Scheduler 和 /Dream 不再创建该用户的 memory_consolidate Job；
- Worker 在写 Candidate 或 Fact 前重新检查个人设置；
- 已排队的 Memory Job 在下一边界取消或释放；
- 已有 Fact、Candidate 和 Evidence 保留，不删除；
- Thread Context、Thread 摘要、聊天记录和文件完全不受影响。

重新开启后：

- 已有 active Fact 可以再次召回；
- 新对话恢复提取和整理；
- pending Candidate 保持原状态并可继续处理。

### 8.3 重置记忆的准确语义

重置作用于当前账号在所有 Project 和 namespace 下的 Owner-private Memory：

- v1 Memory 与旧 Fact；
- v2 Source Batch、Source Item、Extraction/Consolidation Generation；
- Candidate；
- Fact、Revision、Evidence；
- Run Memory Snapshot 及其渲染正文；
- 相关 active Memory Job。

Gateway 必须先从服务端枚举当前账号有权管理的当前项目和仍处于隐私保留期的历史项目，
再逐个调用 Project + Owner scoped Repository。客户端不能提交项目清单，任何 SQL 也不能只按
owner_user_id 做无项目边界的批量删除。

重置不删除：

- Thread 和聊天消息；
- Thread Context 与手动 /compact 摘要；
- Run、文件、Artifact、Automation；
- 账号和项目成员关系；
- 个人“启用记忆”的开关值。

完成后普通 Memory API 和页面应显示为空；历史重放、retry 或旧 Job 不能重新生成已重置内容。
未来新对话是否产生新记忆由“启用记忆”开关决定。

### 8.4 最小 Schema

为 users 增加两个明确字段，不建设通用 JSON 偏好平台：

- memory_enabled BOOLEAN NOT NULL DEFAULT TRUE
- preferences_version BIGINT NOT NULL DEFAULT 1

同步修改：

- backend/packages/harness/deerflow/persistence/user/model.py
- backend/packages/harness/deerflow/persistence/full_schema.sql
- Schema marker 从 full_schema_v2 升为 full_schema_v3

仓库不支持增量迁移，因此不能对旧开发库手工 ALTER 或改 marker。PR13 完成后必须创建空库并
执行 make setup-db、make check-db。用户此前允许删除本地开发数据库，但实际执行删除前仍按
明确目标停止服务并走仓库支持的空库流程。

### 8.5 账号 API

新增严格账号级 API：

~~~text
GET   /api/v1/account/personalization
PATCH /api/v1/account/personalization
POST  /api/v1/account/personalization/memory/reset
~~~

GET 响应：

~~~json
{
  "memoryEnabled": true,
  "effectiveMemoryEnabled": true,
  "platformMemoryAvailable": true,
  "version": 1
}
~~~

PATCH 请求：

~~~json
{
  "memoryEnabled": false,
  "expectedVersion": 1
}
~~~

重置请求：

~~~json
{
  "confirm": true,
  "expectedVersion": 2
}
~~~

规则：

- 只从当前认证账号取得 user_id，不接受客户端 owner/project；
- PATCH 使用 preferences_version CAS，冲突返回 409；
- GET 和响应不返回任何 Memory 正文；
- 重置返回擦除的范围计数，不返回被删除的正文；
- platformMemoryAvailable 为 false 时仍保存用户选择，但 effective 为 false；
- 重置成功后 version 递增，重复旧请求返回 409。

### 8.6 后端集成点

新增一个小型账号偏好 Repository/Service，并在以下边界读取同一份设置：

- backend/app/private_work/memory_source_admission.py
- backend/app/scheduler/memory.py
- backend/app/worker/memory_extract.py
- backend/app/worker/memory_consolidate.py
- backend/app/private_work/memory_authority.py
- memory_search 的 v2 authority/repository 入口
- PR12 的手动 consolidate service

Memory 写入边界在结算前再次检查设置。重置事务与 Memory 写入使用一致锁顺序，确保：

- Worker 先完成时，重置随后擦除结果；
- 重置先完成时，旧 Worker 找不到有效 work 或看到 cancel，不能重新写回；
- 重置期间的新请求不会产生半清理状态。

实现优先复用现有 hard forget、source suppression、Job cancel 和 snapshot erase 逻辑，
不新建 reset Job 或第二套状态机。

### 8.7 前端实现

新增或修改：

- frontend/src/components/workspace/settings/settings-sections.ts
- frontend/src/components/workspace/settings/settings-dialog.tsx
- 新增 frontend/src/components/workspace/settings/personalization-settings-page.tsx
- frontend/src/core/settings 或新增 account-personalization API/hook
- frontend/src/core/i18n/locales/types.ts
- frontend/src/core/i18n/locales/zh-CN.ts
- frontend/src/core/i18n/locales/en-US.ts

Settings section ID 增加 personalization，标签为“个性化”。不复用项目 Memory 页只读的
Runtime Policy Settings tab，也不把个人开关存入 localStorage。

关闭开关后立即失效相关 Memory 查询；重置成功后清除所有 Project Memory query root，
当前打开的 Memory 页面应在下一次查询显示空状态。

### 8.8 TDD

后端单元和 PostgreSQL：

- 新账号默认 memory_enabled=true、version=1；
- PATCH CAS 成功、冲突和严格 schema；
- 两个账号设置隔离；
- 关闭后不准入 extract/consolidate，不召回、不搜索；
- 关闭发生在模型调用期间时，Worker 结算不写 Candidate/Fact；
- 重开后已有 Fact 恢复召回；
- reset 覆盖 v1、v2、Candidate、Fact、Revision、Evidence、Snapshot 和 Memory Job；
- reset 不删除 Thread、Run、消息和 /compact 摘要；
- reset 与 Worker 并发后没有 Memory 正文复活；
- 其他账号和共享 Project 中其他 Owner 数据不变。

前端：

- 个性化 section 能从 SettingsDialog 打开；
- GET loading/error/success 状态；
- Switch PATCH 使用 expectedVersion；
- 409 保留服务端状态并提示刷新；
- reset Dialog 明确“不删除聊天”；
- 未确认不能发请求；
- 成功后 Memory queries 被清理；
- 请求和 Query cache 不包含 Memory 正文。

### 8.9 退出标准

- 新用户默认启用，现有空库初始化正确；
- 关闭后下一次模型调用不再注入或搜索 Memory；
- 关闭后新 Run 不生成 Source/Candidate/Fact；
- 重新开启后已有 Fact 可恢复召回；
- 重置后所有项目 Memory 页面为空；
- 重置前后 Thread、消息和 /compact 摘要完全保留；
- 与正在运行的 Extract/Consolidate 并发时没有内容复活；
- 两个账号、两个 Project、两个 Owner 隔离测试通过；
- full_schema_v3 空库 setup 和 check-db 通过。

## 9. 完整门禁

### 9.1 后端 PR

聚焦测试通过后，从仓库根目录运行：

~~~bash
POSTGRES_TEST_URL="postgresql+asyncpg://.../postgres" make test
~~~

测试 URL 只允许指向可创建和删除随机 deerflow_test_* 的维护库，不能指向正常开发库。
完整核心必须 0 skip。

同时执行：

~~~bash
cd backend
make lint
uv run ruff format --check .
~~~

### 9.2 前端 PR

~~~bash
cd frontend
pnpm test
pnpm check
~~~

PR11、PR12、PR13 分别补充对应 Playwright 真实交互用例。

### 9.3 通用

~~~bash
git diff --check
git status --short
~~~

真实环境验证前执行 make check-db，并通过持久前台 make dev 启动完整栈。独立检查
Gateway/Nginx health、2026 入口和 Worker/Scheduler 日志。

## 10. 最终真实环境复验

PR13 完成后，重新执行一段有明确目标的长时间对话：

1. 同一 Thread 持续完成需求澄清、约束变更、错误纠正和方案冻结；
2. 混入明确长期事实、仅本次要求、角色扮演、假设信息和含糊指代；
3. 验证自动上下文压缩后仍能复述关键决定；
4. 新 Thread 冷启动第一次调用即召回正确 Fact；
5. 执行 /compact 后不刷新页面立即继续；
6. 对未到定时周期的 pending Candidate 执行 /Dream；
7. 保持 Memory 页面打开，观察后台结果自动刷新；
8. 在“设置 → 个性化”关闭记忆，验证下一次调用无注入/搜索；
9. 重新开启，验证已有 Fact 恢复；
10. 执行重置，验证 Memory 页面为空但聊天和 /compact 摘要仍在；
11. 检查 Source、Candidate、Generation、Fact、Revision、Evidence、Snapshot 和 Job 状态；
12. 测试结束后把 consolidation interval 恢复为 120 分钟。

最终必须同时满足：

- 错误质量样例没有进入 active Fact；
- 自动整理无 OUTPUT_INVALID 或 dead Job；
- 冷启动首次召回成功；
- /compact 后下一条消息使用服务端最新 head；
- /Dream 从未成为聊天消息、Skill 或 private Run；
- 个人开关同时约束提取、整理、召回和搜索；
- 重置清除 Memory 但保留全部 Thread Context；
- 页面在一个轮询周期内显示后台变化；
- 后端、前端、随机 PostgreSQL 和真实环境均有本次执行证据。

## 11. 每阶段完成报告

~~~text
PR：PR9 / PR10 / PR11 / PR12 / PR13
状态：完成 / 未完成

修改：
- 实际文件及职责

未做：
- 本 PR 明确排除的范围

验证：
- 聚焦测试及结果
- 完整模块门禁及结果
- PostgreSQL 结果或未执行原因
- 真实环境结果或未执行原因

退出标准：
- [x] 已满足
- [ ] 未满足

结论：
- 可以进入下一 PR / 停止并修复当前 PR
~~~

# RAG Knowledge M9（模型注册表与检索模型拆分 B1 + DeepSeek/OpenAI 适配器精简）Implementation Plan

> 状态：已交付（2026-08-30 立项，同日审查修订并实施，代码审查后完成
> P1/P2 修复与验收缺口补齐）。对应《RAG 知识库 MVP 执行计划》
> 的 M9 小节。动机：管理端"模型设置/知识模型"双菜单并存；一条知识模型
> 配置把 Embedding 与 Reranker 捆绑在同一行、共用同一把 API Key——
> 跨供应商组合不可能、重排被迫强制、组合爆炸伴随密钥副本、只换重排也要
> 全量重嵌入。本计划对齐 Dify 的信息架构（供应商级凭据 + 类型化模型 +
> 消费侧独立选择、rerank 为可选检索设置），不引入其插件体系。
> 本次另纳入 LLM 适配器精简：DeepSeek 收敛为一个 `deepseek` 标识和
> 完整回注实现（Task 9）；删除 `patched_openai`，改为“OpenAI 兼容（Chat
> Completions）”与“OpenAI Responses”两个固定协议入口（Task 10），共同
> 使用原生 SDK、不迁移签名补丁。用户已确认 M9 可以重置数据库，本期
> 按重置后重新初始化交付，不兼容旧适配器标识或旧 checkpoint。LLM 的
> `system_model_configs` 不迁入新注册表，快照校验与密钥世代机制仍保留；
> LLM 目录整体整合仍留 B2，不在本次范围内。



## 目标

宿主层新建模型注册表：凭据按供应商配置一次；Embedding 与 Reranker 拆成
独立的类型化模型；Knowledge Base 只绑 embedding 模型，reranker 变为库级
可选检索设置（换绑/关闭即时生效、不重建）；`actweave_knowledge` 让出模型
配置所有权，经注入端口消费宿主解析的物化材料；管理端合并为一个
"模型管理"入口；语言模型区块同时将 DeepSeek 双适配器收敛为单入口和
共享实现；OpenAI 删除补丁版本，以 `openai` / `openai_responses` 两个
协议入口复用 `langchain_openai:ChatOpenAI`，不复制两套客户端。
不合并 Flash/Pro/Vision 等不同模型记录。M9 采用重置数据库后
重新初始化的交付方式，不提供旧数据迁移、适配器别名或旧计量版本升级。
已有数据库须在确认准确目标、停服和数据处置后显式执行 `make reset-db`。
该命令清理整个应用的 `public` Schema，不仅是 Knowledge 数据；旧模型、
密钥世代、Run 和 checkpoint 不作为 M9 的可恢复数据。本次文档修订不执行
重置命令，实际操作仍须确认目标；MinIO 文件不随 SQL reset 自动删除，
其保留或清理另行确认。

## 审查后确定的边界

- B1 不提供 Provider 整体停用状态；仅模型有 `active|disabled`，被引用模型
不可停用/删除，有子模型的 Provider 不可删除。
- 被任一 Base 引用的 embedding 模型所属 Provider 不得原地修改 `base_url`；
同模型名、同维度、探活成功都不能证明向量空间兼容。需要换端点时新建
Provider/模型，再逐库显式 rebuild；Key 和超时仍可受控更新。
- 无 rerank 时保留原始余弦分 `[-1,1]`，不做归一化或截零；M9 将 rerank 分
明确限定为 `[0,1]` 并补客户端范围校验。阈值仍为 `[0,1]`，`0` 明确
表示不过滤，日志允许负分。
- Query embedding 按 embedding 模型复用；候选召回预算按
`(embedding_model_id, reranker_model_id)` 分配，`NULL` 也是独立子组。
- 模型端口替换不影响 Project 授权、Worker 的 `project_active_check`、
删除/恢复执行保护及独立于功能开关的 Project purge 能力。
- DeepSeek 对外及运行时标识统一为 `deepseek`，界面显示“DeepSeek”；
删除 `patched_deepseek` 描述器与标识分支，但保留其完整 reasoning 回传
能力。默认模型在重置后的新库按统一标识重新 seed，不建设旧记录兼容层。
- OpenAI 按协议分成 `openai`（Chat Completions）与 `openai_responses`
（Responses）两个描述器，共用原生 `ChatOpenAI`；协议由入口固定，不由
另一个用户开关或 SDK 自动推断。删除 `patched_openai` 实现及专属分支，
不迁移 `thought_signature` 补丁，也不新增 Gemini/网关签名支持。



## 实施与合并顺序

Task 1–11 是工作项，不对应可独立上线的批次。内部按三个阶段推进：

1. **准备**：固定新契约、Schema 变更和回归清单；先准备 Task 1/4 的新增
  类型、客户端及 `model_in_use` 接口，再接 Task 2/3 的密钥、seed 和
   注册表逻辑。测试 fixture 与 replay seed 同步准备，不提前删除旧入口；
   Task 9/10 的适配器回归可独立准备，无需等待新检索注册表或 reset。
2. **原子切换**：在功能分支内联调 Task 1–10，接通包消费者、宿主装配、
  安装/replay 引导及全部前端入口；最后一起退役旧表、列、契约、模块和
   路由。不得单独合并“先删旧 Schema/secret adapter”或仅更新后端契约的
   中间状态，也不为这些中间状态建立长期双写/兼容层。Task 9/10 与 Task 7
   的语言模型表单同阶段联调，一起退役 `patched_deepseek`、`patched_openai`
   标识及 OpenAI 补丁实现；不维护
   两份适配器实现，也不保留指向旧数据库的混合版本运行方式。
3. **验收**：执行 Task 11 和全部放行门，更新当前实现文档后整体合并交付。
  准备、切换、验收是内部顺序，不是三次独立发布；未通过门禁不得标记 M9 完成。

各 Task 描述的是最终目标状态；旧实现的删除集中在原子切换收尾。

## 前置事实（已核实）

- 包客户端已是"物化材料"形状：`KnowledgeModelMaterial` 含明文 key
（`repr=False`，仅内存存活），解密统一走 `materialize_model_material`
单一漏斗；端口替换点干净。
- 包 ORM 既定惯例：指向宿主表的外键只写 SQL 快照、不进包 metadata
（`knowledge_bases.project_id → projects` 先例）。
- `backend/tests/knowledge/` 全部 PostgreSQL 测试统一安装完整
`full_schema.sql`，无"只建包表"路径，跨界外键无测试障碍。
- 配置行在包内的消费点仅五处：`bases`（FOR SHARE 绑定校验）、
`ingestion._begin_processing`、`retrieval._searchable_groups`（按配置分组）、
`segments._embedding_material`、`models/service.py`（CRUD 本体）。
- 全系统模型消费面审查（2026-08-30）：LLM 消费点（Run 执行链的
lead/委派/title/summarization/memory/vision 辅助、输入润色、聊天压缩与
追问建议、Memory Dream、Agent Builder、Skill Builder 校验、管理端探活）
全部走 system 目录/Run 快照/运行时策略链，与知识模型零耦合；全库
pgvector 仅知识两表，Memory 检索为 pg_trgm 词法相似度、不用 embedding。
结论：注册表 B1 仅服务知识，LLM 仍走原 system 目录/Run 快照链路；
新增 Task 9/10 仅精简该链路中的 DeepSeek/OpenAI 适配器，不改模型所有权。
- DeepSeek 现有两条适配器描述使用相同字段；bootstrap 的三个 DeepSeek
模型均用 `patched_deepseek`。当前安装 SDK 的原生路径出站不保留历史
`reasoning_content`，补丁路径会恢复；流式和非流式入站处理由同一 SDK
实现继承。本地无网络 payload 对照已核实差异，但不据此假定数据库中
没有管理员创建的 `deepseek` 行，也不将离线验证当作真实 Provider 验收。
- 密钥机制选型理由：系统侧用 Generation+Tombstone 是因为 Run 快照持有
秘密世代引用、轮换需 fail-closed；知识/注册表没有快照类长生命周期
引用，摄取与检索均即时物化当前值，行内 envelope 两列即可，轮换即
更新当前值。



## 文件范围

```text
backend/packages/harness/deerflow/persistence/model_registry/（新增 ORM）
backend/packages/harness/deerflow/persistence/full_schema.sql
backend/packages/harness/deerflow/persistence/final_schema_contract.py
backend/packages/harness/deerflow/persistence/final_schema_digest.py
backend/packages/harness/deerflow/persistence/schema_comments.sql
backend/packages/knowledge/actweave_knowledge/
backend/app/model_registry/（新增域：service/secrets/bootstrap/gateway）
backend/app/knowledge/（model_port 适配、composition、gateway、bootstrap 退役）
backend/app/system_settings/validation.py bootstrap.py（统一 deepseek、OpenAI 双协议描述器）
backend/app/system_settings/execution_adapter.py（固定协议材料化，复用既有密钥/快照机制）
backend/app/gateway/routers/admin_model_settings.py（协议描述器契约核验）
backend/packages/harness/deerflow/models/patched_deepseek.py provider_wire.py factory.py provider_outcome.py
backend/packages/harness/deerflow/models/patched_openai.py（删除，不向 openai 迁移实现）
backend/packages/harness/deerflow/agents/middlewares/provider_request_usage.py
backend/packages/harness/deerflow/agents/middlewares/provider_request_cost_adapter.py
backend/packages/harness/deerflow/runtime/journal.py（Responses summary 识别）
backend/packages/harness/deerflow/utils/oneshot_llm.py
backend/app/shared_assets/skill_design_activity.py
backend/scripts/setup_postgres.py check_postgres.py reset_postgres.py
backend/scripts/run_runtime.py run_replay_gateway.py
backend/scripts/generate_schema_comments.py
backend/tests/model_registry/（新增）
backend/tests/knowledge/
backend/tests/replay_knowledge.py test_replay_gateway_identity.py
backend/tests/test_runtime_environment_security.py
backend/tests/test_setup_postgres.py test_check_postgres.py test_reset_postgres.py
backend/tests/test_deepseek_adapter.py（新增）
backend/tests/test_model_adapter_descriptor_projection.py test_model_runtime.py
backend/tests/test_model_runtime_multimodal_adapters.py test_system_model_execution_adapter.py
backend/tests/test_model_runtime_architecture.py
backend/tests/test_run_execution_profile.py
backend/tests/test_provider_request_usage.py test_provider_request_context_evidence_guard.py
backend/tests/test_compaction_trigger_capacity_clamp.py
backend/tests/test_default_system_model_bootstrap.py test_configuration_secret_lifecycle_postgres.py
backend/tests/test_provider_outcome_classifier.py
frontend/src/core/admin-settings/model-registry/（新增，替代 knowledge/）
frontend/src/core/admin-settings/models/（单一 DeepSeek、OpenAI 双协议描述器契约）
frontend/src/core/messages/utils.ts（Responses 文本/工具/summary 展示与历史回放）
frontend/src/components/admin/settings/
frontend/src/components/admin/operations/admin-operations-shell.tsx
frontend/src/app/admin/settings/
frontend/src/core/knowledge/
frontend/src/core/i18n/locales/
frontend/src/components/projects/knowledge/
frontend/tests/e2e/ frontend/tests/e2e-real-backend/
frontend/tests/unit/（语言模型表单与 Context 投影契约回归）
docker/（引导环境变量改名传递）
```



## Task 1：Schema 全家桶（原子切换工作项）

- 新表 `model_providers`：`id`、`name`（唯一 `lower(name)`）、`base_url`、
`request_timeout_seconds`（默认 30，CHECK 1..300）、
`api_key_nonce`/`api_key_ciphertext`（行内 envelope，
同知识侧现行两列形状）、时间戳；**无 Provider status 字段或停用接口**。
- 新表 `model_provider_models`：`id`、`provider_id`（FK → model_providers，
ON DELETE RESTRICT）、`model_type`（CHECK ∈ embedding|rerank）、
`model_name`、`embedding_dimension`（CHECK：`(type='embedding') = (dimension IS NOT NULL)`，1..16000）、`max_batch`（embedding 默认 64、
rerank 默认 32）、`status`（active|disabled）、时间戳；唯一键
`(provider_id, model_type, model_name)`。
- ORM 放 `deerflow/persistence/model_registry/model.py`（宿主 `Base`，
与 `system_settings/model.py` 同惯例）。
- `knowledge_bases`：`model_configuration_id` 改为 `embedding_model_id`
（NOT NULL）+ 新增 `reranker_model_id`（NULL）；两列外键
`REFERENCES model_provider_models(id) ON DELETE RESTRICT` 只写
`full_schema.sql`，包 ORM 保持裸 UUID；"引用行必须是对应类型"由端口
校验兜底（跨行约束无法用 CHECK 表达）。
- 删除 `knowledge_model_configurations` 整表及其外键与注释；
`knowledge_*` 由八张变七张。
- `knowledge_queries.top_score` 的 CHECK 从 `[0,1]` 放宽为 `[-1,1]`
或 NULL；NULL 仍只表示没有结果，保留最终返回分的原值。注释生成源与
SQL 注释同步改为“最终返回引用的最高检索分数，可为负；无结果为空”，
不再写成固定的 Reranker 相关性分数。
- 同批联动：`full_schema.sql`、`schema_comments.sql` 与生成脚本、
`final_schema_contract.py`（`KNOWLEDGE_APP_TABLES` 移除退役表；新表随
宿主 metadata 入列）、`SCHEMA_V1_CANONICAL_DIGEST` 重算、
`setup/check/reset_postgres.py` required relations、
`test_schema_repository.py` 与相关 Schema 契约测试。



## Task 2：注册表密钥与 seed（原子切换工作项）

- `app/model_registry/secrets.py`：行内 nonce/ciphertext，复用
`deerflow.secrets` 的 `SecretKey`/`SecretEnvelope`；recipient 绑
provider UUID + base_url origin（对齐系统侧 `model_secret_recipient`
的端点绑定姿态，比知识现行"只绑配置 UUID"更严）——因此**修改
base_url 必须先通过 embedding 引用冻结检查，再同时重新提交 API Key**，
已存密文不能被改道到新端点；Key 留空保留仅适用于地址未变的更新；
`app/knowledge/secret_adapter.py` 退役。
- `app/model_registry/bootstrap.py`：advisory lock 下、仅当两表全空时
seed——SiliconFlow provider（`https://api.siliconflow.cn/v1`、timeout 30、
加密 Key）+ embedding 行（`Qwen/Qwen3-VL-Embedding-8B`，4096，64）+
rerank 行（`Qwen/Qwen3-VL-Reranker-8B`，32），固定 UUID5；
竞败方只读返回。
- 引导环境变量 `ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY` 改名
`ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY`，skip 改为
`ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP`；
`setup_postgres.py` 的 `knowledge_models` 引导阶段替换为注册表阶段，
保留"空库初始化缺少预检材料即失败"的语义；包内 `bootstrap.py` 与
`app/knowledge/bootstrap.py` 删除；Compose 与 `Install.md` 的变量
名同步。`backend/scripts/run_runtime.py` 的 installation-only 过滤名单
同批增加两个新名称，旧名称也继续过滤（不恢复旧引导入口）；无论来自
`.env` 还是父进程，新旧 Key/skip 都不得进入 Gateway/Worker/Scheduler。
- `backend/tests/replay_knowledge.py` 的 seed 改为 Provider + 独立
embedding/rerank 模型，删除对旧包 bootstrap 与 secret adapter 的依赖；
同步 `backend/scripts/run_replay_gateway.py` 的启动调用、返回模型 ID 和
replay 测试约定，保证启用 Knowledge 的 Gateway 能实际启动。
- 脚本测试三件套（setup/check/reset）、`test_runtime_environment_security.py`
和 `test_replay_gateway_identity.py` 更新；环境测试用假值分别覆盖 `.env`
与父进程来源，断言两个新名称及残留旧名称均被移除，不读取真实密钥。



## Task 3：注册表服务与 Admin API（原子切换工作项）

- `app/model_registry/service.py`：providers CRUD（不提供停用），models CRUD
（`provider_id`/`model_type`/`model_name`/`embedding_dimension` 建后不可变，
改=建新行；status 可切换），按类型探活复用包客户端
`verify_embedding`/`verify_rerank`。
- 引用保护：被任一 Base 的两个模型字段引用即 `in_use`，包括待删除 Base；
被引用模型不可停用/删除，有模型的 Provider 不可删除，FK RESTRICT 兜底。
Provider 存在被引用 embedding 子模型时，`base_url` 修改在探活前拒绝；
提交前重新检查，不能用同名/同维度或成功探活代替此保护。
- 锁规则：注册表写操作先 Provider FOR UPDATE，再按 ID 顺序锁必要的模型
FOR UPDATE；绑定端口先 Provider FOR SHARE，再模型 FOR SHARE，均使用
调用方事务。`model_in_use(session, model_id)` 在该事务内做非锁定引用
查询，不另开事务、不反向锁 Project/Base；绑定方保留原有 Project →
Membership → Base 资源锁顺序，再进入 Provider → Model。模型的
`provider_id` 不可变；查找所属 Provider 后须在持锁状态重读模型校验。
- 更新 base_url/timeout/Key 时，冻结目标 Provider 材料、密文与全部子模型
集合及其参数，结束短事务后探活，再重新加锁比较这些值、检查引用并提交；
中途变化返回冲突，不持有数据库事务调用外部模型，不覆盖并发修改。
每个 active 子模型均须按类型探活成功；模型创建/重新启用也必须探活，
提交前比较 Provider 材料，防止探活期间端点/Key 轮换后误用旧结论。
无 active 子模型的 Provider 仅保存并标注“凭据已配置，尚无可用模型验证”，
不虚构 Provider ping 或连接测试成功。地址不变时 Key 留空=保留；
地址可改时仍要求重交 Key。
- Admin 路由（`app/model_registry/gateway.py`）。前缀并入 admin settings
家族（与 `/api/admin/settings/models` 一致，替代知识现行的
`/api/admin/knowledge/models` 形状）：
`GET/POST /api/admin/settings/model-providers`、
`PATCH/DELETE /api/admin/settings/model-providers/{provider_id}`、
`GET/POST /api/admin/settings/model-providers/{provider_id}/models`、
`PATCH/DELETE /api/admin/settings/provider-models/{model_id}`、
`POST /api/admin/settings/provider-models/{model_id}/test`；
删除旧 `/api/admin/knowledge/models*` 全部五条。
- 守卫与审计与现行 admin 面完全同门：`system_admin` 经
`authenticated_system_identity`（非管理员 404 隐藏存在性）、
route_class 沿用 `AdminOperationsRoute` + 系统审计上下文，增删改/
轮换/探活全部进 `audit_logs`；模块门控沿用 knowledge（未启用 404
`KNOWLEDGE_DISABLED`），门控以依赖注入实现，便于 B2 折入 LLM 时
单点移除。
- 路由守卫测试的 admin expected 清单同步。
- `backend/tests/model_registry/`：CRUD、Key 轮换、分型探活、in_use 保护、
bootstrap seed、Admin API 契约；补同维度异空间端点修改拒绝、端点修改
与绑定竞争、停用/删除与绑定竞争、stale probe 不覆盖新值、子模型集合
变化以及无模型 Provider 的未验证状态。原 `test_models.py` 的引用冻结、
密钥与 stale-probe 回归迁到这里，不能随旧服务删除而丢失。



## Task 4：包契约与客户端拆分（原子切换工作项；新增接口先于 Task 3 消费）

- `contracts.py` 删除 `KnowledgeModelConfiguration{Create,Update,View}`、
`KnowledgeModelOption`、`KnowledgeModelConnectionResult`、
`KnowledgeSecretPort`、`KnowledgeProtectedSecret`；新增
`KnowledgeEmbeddingMaterial`（model_id、base_url、model_name、dimension、
max_batch、request_timeout_seconds、api_key `repr=False`）、
`KnowledgeRerankMaterial`（同上无 dimension）与协议
`KnowledgeModelPort`：`lock_model_for_binding(session, model_id, model_type)`、`embedding_material(session, model_id)`、
`rerank_material(session, model_id)`。端口方法接收调用方 session——
建库/重建/换绑在自身事务内按 Provider → Model 取得 FOR SHARE，与
注册表更新/停用路径的 FOR UPDATE 串行化；模型必须类型匹配且 active，
Provider 存在且材料可解析。包只调用端口，不导入宿主 ORM 或查询宿主表；
解析失败统一抛 `KNOWLEDGE_MODEL_UNAVAILABLE`。
- `models/client.py`：`KnowledgeModelMaterial` 拆为两份 material；
`verify_connection` 拆 `verify_embedding`/`verify_rerank`；批量、重试、
index/维度等响应校验沿用。Reranker 分数由现有“仅 finite”检查补为
finite 且 `[0,1]`，越界抛 `KNOWLEDGE_RERANK_FAILED`，不得夹值；测试
覆盖 0/1 接受、负数/>1/非有限数拒绝。`models/service.py` 与
`materialize_model_material` 删除，目录只留客户端。
- `module.py`：构造入口改为 `(settings, session_factory, model_port, project_active_check, model_client=None)`（保留 keyword-only）；
`create_knowledge_module` 和 `app/knowledge/composition.py` 继续要求注入
`project_active_check`，不得删掉 `run_worker` 的缺失回调检查。保留 claim
事务内 Project-active 检查、待删除任务退回 retry_wait 不耗 attempt、恢复
后自动继续的语义；独立 `create_knowledge_project_purger` 不增加模型依赖。
删除六个模型配置方法；新增 `model_in_use(session, model_id) -> bool`
（查 `knowledge_bases` 两列、不拥有事务）；管理列表可在自己的读事务调用。



## Task 5：五服务改造与行为语义（原子切换工作项）

- `bases/service.py`：create/rebuild 经 `lock_model_for_binding`；
rebuild 参数改 `embedding_model_id`（语义不变：version bump + 逐文档
重入队，允许同模型重跑）；update 新增 reranker 换绑与清除（不重建、
不 bump）；IntegrityError 兜底保留（SQL FK 仍在）。
- `ingestion/pipeline.py`：`_begin_processing` 在锁文档事务内
`port.embedding_material(session, base.embedding_model_id)`。
- `retrieval/service.py`：Query embedding 按 `embedding_model_id` 缓存在
单次搜索内；**召回前**按 `(embedding_model_id, reranker_model_id)` 分组，
`NULL` reranker 也成组，每组独享现行 `candidate_k` 预算。组内仍合并
general/parent_child、父段去重后截断；不得先在整个 embedding 组截断，
再把所剩候选分给不同 reranker。各有 reranker 的组独立调用，失败仍失败
整个搜索；重排调用 `top_n=len(candidates)`，避免先截到 top_k 再按
各 Base 的不同阈值过滤而漏掉合格候选。NULL 组按余弦分排序，所有组
均先应用各自阈值，再稳定排序、Segment 去重、取全局 top_k。保留
每次外部调用前、召回事务内及最终返回前
的 Project authority 复核；无 rerank 也不能绕过最终复核与日志权限检查。
- `segments/service.py`：`_embedding_material` 改走端口。
- 行为语义（写死，防实现漂移）：
  1. 无 rerank：最终分=原始余弦相似度 `[-1,1]`；有 rerank：仍是客户端
    校验后的 relevance score `[0,1]`。`citation.score` 与查询日志
     `top_score` 同源；不以绝对值、截零或归一化规避日志约束。
     `score_threshold` 及 Base 默认值仍限 `[0,1]`，0=不过滤（含负分），
     正阈值过滤小于阈值的最终分，未传继续用 Base 默认值；
  2. 跨 rerank 模型/余弦分仍直接混排进全局 top_k，这是 B1 接受的质量限制，
    不是分数已校准或相关性概率的承诺；不引入新的校准/融合框架；
  3. 模型行所属 Provider/type/名称/维度不可变；Provider 无停用状态，
    有被引用 embedding 时端点冻结；允许的地址更新仍须重交 Key，并与
     timeout/Key 更新一起遵守 Task 3 的探活及提交前复核；
  4. in_use =被任一 base 的两列之一引用，in_use 模型不可停用/删除；
  5. 注册表路由与 knowledge 同门控（未启用 404）；
  6. 绑定校验并发：调用方同事务内的 Provider → Model 锁顺序、引用复核
    与 SQL FK RESTRICT 共同保护，详见 Task 3/4。
- 包测试改造：`test_models.py` 缩减为客户端校验（宿主治理回归迁至 Task 3）；`test_bases/retrieval/ ingestion/upload/governance/worker/metadata` 的配置 seed fixture 改为
注册表行 + fake port；新增"无 rerank 检索路径""换 rerank 不重建
不重嵌""rebuild 换 embedding 语义"用例；再覆盖负余弦 + 阈值 0 时
citation/log/命中计数一起成功、正阈值过滤、NULL/正分日志、同组不同 Base
阈值在 top_k 截断前生效、相同 E 不同 R
各有候选预算但 query embedding 仅调用一次、NULL 子组、混排的确定性结果。
保留 Worker 删除/恢复、Project purge、authority、文档版本与分段写回守卫回归。



## Task 6：适配层与项目路由（原子切换工作项）

- `app/knowledge/model_port.py` 实现 `KnowledgeModelPort`（用传入 session
查两表、按 Task 3/4 锁定并校验、解密、拼 material）；`composition.py`
替换模型端口注入，同时保留 `project_active_check` 和独立 purge 装配。
- 项目路由（`app/knowledge/gateway.py`）：
`GET /model-options` → 改由注册表供数，返回
`{embedding: [...], rerank: [...]}`（id、provider_name、model_name、
dimension），成员可见性不变；
`POST /bases` 收 `embedding_model_id` + 可选 `reranker_model_id`；
`PATCH /bases/{base_id}` 支持 reranker 换绑与清除（Pydantic
`model_fields_set` 区分"未传"与"传 null=关闭重排"）；
`POST /bases/{base_id}/rebuild` 收 `{embedding_model_id}`。
- `KnowledgeBaseView` 只带两个模型 id，显示名由前端用 options 映射
（包不跨界 join 宿主表）；Agent `knowledge_search` 工具不动（断言
输入 schema 不变的测试保留）。Citation `score`、Query `top_score` 字段
结构不变，新增负分穿过 HTTP/ToolMessage/前端投影的回归，不新增工具参数。
- 项目路由守卫 expected 清单同步；`test_admin_api.py` 的知识模型部分
移除（由 Task 3 的注册表契约测试接管）。



## Task 7：前端管理端（原子切换工作项）

- `core/admin-settings/knowledge/` 退役，新建
`core/admin-settings/model-registry/`（providers+models 的
api/hooks/types/query-keys；写路径沿用 `useImperativeRequest`，密钥不进
query 缓存；Key 留空=保留语义照搬）。
- `admin-knowledge-settings-page.tsx` 退役，新建
`admin-model-registry-page.tsx`：供应商卡片（名称、base_url、Key 状态、
超时、编辑/轮换）+ 卡片内模型列表（类型徽标、维度、批量、状态、
in_use、逐模型测试/停用/删除）+ 添加供应商/模型 Dialog + 删除确认；
文案沿用组件内本地 copy 常量的现行模式。
- Provider 不展示整体停用开关；存在被引用 embedding 时禁改地址并解释
“新建供应商/模型后重建”，不禁用 Key 轮换或超时编辑。地址允许修改时
强制输入新 Key；无 active 模型时显示“已配置，未验证”，不显示探活成功。
UI 禁用只作提示，服务端必须重新验证引用和材料。
- `/admin/settings/models` 页改为两个区块：语言模型（仅按 Task 9/10 精简
DeepSeek/OpenAI 选项，其他行为保留）+
检索模型供应商（新组件）；导航删除 `knowledgeSettings` 入口、
`settings` 标签改"模型管理"；`/admin/settings/knowledge` 路由删除；
导航 i18n 更新 `adminOperations.navigation`（types/zh-CN/en-US）；
Knowledge 模型选择、检索与评分文案另按 Task 8 同步，不限于导航改名。
- 模块未启用状态：管理导航入口保持静态（与现状一致），供应商区块
收到 404 `KNOWLEDGE_DISABLED` 时渲染"Knowledge 模块未启用"的明确
空态（凭 `knowledgeCode` 区分于一般加载失败），语言模型区块不受
影响照常工作。



## Task 8：前端项目侧（原子切换工作项）

- `core/knowledge/types.ts`：options 重形为 embedding/rerank 两组；
`RebuildKnowledgeBaseInput → { embedding_model_id }`；base view 两个
模型 id 字段；`api.ts`/`hooks.ts` 随动（update base 支持
reranker_model_id 含显式清除）。
- 创建向导 step2 只选 embedding 模型（rerank 留设置页，与 Dify 一致）。
- `knowledge-bases-view.tsx` 的 `CreateBaseDialog`（“创建空知识库”）同改：
读取 `options.embedding`，提交 `embedding_model_id`，初始 reranker 为空；
不再读取旧 options 数组的 length/map 或 `display_name`。两个创建入口
均显示 Provider 名称 + 模型名，加载、失败、无可用 embedding 三种状态
分开呈现；没有 rerank 模型不阻止建库。
- `knowledge-base-detail.tsx` 设置面板拆两块："重排序模型"下拉（含
"不使用重排序"空选项，保存即生效、不重建）+ 重建区块只选 embedding
模型（确认对话框语义不变）。
- `knowledge-search-panel.tsx` 及中英文 i18n 同步去掉“检索一定两阶段/分数
一定来自 Reranker”的表述，说明可选重排、余弦/重排分范围与“0 不过滤”。
搜索结果和查询历史统一用中性“检索分数/最高检索分数”，不把分数当作
百分比或概率，也不根据当前 Base 的 reranker 反推历史分数来源；本次不
新增 score_kind 或历史模型快照。切换重排后清除旧搜索结果，重新检索再展示。



## Task 9：DeepSeek 适配器收敛（与 Task 7 联调，不迁移 LLM 目录）



### 协议依据与最小实现

- 依据 [DeepSeek 思考模式官方指南](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)
（2026-08-30 核对）：携带 `tools` 的请求须完整回传历史 assistant 的
`reasoning_content`，包括未实际产生 `tool_calls` 的轮次；未携带 `tools`
时可不传，传入也会被忽略。因此保留现有补丁的完整回传即可，不新增
“有 tools 才恢复”分支，更不能按某条消息是否有 `tool_calls` 决定回传。
- [多轮对话官方指南](https://api-docs.deepseek.com/zh-cn/guides/multi_round_chat/)
说明 `/chat/completions` 无状态，历史 messages 仍由调用方传递；不新增
普通多轮专用适配器，不丢弃已保存的 reasoning，不修改历史消息内容。
- 复用 `PatchedChatDeepSeek` 及 `restore_assistant_payloads`，保留其
`is_lc_serializable`/`lc_secrets` 密钥序列化保护；唯一 `deepseek` 描述器
直接指向这一实现，不再同时暴露原生 SDK 与补丁两个选项。内部类名可以
沿用，不为去掉类名中的 Patched 扩大改造；适配器 ID 则只保留 `deepseek`。
不新增 HTTP 客户端、不升级 SDK 来替代本次收敛；修正补丁注释中
“所有思考请求都必须回传”的过强表述。流式/非流式入站沿用 SDK。



### 唯一标识与重新初始化

- 删除 `PROVIDER_ADAPTERS`/builtin catalog 中的 `patched_deepseek` 项，
将 `deepseek` 指向保留完整回注的实现；factory 的 DeepSeek 集合、
Provider outcome、wire/计量白名单、类路径推断及测试 fixture 同步收敛。
`patched_deepseek` 不接受新建、编辑、绑定或物化，不做运行时 alias。
- `app/system_settings/bootstrap.py` 的三个默认 DeepSeek 模型统一 seed
`provider_adapter="deepseek"`；模型 UUID/名称和 Flash/Pro/Vision 的独立
身份不变，thinking 开关、Run 模式与 reasoning-effort 映射仍沿用。
在重置后的空库按新标识正常生成 checksum、Secret recipient 和加密世代，
不在旧数据库里直接替换字符串，也不复用旧标识加密的密文。
- DeepSeek 安装密钥仍走原 `ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY`，
与新检索 Provider 的引导密钥独立；LLM 不因此迁入 RAG 注册表。
不编写旧模型行、Run、Secret Generation 或 v6 checkpoint 的迁移/恢复
适配。M9 新建 Run 仍遵守原有冻结 payload、checksum 和密钥世代验证，
重置交付不意味着放宽这些安全机制。

### Wire、计量与管理端

- `provider_wire.py` 对唯一 `deepseek` 复用 `restore_reasoning_content`；真实
请求与 Provider Profile、cost fingerprint、Context lane、压缩容量估计
必须一致。无条件完整回传不需要给各计量入口新增 tools 参数。
- 原生 `deepseek` 的 wire 语义改变，同步提升
`PROVIDER_REQUEST_ESTIMATOR_REVISION` 与 `MODEL_REQUEST_COST_ADAPTER_REVISION`
（当前分别为 `provider-wire-engineering-v6` / `provider-wire-request-cost-v6`，
本次递增到 v7；若实施基线已变化则重新确认），更新相关版本/指纹回归。
新库中的 Run 从新 revision 建立 Profile；旧 v6 checkpoint 随数据库
reset 清理，不增加受控重冻结、双版本解码或历史指纹升级机制。版本和
指纹校验仍保持严格；回归 M9 新建 Run 的普通恢复、人工 `Command`
恢复及其他 Provider 的同版本计量，不把“无需旧版兼容”误解为不测恢复。
- 后端描述器目录只返回一个 DeepSeek 项；管理端沿用现有 descriptor 驱动
的创建/编辑表单，显示“DeepSeek”、提交 `deepseek`，无需别名展示映射或
旧记录特殊分支。Key 留空保留语义继续用于 M9 新建模型的正常编辑；
不增加第二套 DeepSeek 设置页面。



### 专项验收

- 新增 `test_deepseek_adapter.py`：非流式 SDK 响应、流式
分片及合并、连续工具子轮、没有实际 tool_calls 的回答、跨用户轮次、
无 tools 请求；断言统一适配器出站 reasoning 完整且原消息未被修改。
- 运行 descriptor、factory/runtime、多模态、execution adapter 与默认
bootstrap 回归，确认单实现不改变模型能力、采样/思考设置、密钥脱敏
或独立模型身份；运行 Provider usage/evidence-guard 回归，核对 wire、
fingerprint、reasoning lane 与估算 revision 一致。Profile 快照测试覆盖
新 revision 的普通/人工恢复、其他 Provider 同版本请求及旧/未知版本、
坏指纹拒绝；`test_compaction_trigger_capacity_clamp.py` 覆盖压缩 cutoff
和摘要复测按新 wire 计量。无 tools 用例须真正构造空工具集合，不使用
会将 `[]` 回退成默认工具的 fixture。
- 临时 PostgreSQL 安装/reset 回归确认三个默认模型均使用 `deepseek`，
无 `patched_deepseek` 记录；新模型留空 Key 编辑、密钥解密、Run 准入和
同版本冻结快照恢复正常，checksum/recipient 不匹配仍拒绝。安装与
后端 catalog 契约断言只有一个 DeepSeek 描述器，旧标识已不受支持。
- 前端单测及 Task 11 的 mock Playwright 覆盖新建仅一个 DeepSeek 选项、
创建/编辑提交 `deepseek`、Key 留空保留、Knowledge 关闭时语言模型
区块仍可用。临时后端/replay 验证重新 seed 后的模型与新 Run 准入；
离线/mock 与真实 DeepSeek 联通证据分开报告。



## Task 10：OpenAI 双协议入口（退役 patched_openai）



### 两个入口、同一原生实现

按 [OpenAI 官方协议迁移说明](https://developers.openai.com/api/docs/guides/migrate-to-responses)，
Chat Completions 使用 messages，Responses 使用 input/output items，工具调用
结构也不同；不能仅改显示名。两个描述器都指向 `langchain_openai:ChatOpenAI`，
由 SDK 处理各自的请求、响应和流式转换，不新增 HTTP 客户端。


| 管理端选择                       | provider_adapter   | 调用路径（base_url 默认含 /v1）             | 固定 SDK 设置                                                |
| --------------------------- | ------------------ | ---------------------------------- | -------------------------------------------------------- |
| OpenAI 兼容（Chat Completions） | `openai`           | `POST {base_url}/chat/completions` | `use_responses_api=false`                                |
| OpenAI Responses            | `openai_responses` | `POST {base_url}/responses`        | `use_responses_api=true`、`output_version="responses/v1"` |


- 两者默认 `base_url=https://api.openai.com/v1`，均允许显式配置支持相应
协议的端点。“兼容”指 Chat Completions 协议，不承诺所有供应商扩展；
第三方端点只有支持 `/responses` 才能选 Responses，不根据 URL/模型名
猜协议，也不在探活或执行失败后自动降级/切换协议。
- builtin/runtime catalog 新增公开 `openai_responses` 描述器，与 `openai`
共用基础字段定义；`provider_adapter` 随现有 System Model payload 冻结，
新 Run 继续沿用当前 checksum、Secret recipient 和 Generation 机制。
仅协议入口变化，不把 LLM 迁入检索注册表。



### 协议约束与运行时一致性

- `use_responses_api` 和 `output_version` 在这两个入口中改为由适配器派生，
不再作为管理员可填写的 settings 字段或 UI 布尔开关；Chat 入口不接收
Responses 输出格式，Responses 入口固定如上。后端拒绝请求中手写的
协议派生字段和冲突覆盖，不能只隐藏前端控件；其他适配器字段不随之删减。
- 采用 runtime-only 派生：持久化 settings、Admin DTO 和 canonical Run
payload 只保留已验证的业务参数及原有 `provider_adapter`，不写入上述
两个固定字段。现有 authoring/冻结 settings 验证之后，物化输出 ModelConfig
时按 ID 注入固定值，再由 factory 最终复核；不得把物化后的字典反写数据库。
这样不新增快照字段或“隐藏字段”兼容机制，前端编辑往返也不会因 descriptor
未声明却出现在 stored settings 中的协议字段而被判 incompatible。
- 物化与共享 factory 保证最终构造 `ChatOpenAI` 时传入确定的 true/false，
不留 None 给 SDK 自动选协议；检查模型配置、thinking overrides、Agent/
Run 参数合并后仍满足协议约束，冲突明确报错，不静默覆盖用户的另一种选择。
当前普通平台默认值可被 settings 覆盖，不能仅添加 default 就声称协议已固定。
- 新公开 `openai_responses` 同时作为已有 Responses wire/计量分类的标识；
`resolve_provider_adapter` 以冻结 ID 和最终模型协议一致性为准，不因共用
`ChatOpenAI` 类而把 Responses 推断成 Chat，也不得重复拼接 `_responses`。
wire、Profile、cost fingerprint、Context lane、outcome 和实际 SDK 请求
同步对应所选协议；与 Task 9 共用本次 M9 的计量 revision 提升。
- 保留现有客户端历史/checkpoint 和 ToolMessage 链路；新增 Responses 入口
不自动启用服务端会话链、`previous_response_id`、内置 Web/File Search 或
新 MCP 执行路径。这些能力不因选择协议自动获得授权或进入 M9 范围。
- 补齐现有 Responses 输出适配的缺口：SDK 可返回
`content[{type:"reasoning",summary:[{type:"summary_text",text:...}]}]`，
当前前端推理识别、RunJournal 计时及 oneshot/Builder 提取未完整覆盖此形状。
扩展现有提取函数处理 Provider 已返回的 summary 文本（包括流式合并），
按“推理摘要”呈现；没有 summary 就不显示，不承诺或构造完整思维链。
不解释/展示 `encrypted_content`，原始 AIMessage/标准工具关联仍留给 SDK
进行下一轮输入，不能为 UI 显示而破坏协议 items。



### 补丁退役与管理端

- 删除 `patched_openai` 描述器、`models/patched_openai.py`、
`PatchedChatOpenAI` 及其导入；清理 wire/outcome/计量表、类路径推断中的
`patched_openai` 和 `patched_openai_responses`。不保留别名或旧数据兼容，
沿用 M9 reset 后重新初始化的交付方式。
- 删除 `_restore_tool_call_signatures` 和专属回传分支，不把签名逻辑复制
到任一新入口，也不顺带补 Gemini 入站/流式/嵌套签名支持。DeepSeek 使用的
`assistant_payload_replay.py` 和 vLLM 自身的回放 helper/wire 分支保留。
- 管理端明确显示表格中的两个名称与协议说明，创建/编辑/探活均提交所选
ID；界面不再同时出现 “Use Responses API” 开关或 Output version 下拉。
同一入口普通编辑继续允许 Key 留空保留；切换入口属于改变适配器，按现有
Secret recipient 规则清空 Key 输入并要求重新提交，不能静默搬用旧密文。
不新增迁移向导或自定义签名兼容开关。



### 专项验收

- 更新 descriptor、system-model API/execution-adapter、factory/runtime 与
前端目录测试：两个 ID 都可新建/绑定/物化，补丁 ID 被拒绝；公开 settings
不可伪造协议字段，合并后的冲突也拒绝，原有密钥/快照校验保持；两种
配置编辑往返不带运行时派生字段。旧 `openai + use_responses_api=true`
的测试 fixture 改为显式 `openai_responses`，不绕过新的公开契约。
- 用本地 mock HTTP 记录实际路径和 body：Chat 只访问 `/chat/completions`
且使用 messages，Responses 只访问 `/responses` 且使用 input；分别覆盖
同步/异步、流式/非流式、标准工具调用多轮、图像、Flash/思考参数及同版本
Run 恢复。加入会触发 SDK 自动选 Responses 的模型/参数场景，确认 Chat
入口仍固定协议；错误响应不得引发另一协议请求。公共 max_tokens、
reasoning_effort、function tools 与结构化输出按各协议的 SDK 映射验收，
不把“共用字段”当成两个 HTTP body 形状完全相同。
- 保留并调整 `test_model_runtime_multimodal_adapters.py`、
`test_run_execution_profile.py`、Provider usage/evidence-guard/outcome 回归，
断言两种 body 投影、fingerprint、reasoning/tool lane 与实际请求一致；
Responses 原有工具结果和多模态投影不可退化。补 Responses 事件 →
AIMessageChunk 合并 → SSE/RunJournal → 前端的文本、工具参数 delta、
可见 summary 回归，覆盖历史刷新与工具多轮；summary 提取不改原始消息，
DeepSeek/Anthropic 的既有 reasoning 显示仍通过。
- `test_model_runtime_architecture.py` 等移除已删补丁模块的导入/白名单，
删除“补丁回传签名成功”的专属用例；临时库无补丁记录，生产导入无悬空
引用。浏览器按 Task 11 分别验两个选项的创建、探活、编辑和切换 Key 提示；
离线/mock 与真实端点联通证据分开报告。



## Task 11：浏览器验收（整体交付前）

Mock Playwright：注册表页建供应商、加两类模型、分型探活结论、in_use
保护（模型停用/删除禁用、被引用 embedding 的 Provider 地址冻结）、
Key 留空保留与允许改地址时必须重交、无模型 Provider 未验证状态；向导和
“创建空知识库”分别选 embedding 建库，断言新字段与初始 reranker 为空；设置页换
rerank 即时生效、选"不使用重排序"后检索仍返回结果；重建确认 POST
`{embedding_model_id}` 且文档重跑；导航合并后入口与面包屑正确；无重排/有
重排文案、负分显示与阈值 0、切换设置后旧结果清除均验收。
语言模型区块还须执行 Task 9 的 DeepSeek 单标识创建/编辑场景，不因
本页合并或 Knowledge 关闭而丢失唯一入口。
Task 10 同步验收“OpenAI 兼容（Chat Completions）”和“OpenAI Responses”
两个入口、无补丁入口/独立协议开关、不同 ID 的创建/探活/保存，以及切换
协议要求重交 Key 的提示；mock 与原生 API/replay 证据分开报告。

Real-backend Playwright（临时 PostgreSQL + MinIO + replay Provider）：
换 rerank 不触发重嵌入（provider `embedding_calls` 计数不涨）；关闭
rerank 后检索走余弦分且结果非空；换 embedding 重建后 provider 嵌入调用
增长、文档回 ready、新版本分段可检索。Task 2 的 replay seed/启动脚本
迁移是前置条件；记录实际执行数量，Knowledge 用例不得因引导失败而 skip。
调用计数断言隔离后台任务和检索自身的 query embedding：先等文档 ready、
采样计数，再仅 PATCH reranker 并检查 embedding_calls 不变，随后另测搜索。

## 放行门

- backend `make format`、`tests/model_registry/` 与 `tests/knowledge/`、
Schema 契约套件、脚本测试三件套、`test_runtime_environment_security.py`
与 replay 引导测试通过；负分日志/命中计数、同维度异空间端点冻结、
绑定/探活并发、独立候选预算、Worker 删除/恢复保护均有明确回归证据；
- frontend `pnpm check` 与单测通过；mock 与 real-backend Playwright
分开报告并全部通过；
- Task 9 的 DeepSeek 协议、单标识/单实现、重新 seed 的模型/密钥/Run、
新计量版本及同版本恢复、管理端唯一入口回归全部通过；不要求旧数据
兼容验收。真实 Provider 未测试时明确记录，不宣称已联通；
- Task 10 的 OpenAI 两协议入口与真实请求路径一一对应，协议覆盖/自动切换
不可绕过约束；补丁模块/描述器/派生标签已退出生产路径，原生
Chat Completions 和 Responses、标准工具调用及计量回归通过；不以
“恢复网关签名”的旧测试或新实现替代本次直接删除决定；
- 整体合并前 ORM、SQL 快照、digest、注释、Schema 测试及前后端契约一致；
无待删除 RAG 模块的运行时或 replay 导入，无旧检索模型字段/options 数组的
活跃消费者；除历史说明与旧标识拒绝测试外，`patched_deepseek` 不再作为
运行时标识、描述器或 seed 值出现（内部类/文件名沿用不算别名支持）；
`patched_openai`/`patched_openai_responses` 除历史说明和拒绝测试外无
活跃引用，原补丁模块已删除，共享消息恢复 helper 和 `openai_responses` 保留；
- 实施完成时更新文档：CONTEXT.md（退役 Knowledge Model Configuration，新增
Model Provider / Provider Model 词条，并与系统侧 `provider_adapter`
概念显式区分；Knowledge Base 词条改绑定描述）、
《RAG知识库独立软件包架构设计》决策修订注记（模型配置所有权移宿主）、
《RAG模型接入层设计》现状说明、《RAG检索模块技术设计》与
《RAG知识库系统需求文档》现状说明补 rerank 可选、评分域和候选预算、README、
Install（环境变量改名与 M9 重置安装步骤）、两份 AGENTS.md（含 DeepSeek
单实现/单标识及 OpenAI 双协议入口、固定协议与补丁退役说明）、
《RAG 知识库 MVP 执行计划》M9 小节状态更新；计划修订阶段仍保留“未实施”，
不提前把当前实现指南改成 M9 已交付；
- 已有数据库的 reset 另列操作者步骤：停服、确认准确数据库目标与整个应用
数据的保留/备份处置，核对授权目标后执行；不得把 reset 藏进普通启动
命令，临时测试库与操作者目标数据库必须明确区分。


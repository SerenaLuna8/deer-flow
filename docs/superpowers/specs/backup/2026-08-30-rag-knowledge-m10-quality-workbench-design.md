# RAG Knowledge M10：检索质量与知识维护工作区设计方案

> 状态：方案稿，待审阅、未实施（2026-08-30）。
> 前置里程碑：M9 完成前后端契约切换并通过其放行门。
> 本文定义目标契约，不表示当前代码已经具备这些能力，也不授权执行数据库重置。
> 执行拆分见 [M10 执行计划](../plans/2026-08-30-rag-knowledge-m10-quality-workbench.md)。

## 1. 目标与证据边界

M10 将 Dify 对比中适合本项目的十项增量落到现有 Knowledge Module：让模型得到完整证据、让更换 Embedding 不破坏人工内容、让文档定位和检索诊断形成闭环，再以评测约束混合召回和跨库排序。

已确认的实施基线：

- `rag-knowledge` 基于 `811226b3e4559ecffd03b69da59bae4fea5c11db`，包含尚未收敛的 M9 工作树修改。M10 实施前须重新记录 M9 验收后的 commit，不依赖本次行号或旧文档状态。
- 后端已有通用/父子分块、共享预览通道、可选 Reranker、typed metadata、查询历史、引用、文档/分段治理，以及 claim/lease/版本防迟到。
- 当前检索将内容截成前 320 字符，Agent 使用同一个 snippet；当前 rebuild 通过普通 ingest 重新解析原文件并替换 Segment。这是 M10 首先修正的实现，不是需要再引进一套 RAG 引擎。
- 当前前端仍存在旧模型配置字段。M9 的模型选项响应、建库/重建请求、引用保护和前端类型须先统一；M10 不新增旧 `model_configuration_id` 的兼容层。
- Dify 源码参照为 `9c16c865977e9d89a9ec7ae0536e893f4385a758`，本地运行镜像标记为 1.17.0。实测完成上传、两种预览、建库、分段查看、搜索返回定位和设置保存；Embedding 返回403，未证明成功召回、重排或问答效果。
- Dify 的“任务结束即显示嵌入已完成”、错误通知消失后回到初始占位，以及 new-rag 占位菜单不作为迁入目标。参考功能行为，不复制其代码、组件或视觉资产；直接复制须另做许可评估。

当前代码依据：
[检索投影](../../../backend/packages/knowledge/actweave_knowledge/retrieval/service.py)、
[Agent 工具](../../../backend/app/knowledge/run_tool.py)、
[rebuild](../../../backend/packages/knowledge/actweave_knowledge/bases/service.py)、
[摄取发布](../../../backend/packages/knowledge/actweave_knowledge/ingestion/pipeline.py)、
[创建向导](../../../frontend/src/components/projects/knowledge/knowledge-create-wizard.tsx)。

## 2. 范围

| 编号 | 本期能力 | 完成标准摘要 |
| --- | --- | --- |
| F01 | 完整模型上下文 | 模型获得预算内的完整 Segment；展示摘要与正文分离 |
| F02 | 安全重嵌入 / 原文件重新解析 | 前者保留人工内容和身份，后者明确覆盖并单独确认 |
| F03 | 预览文件选择 | 可以选择待上传文件，旧请求不能覆盖新文件预览 |
| F04 | 文档搜索 / 筛选 / 排序 / 分页 | 在完整、有界的权威列表上快速定位文档 |
| F05 | 深链接和返回定位 | 库、文档、Segment 可定位；返回保留列表状态 |
| F06 | 检索详情与诊断 | 原段落、真实命中 Child、实际参数、计数、耗时可查 |
| F07 | 真实阶段与进度 | 明确阶段、当前尝试及完成条目，不模拟百分比 |
| F08 | 元数据增强 | 字段发现、只读内建字段、同库有界批量赋值 |
| F09 | 词法与混合召回 | PostgreSQL 内完成派生词法索引与双路召回 |
| F10 | 多库候选预算与排序 | 各库有候选机会，不再直接比较不同模型的原始分数 |

不在 M10：URL/Notion 同步、外部知识库、Q&A/摘要索引、OCR/多模态、独立 Child 编辑、批量 Segment CSV、归档/暂停续传、内容版本历史、可视化 Pipeline、插件市场、向量数据库切换、ANN、资源级 ACL、Agent 绑定库的配置界面、LLM 目录整合。原有功能不重复建设。

## 3. 模块与不变条件

- `actweave_knowledge` 继续拥有业务 Implementation；一个 `KnowledgeModule.search()` 同时服务 HTTP 与 Agent，不能分别实现两套排名或过滤。
- 宿主 `app/knowledge` 拥有 HTTP、Project authority、M9 ModelPort、Worker 装配、Agent ToolMessage 投影。Package 不导入 `app.*` / `deerflow.*`。
- PostgreSQL 是文档、分段、向量、词法派生数据、任务和元数据的权威；MinIO 只保存原文件。不新增服务、消息队列或第二套任务状态。
- 继续使用 `shared_assets.read/edit`、server-issued Project context、owner-private 查询历史；system admin 不自动获得项目内容权限。
- Provider 调用不占用数据库事务；每个实际 HTTP 批次及重试前、发布/最终读取事务中重新验证相应 authority，后台写任务另验 claim/lease。不能仅在多批 embed/rerank 客户端入口检查一次。停止/失租后先等待已启动的阻塞工作收敛，再释放资源。
- 原文和检索结果属于不可信资料，不能升级为系统指令。日志/诊断禁止密钥、存储定位符、原始 Provider body 和未返回候选的正文。
- 本次不改 M9 的供应商、模型、密钥所有权和适配器选择；不因跨库排序隐式选择另一个收费模型。

## 4. F01 / F06：命中、模型正文与展示摘要

### 4.1 一个命中对象，多种安全投影

Package 新增只读 `KnowledgeSearchHit`，包含：

- `citation`：现有知识库/文档/Segment 身份、名称、position、短 snippet、最终排序分数及来源位置。
- `passage`：召回快照中的完整父 Segment 文本，general 同样返回完整 Segment；不返回整篇原文件。
- `document_version`、`content_digest`：文档执行代次和文本 SHA-256，检测同 ID 内容已更新，不是内容历史或访问令牌。
- `local_score`、`local_score_kind`、`score_domain`：阈值实际使用的原生分数及其来源。
- `ranking_method`、`ranking_score`：最终顺序的依据，见第8节。
- `matched_children`：每命中最多3个真实参与召回的 Child 身份、position、route 和该路分数；general 为空。由当次召回事务携带，禁止事后扫描 Child 猜命中。

`KnowledgeSearchResult.hits` 为唯一结果源；兼容既有调用点需要的 `citations` 只能从 hits 派生，不能并行维护两份排序列表。

投影规则：

1. 普通 HTTP 搜索默认仅返回短引用；`debug=true` 只增加第4.3节的有界信息，不整包返回 passage。
2. Agent ToolMessage 正文按排名装入完整 passage；`additional_kwargs.knowledge_citations` 只保存实际发送正文对应的短引用，不再次复制 passage。
3. Segment 详情通过单独受权威校验的读取取得。浏览器不能使用对象存储 key，也不能循环拉全库分段查一个 ID。
4. 新 Citation 增加可选 `document_version/content_digest/score_kind`；新写入必须提供，旧消息缺失时按历史短引用展示，来源类型为 unknown。不能拿当前模型配置反推旧分数，也不修改旧 ToolMessage。

### 4.2 上下文预算

M10 使用简单可验证的 **64 KiB UTF-8 JSON ToolMessage 正文硬上限**，包含 passage、名称和结构开销。它是字节预算，不冒充 token 预算；不新增一个与宿主不一致的 tokenizer。最终 LLM 请求仍由既有冻结 Provider profile / capacity guard 计量与准入。

- 逐个装入完整 Segment；装不下则跳过该项，尝试下一项，返回 `omitted_count` 和 `context_limited=true`。不能再次截为前缀并宣称“完整正文”。
- 当前单段上限4000字符，预算应保证合法的第一段可装入；若连第一段都不满足封装上限，返回稳定错误并保持无引用，不能伪造空命中。
- top_k 仍控制检索结果上限。查询日志/命中计数记录检索选中数，不声称等于模型最终使用数；ToolMessage 单独给出实际发送数。HTTP 与 Agent 共享检索，而非共享展示大小。
- 工具说明改为依据返回正文回答，结果已排序、分数不是正确性概率；资料中的指令不得影响执行权限。

### 4.3 详情和诊断 Interface

新增 `get_segment_detail(project_id, base_id, document_id, segment_id, expected_document_version?, expected_content_digest?, child_page)`：

- 校验完整资源关系、成员与读取能力；不存在/跨作用域沿用404。正常文档浏览可读已存内容；从检索结果打开必须提交期望代次和 digest，并要求文档仍 ready、内容代次为当前代次。
- 期望值不符返回 `KNOWLEDGE_CONFLICT`，UI 显示“文档已更新，请重新检索”；不能悄悄拿新文本解释旧分数。
- 返回完整单段、当前启用状态、来源及 Child 分页（每页最多50）；每页均复核期望值。失败重处理留下的行另带 `stored_content_version/current_document_version/content_state=stale`，仅在普通维护浏览中只读展示，不能用来解释旧检索分数。没有历史正文恢复能力。

检索测试新增可选 `debug=true`，诊断仅在本次响应存在，不落普通日志或新增检索追踪表：

- `strategy_version`、tokenizer version、实际目标库数、有效参数/候选预算；
- 各路召回数、父块去重数、阈值淘汰数、过期候选淘汰数、最终返回数；
- query embedding / recall / rerank / final validation 的服务端 monotonic 耗时；
- 原生分数类型、最终排序方法、模型 ID；只暴露项目模型选项已允许的安全材料，不返回端点、密钥或向量；
- `empty_reason=not_ready|no_candidates|filtered_out|stale_candidates`；模型/存储/权限错误仍走错误契约，不能伪装空成功。

`debug=true` 另返回与最终短引用一一对应的 `hit_diagnostics`：Segment ID、local_score/kind/domain、ranking_method/ranking_score，以及真实 matched_children 的 ID/position/route/score；无 passage、Child 正文或未入选候选材料。检索测试页显式请求 debug；详情读取只按这些身份高亮本次命中，不能从 Child 列表猜测。普通历史 Citation 没有此证据时仅展示原段落，不虚构命中 Child。

最终排序取 top_k 后，除现有 Project authority 外，批量复核 Base 绑定/有效检索参数、Document status/enabled/version、Segment enabled/content digest 和真实 Child 身份，并复用完整 metadata/builtin 硬过滤：字段改名、文档改名或批量赋值不一定改变内容代次，不能仅靠 version 防过期。内容或过滤不再符合的命中剔除，**本次不补位**，允许返回少于 top_k 并报告淘汰数；不拼接新正文、不隐式重试 Provider。模型/策略变更返回冲突，撤权/基础设施不确定性整体失败。该事务验证通过后才记录相应最终命中。

## 5. F02：把重嵌入与重新解析彻底分开

### 5.1 操作契约

| 操作 | 内容来源 | 保留 | 重建/覆盖 |
| --- | --- | --- | --- |
| 库级重嵌入 | 当前已发布 Segment / Child | UUID、文本、顺序、enabled、source_position、metadata、命中历史 | embedding、document_version、任务进度 |
| 文档重新解析 | 原始 MinIO 文件 + 本次确认的切分参数 | Document ID、名称、原文件、Document.enabled、metadata | Segment/Child 文本、UUID、段级启停和命中历史 |
| 失败重试 | 失败操作原有语义与已冻结参数 | 操作类型 | 不得把重嵌入失败转换成重新解析 |

原文件重新解析不是内容回滚。确认文案明确：人工增删改和段级禁用会被覆盖；成功后生成新 Segment，旧引用只保留历史摘要。Document.version 是执行代次，不是用户可恢复的版本。

### 5.2 重嵌入准入

保留 `POST /bases/{base_id}/rebuild {embedding_model_id}` 路径，在 M10 明确变为“重嵌入当前内容”；不继续支持这个路径的隐式重新解析行为。UI、契约测试和文档同批切换。

- 新增 `KnowledgeDocument.published_version`（初始 NULL），成功 ingest/reembed 发布时写入对应执行代次；`content_initialized` 仅为它非空的 DTO 派生值，不再存第二个布尔字段。人工删空后 published_version 不清除；即使没有 Segment 行，也能区分从未发布和已删空，以及失败重处理留下的内容代次。这不提供版本历史。
- Base 锁后按 UUID 排序锁 Document；有 uploading、queued、processing、deleting 文档或开放索引任务时拒绝本次库级操作，提示先等待/处理，避免换模型与上传/编辑并行改变向量空间。上传准入须使用同一 Base 锁规则。
- 对已初始化的 ready/failed 文档：版本增加、状态 queued，入 `reembed_document`，保留原行和字数计数。未初始化且 failed 的文档不自动重新解析，保持失败并在响应列明；显式 retry 使用新绑定模型走 ingest。
- Base 改绑与任务准入在一个事务内完成。旧模型和新模型即使同名/同维度也不视为同一向量空间；M9 Provider → Model 引用锁及 FK 保护不变。
- 重嵌入期间这些文档不参与任何召回（包括词法）；逐文档成功后恢复。M10 不做新旧向量双份存储、零停机切换或自动回滚模型。

### 5.3 重嵌入执行与重试

Worker 对 general 嵌入当前全部 Segment，对 parent_child 嵌入全部 Child，包含 disabled 内容，避免重新启用时缺向量。空的已初始化文档以零条向量成功发布。

发布只更新向量和代次、Document 状态/published_version、Task 终态，不删除/重建内容行；一事务验证 claim、target_version、当前模型绑定。失败保留内容行及 published_version，但文档不 ready。自动/手动 retry 保留 `reembed_document` kind；相关失败派生、开放任务约束、过期恢复和 Project purge 必须识别新 kind。

保留 UUID 会移除旧代码依赖“行被删除”实现的隐含冲突保护。因此手工 Segment edit/add 必须显式比较 Provider 调用前冻结的 Document.version、模型绑定及当前目标内容状态；迟到编辑不得写入新向量空间。相关治理路径与 reembed 使用相同锁序和版本规则。

### 5.4 原文件重新解析

新增 `POST /documents/{id}/reparse {expected_version, chunk_settings}`，只接受已初始化且 ready/failed、没有开放索引任务的文档；不能顺便换库模型。确认参数完整校验后固化到 Task 专用、可空的 `reparse_settings`，Document.version++，复用 `ingest_document`。这不是通用任务 payload 扩展点；仅允许完整、严格校验的切分/清洗参数，其他任务为空。retry 继承失败任务的这份参数。

Document 上的切分/清洗参数继续描述已发布内容；有 reparse_settings 的 ingest 按任务参数准备，成功时才与新 Segment/Child 同事务切换 Document 参数。失败时旧行及原模式不变，因此后续重嵌入仍能正确区分 general/parent_child。初次摄取继续使用上传时固化的 Document 参数。安全任务投影单独给出本次请求的重新解析参数，UI 不将其冒充已发布设置。

新增同资源的只读 `reparse-preview`：宿主下载该文档原文件到临时 Path，经同一 preview 通道计算；返回期望 Document.version 与预览结果，不改行、不入队。下载后及返回前重新鉴权；临时文件必清理。预览不证明之后仍可提交，reparse 仍执行 CAS。

重新解析失败时保留上一批已发布行，供有权限的文档维护界面查看并标记“旧内容，当前不可检索”；不让旧行重返召回。成功发布才替换内容，并重新生成词法数据。M10 不扩大重嵌入/重新解析的删除权限。

## 6. F07：阶段进度，不新增任务系统

`knowledge_tasks` 增加 `stage`、`completed_units`、`total_units`（可空）、`progress_updated_at`：

- stage：`queued|reading_source|extracting_splitting|loading_segments|embedding|publishing|done`；与 task.status 正交，failed 保留失败阶段。
- 读取/解析阶段显示不定进度；embedding 的单位是实际向量条目，parent_child 计 Child；只在一次 Provider 批次响应校验成功后增加完成数。
- 模型客户端复用内部 before-request guard，embed/rerank 的每个真实批次及重试都执行；embedding 另有成功批次进度回调供摄取/重嵌入复用。外部 Interface 不暴露任意事件总线。取消、撤权、回调失败或失租不继续派发下一批。
- 新 attempt 从零计数，展示 attempt_count；不将失败尝试的进度加到重试进度上。ready/done 只在最终事务发布成功时出现，不能用 completed_units==total_units 推断成功。
- Document GET/List 增加安全 `task_progress` 投影，绑定当前 target_version；不暴露 claim token、lease、storage_key、Provider 原始错误。旧 attempt 的迟到进度更新失败关闭。
- 继续既有2秒条件轮询，不新增 SSE 或独立任务中心。retry_wait 显示正在等待自动重试及安全的下次时间，自动尝试耗尽才提示手工重试；deleting+delete_error 的停轮询规则保留。

## 7. F08：元数据发现、内建字段与批量赋值

### 7.1 字段发现与硬过滤

新增 `list_filter_fields(project_id, base_ids?)`，返回每库 builtin/custom 字段、稳定标识、类型、允许操作和 writable；不返回扫描出的文档值/用户隐私。

内建只读字段首批固定为：`document_name`、`uploaded_at`、`file_type`、`source_type`。分别来自 Document 名称、created_at、原文件扩展名、当前固定 `file_upload`，不复制进 doc_metadata；不新增缺乏历史依据的 uploader。

过滤条件增加 `field_kind=custom|builtin`（缺省 custom），避免与已有任意自定义字段重名。现有 eq/contains/gte/lte、类型保护、AND、最多10条规则保留；未知 custom 字段仍按该库不匹配，不能悄悄删除条件。M10 不加入 LLM 自动过滤、OR/IN 或值自动猜测。

HTTP 使用字段发现结果建表单；Agent 增加一个只读 `knowledge_metadata_fields` 工具，绑定同一 Project/owner authority，并在每次调用重新读取，返回最多当前默认20库×24字段的安全定义；超过总量时明确告知并允许按库ID缩小，不静默截断。它只发现当前 Project 中有权限的库/字段，不增加 Agent 配置界面。`knowledge_search` 保持原参数集合，metadata 条目允许新增 field_kind。

### 7.2 有界批量赋值

新增 `PATCH /bases/{id}/documents/metadata`，请求 `{document_ids, values}`：同库最多100个文档、共同 patch 最多20个自定义字段。未触碰不改，null 删除，禁止写 builtin。数值必须有限；时间仍是 epoch 秒。

事务按 Project/Membership → Base → 字段 → 有序 Documents 锁定，基于当前 JSON 合并共同 patch；任一越权、缺失、deleting、重名/类型冲突则全批回滚。字段 create/rename/delete 和单/批量赋值统一锁序，防止并发改名后旧 key 回流。不做逐文档 commit，也不将全部 JSON 替换为表单快照。

UI 显示选中数量、混合值和“保持 / 设置 / 清空”意图；仅发送明确编辑字段，确认后一次提交。返回完整成功结果或单一失败，不伪造部分成功。批量元数据不会触发重新嵌入。

## 8. F09 / F10：词法召回、候选预算与排序

### 8.1 默认和派生索引

Base 新增 `retrieval_mode=semantic|hybrid`，默认 semantic；检索测试允许仅本次覆盖，Agent 使用库默认。保留必需 Embedding 和可选 Reranker，不加入无需 Embedding 的 Economy 产品模式。

Segment / Child 新增 `lexical_tsv` 与 `lexical_version`，建立 GIN 索引；general 词法召回使用 Segment，parent_child 使用 Child 后回卷 Parent。两种模式的父 Segment 都维护词法字段，供入围候选共同评分，不能因 parent_child 从 Child 召回而遗漏父段派生数据。M10 的新入库和手工编辑即使库仍用 semantic，也生成派生字段；重嵌入不重新切分或修改它们。

词法算法为包内固定版本 `lexical_v1`，不依赖新的服务、数据库中文扩展或词典下载：

- 只规范化派生文本：NFKC、英文小写；原文、引用位置不变。
- 连续中文用重叠二元词元，单字保留；英文/数字词保留。型号、错误码、带点/冒号/斜线的标识符保留完整项并补组成词；IP 地址用标准库解析后保留规范完整项。
- UTF-8 不超过128字节的词元编码为 `x`+十六进制，超长词元编码为 `h`+其SHA-256十六进制，再交 PostgreSQL `simple` 配置；标点不会二次解析，长URL/无空格内容也不因单词元长度而拒绝正常入库。单段派生输入硬限256KiB UTF-8，超限明确失败，不静默丢全文；用现有4000字符合法内容边界验证上限足够。
- 存在 hybrid 目标库时，query 最多128个去重词元，超过明确提示缩短检索文本或改用 semantic；全 semantic 搜索不生成词法查询、不施加这个额外限制。所有词元经生成器产生，以参数化 OR tsquery 查询，原始文本不得直接拼入 tsquery/SQL。
- 固定使用 `ts_rank_cd(..., 2)` 按文档长度归一化，不称为 BM25，也不解释成相关性概率。没有词元时词法路为空；hybrid 仍可走向量路。

词元序列同样属于版本契约：从左到右扫描，优先完整有效 IP，再最长带内部 `._:/-` 的英文数字标识符，再连续汉字，最后其他连续字母/数字；未识别标点作边界。标识符先输出完整项，再按分隔符输出组成词，同一片段内重复项去重；汉字依原位置输出单字及其与下一字的二元词元。文档中不同位置的重复词元保留，派生流按原位置排序，同位置先完整项/单字再组成项/二元项；交 simple 生成的是派生词元位置，不冒充原文位置。query 复用同一规则后才去重。固定样例包括 `ＡB-12 → [ab-12, ab, 12]`、`网络 → [网, 网络, 络]`，以及重复词、IPv4/IPv6、长标识符的编码/位置/排名快照；策略变动必须提升 lexical_version 并重跑评测。

PostgreSQL 的 `tsvector/tsquery` 与排序函数提供索引和查询基础，GIN 是其推荐全文索引类型；上述中文、标识符和融合规则是本项目设计，质量必须单独评测。[查询与排序官方说明](https://www.postgresql.org/docs/current/textsearch-controls.html)、[索引官方说明](https://www.postgresql.org/docs/current/textsearch-indexes.html)。

索引与 content 同事务更新：正式发布、人工 add/edit、Child 重切、重新解析全部覆盖；删除依现有关系清理。仅重嵌入不重算词法。词法版本不一致明确失败，不运行时补数据；若未来需要持有旧内容重建词法，只能另加显式维护流程，本期不建立后台自修复框架。

### 8.2 候选预算与一致过滤

设目标库数 N，全局父段候选预算 G=400，`B=min(100,max(20,5*top_k))`，每库每路 `C=min(B,floor(G/N))`。top_k 未传时仍先取目标库默认值最大值；N=0 返回空，C<1 明确拒绝，不能静默忽略库。预算为服务端固定策略，不开放多个同名 top_k 控件。

每库独立选最多 C 个父段：semantic 路按精确 cosine；lexical 路按词法分，Child 在各路内先回卷父块、取最佳分并去重，不能让同一父块的众多 Child 占满父段预算。hybrid 两路用 `Σ1/(60+rank)` 合并再保留 C；全局至多400父段，两个召回路的候选结果至多800项。所有 SQL 在 limit 前执行相同 Project、Base、Document.ready/enabled/version、Segment.enabled 和 metadata 硬过滤。

同 Embedding 的 query vector 继续单次搜索复用。包括词法新增候选在内，都计算其向量空间内的 cosine；parent_child 的父候选取全部当前 Child 的最大 cosine，不对 NULL 父向量计算，也不只取词法命中的那个 Child。Reranker 对完整候选评分，按同模型合批但遵守 max_batch 和全局预算。

### 8.3 原生阈值与最终排序分离

**阈值不应用于 RRF。** 有库级 Reranker 使用它的 `[0,1]` 原生分；无 Reranker 使用 cosine `[-1,1]`。0仍表示完全不过滤，非零阈值仍使用库默认/请求覆盖。因而词法命中仍可能被 cosine 阈值排除，界面必须说明；不能为制造命中而绕开用户阈值。

最终排名采用以下固定决策，不隐式挑选另一个 Reranker：

1. 全部目标库绑定同一个非空 Reranker：合并候选后由该模型统一评分，应用各库阈值，按原生分排序。
2. 单一可比分数域且全部为 semantic：保持原生排序。分数域为同一 Reranker 模型，或无重排时同一 Embedding 模型；不是“同一个库”。
3. hybrid 且无统一 Reranker，或存在异构分数域：每个分数域内把所有库候选按原生分统一排名。只要本次有 hybrid 库，就对全部入围候选的完整父段 `lexical_tsv` 用同一查询计算词法分，正分候选建立全局词法排名；semantic 库不因此增加召回候选。不按“是否挤入词法路top C”决定谁有第二路分，不将共同评分伪装成 Child 召回。等权融合 `rank_score=61/2*(1/(60+domain_rank)+1/(60+lexical_rank))`，没有正词法分或全semantic时第二项为0。按融合分及稳定资源身份破同分，不比较不同模型的原始数值。

第3项是可解释的秩融合，不是分数校准。纯语义、异构分数域且没有词法证据时，只剩域内名次的公平性折中，诊断必须返回 `heterogeneous_without_lexical_evidence`；不得承诺比 M9 更准确。该场景必须进入第11节真实评测，质量不达标就不能标记 F10 完成。

每路合并前的 rank 和阈值过滤后的 domain_rank/lexical_rank 均采用并列共享名次的 `RANK` 语义，例如 `1,1,3`，不使用任意输入顺序产生 row_number。各库原生阈值先应用，再建立最终分数域/词法排名并取 top_k。原始路候选截到 C 和最终等分结果均按 Base/Document/Segment UUID 稳定破同分；身份只用于稳定选择，不把相同原始分强行变成不同融合分。最终 stale 剔除不补位，见第4.3节。

`citation.score` 继续表示本次最终排序分：原生分或上述 `[0,1]` 融合分；`score_kind=cosine|rerank|rank_fusion` 必须同行。局部阈值分单独出现在 hit/debug，不对融合分开放相关性阈值。Query 增加 `top_score_kind`、`strategy_version`，top_score 与最终返回引用同源；旧历史缺来源时显示 unknown，不推断。

## 9. F03–F06：前端操作逻辑

### 9.1 预览

在现有向导加入 File picker；仅保存当前文件的预览，不缓存所有文件正文。首次选择/切换文件触发一次预览；参数变化标 stale，用户显式刷新。身份至少包含 File 对象、参数快照、scope generation、请求序号；删除/替换文件或切库后旧响应不得显示。上传对话框与向导继续复用相同参数校验。

### 9.2 文档列表与 URL

保留 `/projects/[project_slug]/knowledge` 和现有 Project client。URL 仅携带 `kb/view/doc/segment/status/sort/page`：UUID、枚举、正整数集中校验；view 为 documents/search/metadata/settings。库/文档切换用 push，筛选/页码用 replace；改变筛选重置页码。URL 不是访问授权，资源不匹配显示不可访问，不能回退到上次缓存对象。

不把检索问题、文件名关键词、metadata 值、预览内容写入 URL/localStorage。文档关键词和滚动位置保存在当前 account/project 的非持久 UI 状态，进入详情返回时恢复；刷新恢复安全 URL 定位但清空内容型关键词。不能为了复制 Dify 的 keyword URL 而把业务文本放进浏览器历史。

先取得完整文档列表，再执行文件名/原文件名关键词、生命周期状态、创建时间/名称排序和20条分页，最后以 ID 稳定破同分；不得只对后端第一页筛选。当前 listAllPages 在提前空页或触达页数上限时会把部分数据成功返回，M10 必须补齐完整性检查：未满足服务端 total 而停止时返回明确的不完整错误，不把部分 items 发布为完整列表；默认配额500不构成完整性证明。选择限定当前页，换筛选/页码清空选择；删除末页后回到合法页码。

### 9.3 文档与检索工作区

复用现有 Segment 浏览器和侧栏 primitives，不复制 Dify 组件。提供原段落、来源、真实 Child、元数据与处理状态；检索结果可打开详情，再定位到同一 Segment 的维护页。详情与诊断只在有权限时查询，过期内容明确要求重新检索。

宽屏问题/参数与结果并排，窄屏顺序布局；高级诊断折叠。显示本次参数而非当前 Base 参数；设置、重建、重新解析后清除旧结果。错误在结果区持久保留，区分从未测试、无命中、阈值过滤、未就绪、版本过期、模型失败，不仅依赖 toast。

所有新缓存仍在 account UUID / project UUID / knowledge 根下，转 scope 时 abort+remove；403/404/失权清除正文，普通刷新错误可保留已授权旧列表并提示重试。只读成员可查看字段、详情和进度，但没有写控件。弹窗焦点、键盘操作、错误关联及移动宽度进入验收。

## 10. Schema、HTTP 与交付

### 10.1 最小数据变更

| 位置 | 变更 |
| --- | --- |
| knowledge_bases | retrieval_mode，semantic 默认值与枚举约束 |
| knowledge_documents | nullable published_version（派生 content_initialized）；现有参数描述已发布内容，version 表示执行代次 |
| knowledge_segments / knowledge_segment_children | lexical_tsv、lexical_version、GIN 索引 |
| knowledge_tasks | reembed_document kind；专用 reparse_settings；进度字段；索引任务约束和失败/恢复识别同步 |
| knowledge_queries | top_score_kind、strategy_version；不新增正文/全量候选日志 |

不新增模型表、外部连接器表、内容历史表或任务追踪表。ModelPort 不增加模型所有权；Package ORM、full_schema.sql、catalog digest、中文注释、required relations 及安装/契约测试同批更新。

### 10.2 项目 HTTP 增量

以下均相对于现有 `/api/projects/{project_id}/knowledge` 前缀：

| Interface | 变更 |
| --- | --- |
| POST /search | 可选 retrieval_mode/debug；新增安全排名来源和诊断投影 |
| GET /bases、GET/PATCH /bases/{id} | retrieval_mode；其余延续 M9 |
| POST /bases/{id}/rebuild | 改为重嵌入当前内容，响应补受理/未初始化跳过数量 |
| GET /documents/{id}、列表 | content_initialized、安全 task_progress |
| POST /documents/{id}/reparse-preview | 原文件的只读参数预览 |
| POST /documents/{id}/reparse | expected_version + 固化参数，明确覆盖 |
| GET /bases/{base}/documents/{doc}/segments/{segment} | 单段详情、期望代次/digest、Child 分页 |
| GET /filter-fields | 当前库/库集合的 builtin/custom 发现，完整性与总量约束 |
| PATCH /bases/{id}/documents/metadata | 同库有界、全有或全无的共同 patch |

路由守卫清单、strict Pydantic/Zod、错误映射和 feature-off 404 同步。错误复用现有 INVALID_REQUEST / CONFLICT / NOT_FOUND / QUOTA_EXCEEDED / 模型错误，不透出 SQL 或 Provider 原始响应。

### 10.3 部署确认门

本仓库 Schema V1 当前不提供迁移 ancestry、漂移修复或在线升级。M10 的受支持安装验证路径是新的空数据库，不得自行引入 ALTER 迁移体系、改 marker 或让 Runtime 补列。

已有数据库的交付是单独操作者决策：确认准确目标、停服、备份/保留要求和 MinIO 处置后，才能决定是否允许显式重建。**M9 可 reset 的授权不延伸到 M10。** 若要求保留已有数据库内容且不能重建，则 M10 部署被阻塞，需另行批准非破坏升级方案；本文不承诺一个尚不存在的保数据升级路径。

运行期的“重嵌入保留人工内容”与“部署 Schema 时保留旧数据库”是两件事，不能混为一谈。此次只生成文档，不执行任何 DDL/reset 或业务数据改动。

## 11. 验收与完成定义

### 11.1 必须通过的确定性验收

- 答案位于第320字符后仍完整进入 Agent；超预算整段选择、引用一一对应，旧引用回放不退化。
- general/parent_child 的人工增改删、删空、enabled、UUID、来源及命中历史在重嵌入后保持；重新解析才覆盖。重嵌入失败重试仍保留语义，双向切模式的 reparse 失败后仍可正确重嵌入旧内容。
- 迟到编辑、失租、旧任务、模型改绑、删除/恢复、权限撤销不能产生跨版本/跨项目内容或向量。
- metadata 硬过滤在全部路的 limit 前及最终返回前生效；批量赋值冲突全部回滚，builtin 不可写。
- 进度来自成功批次和当前 attempt，失败不能显示成功，旧 attempt 不能覆盖新进度。
- 真正完整列表上的筛选、返回定位、A/B预览乱序、过期详情、跨账户/Project缓存清理、移动/键盘可用性。
- 相同模型复用 query embedding；每库/全局预算可测；局部阈值不被 RRF 替代；最终分数、历史、工具、前端来源一致。

### 11.2 真实质量和性能门

至少60条脱敏标注问题，覆盖中文自然语言、型号/错误码/IP、答案在段尾、父子回卷、大库+小库/异构模型、metadata 与无答案。按类别分层冻结开发集与独立验收集；验收集不少于30题且标识符不少于20题，调参只用开发集，不能在同一验收集上反复试到通过。固定语料、模型、参数、候选预算和硬件，分别记录 M9 与 M10 的实际召回数量、Recall@candidate、最终Recall@10、nDCG@10、非 Provider P95、外部调用/条目数和费用。

标注单位为完整父 Segment（general同样为Segment），以固定语料中的来源/位置/内容校验标识匹配，不能靠两次安装恰好相同的数据库UUID。相关性分三级：0无关、1相关但不足以回答、2可支持答案；Recall把2视为目标。Recall@candidate在每库两路合并并截C后、原生阈值/重排前计算；最终Recall@10在阈值、排序及最终复核后计算。nDCG固定 `top_k=10`、增益 `2^grade-1`、折扣 `log2(rank+1)`；IDCG=0的无答案题不混入Recall/nDCG平均值，单独报告按冻结生产阈值的误召回率和返回数量。

建议放行阈值（目标，不是已实现结论）：独立验收集精确标识符 Recall@candidate ≥95%、最终Recall@10 ≥95%；自然语言两种召回率均不低于基线超过2个百分点，nDCG@10不低于基线超过0.02；无答案误召回率不得高于同阈值基线；明确的尾部答案与权限/版本用例零漏项。非 Provider P95 相对基线恶化超过50%时必须优化或经记录的产品预算复审，不能只报平均耗时。数据规模至少覆盖1万检索单元；更大规模按实际使用补测，不虚构统一500ms承诺。

本地 replay Provider 用于确定性接口/索引/排名/竞态验证，不证明真实模型质量。401/403、额度、网络、缺标注数据阻塞真实验收时明确记录；不得以 skip、Dify 菜单存在、或 mock 分数达标替代 F09/F10 质量结论。

M10 只有十项功能、确定性门、真实质量门、Schema交付确认和文档更新全部完成才标记完成。各内部阶段完成可以记录，但不能把未通过的检索增强标记为已交付。

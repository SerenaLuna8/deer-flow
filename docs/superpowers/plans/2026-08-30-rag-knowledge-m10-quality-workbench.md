# RAG Knowledge M10（检索质量与知识维护工作区）Implementation Plan

> 状态：计划中，待审阅、未实施（2026-08-30）。
> 规范来源：[M10 设计方案](../specs/2026-08-30-rag-knowledge-m10-quality-workbench-design.md)。
> 前置：[M9 模型注册表计划](2026-08-30-rag-knowledge-m9-model-registry.md)完整验收。
> 本次只编写方案和计划；未执行下面的开发、测试、Provider 调用或数据库操作。

## 目标与非目标

交付 F01–F10：完整模型正文、安全重嵌入/重新解析、预览文件选择、文档定位、深链接、检索详情/诊断、真实进度、元数据增强、词法/混合召回、多库预算与排序。

保留 Knowledge Package、Gateway/Worker、M9 ModelPort、Project authority、现有轮询及 PostgreSQL/MinIO；不引入外部连接器、Q&A、OCR、多模态、Pipeline、插件平台、第二套任务系统或数据库自动升级。

## 实施纪律

1. Task 是内部工作项，不是各自可独立发布的版本。涉及 strict DTO/Schema 的变更在功能分支内联调后一起交付，不发布中间不兼容状态。
2. 每项按聚焦失败测试 → 最小实现 → 回归验证推进。仅为实现细节重复覆盖的测试可以整合，但权限、lease、版本和真实协议回归不得随重构删除。
3. 不改变 M9 的模型所有权、Provider 端点冻结、Secret recipient 和适配器。M9 的临时兼容问题必须在 Task 0 收敛，不能由 M10 扩建旧接口兼容层。
4. 现有数据库是否可重建须单独确认；M9 reset 授权不继承。本计划不引入迁移框架，Runtime 不补列/改 marker。临时测试库与操作者目标库必须区分。
5. 源码和接口门、真实 PostgreSQL/MinIO、mock/replay 浏览器、真实模型质量分别报告；外部403不是成功证据，也不通过 skip 绕过。

## 范围与任务对应

| 功能 | 主要任务 | 验收任务 |
| --- | --- | --- |
| F01 完整正文 | T1、T5 | T11、T13 |
| F02 安全重处理 | T1–T4、T12 | T13 |
| F03 预览选择 | T10 | T13 |
| F04 文档定位 | T9 | T13 |
| F05 深链接 | T5、T9、T11 | T13 |
| F06 详情/诊断 | T5、T7、T8、T11 | T13 |
| F07 阶段进度 | T1、T4、T12 | T13 |
| F08 元数据 | T1、T6、T12 | T13 |
| F09 混合召回 | T1、T7、T8 | T13、T14 |
| F10 多库预算/排序 | T7、T8、T11 | T13、T14 |

## 文件范围

现有文件（实施时重新核对，保持拥有者路径）：

```text
backend/packages/knowledge/actweave_knowledge/
  contracts.py __init__.py module.py
  bases/service.py documents/service.py segments/service.py metadata/service.py
  ingestion/pipeline.py ingestion/preview.py
  retrieval/service.py models/client.py
  persistence/models.py persistence/tasks.py persistence/derivations.py
  tasks/worker.py project_retention.py
backend/app/knowledge/gateway.py run_tool.py composition.py authority.py model_port.py
backend/packages/harness/deerflow/persistence/
  full_schema.sql final_schema_contract.py final_schema_digest.py schema_comments.sql
backend/scripts/generate_schema_comments.py setup_postgres.py check_postgres.py reset_postgres.py
backend/tests/knowledge/
backend/tests/replay_knowledge.py
frontend/src/components/projects/knowledge/
frontend/src/components/workspace/citations/knowledge-citations-panel.tsx
frontend/src/core/knowledge/ frontend/src/core/i18n/locales/
frontend/tests/unit/core/knowledge/ frontend/tests/unit/core/threads/knowledge-citations.test.ts
frontend/tests/e2e/project-knowledge.spec.ts
frontend/tests/e2e-real-backend/knowledge-real-backend.spec.ts
docs/knowledge/ README.md Install.md backend/AGENTS.md frontend/AGENTS.md
```

建议新增的实现文件（尚不存在，不先建立空框架）：

```text
backend/packages/knowledge/actweave_knowledge/ingestion/reembedding.py
backend/packages/knowledge/actweave_knowledge/retrieval/lexical.py
backend/packages/knowledge/actweave_knowledge/retrieval/ranking.py
frontend/src/core/knowledge/navigation.ts
frontend/src/core/knowledge/document-list.ts
frontend/src/components/projects/knowledge/knowledge-search-result-detail.tsx
backend/tests/knowledge/test_reembedding.py
backend/tests/knowledge/test_search_details.py
backend/tests/knowledge/test_lexical_retrieval.py
backend/tests/knowledge/test_search_ranking.py
backend/tests/knowledge/test_task_progress.py
backend/tests/knowledge/fixtures/m10_retrieval_cases.json
```

新增代码只在出现实际调用方时落地；不暴露内部 tokenizer、融合器、进度回调作为 Package 公共框架。

## 阶段 A：前置与契约

### T0：固定 M9 基线和交付前提

- [ ] 记录 M9 验收 commit、工作树状态、实际前后端模型选项 DTO，以及完整验收证据。当前计划稿/旧行号不作为实现权威。
- [ ] 验证 embedding_model_id、nullable reranker_model_id、原生分数与阈值语义、注册表引用保护、replay 引导，以及旧字段活跃消费者退役。
- [ ] 将原生分数契约、未来融合分、操作类型、期望版本和 metadata field_kind 固定为测试样例；不得隐式选取另一模型。
- [ ] 确认已有数据库交付约束。若不能重建且必须保数据，记录部署阻塞并另立审批事项，不自行实现 ALTER/reset 路径。
- [ ] 收集第14项评测语料与真实 Provider 使用条件；没有标注数据或调用权限不妨碍设计/离线开发，但阻塞对应真实放行门。

验收：M9 不是靠名称标为完成；M10 的操作/分数/数据处置有明确记录，没有未经授权的破坏性动作。

### T1：最终 DTO 与 Schema 一次定义

依赖 T0。

- [ ] 在 contracts 中定义 SearchHit、score kind/domain、safe diagnostics、Segment detail（含旧内容只读状态）、reparse 输入/任务专用参数、task progress、filter field 和 batch metadata DTO；由 __init__/module 暴露最小业务 Interface。
- [ ] 定义新增 Citation 字段的新写入规则和旧消息可缺省规则；hits 为结果唯一源，citations 派生，避免双重排序。
- [ ] ORM/SQL 同步增加设计方案第10节字段与索引；Document.published_version 派生 content_initialized，避免双重状态；reembed 与 ingest 在同一 Document/target_version 上只能有一个开放索引操作，不能因 kind 不同绕过唯一保护。
- [ ] 同步 catalog digest、注释源/快照、Schema 契约与 required relations；不增加新知识库模型表。
- [ ] Gateway strict DTO、前端 strict Zod 与 route guard expected 清单同步准备。调试字段不得直接序列化 ORM、material 或异常对象。

测试：扩展 `test_schema_repository.py`、`test_package.py`、现有 Schema/注释/安装脚本测试；覆盖列类型、约束、索引、模型和快照一致性、未知字段拒绝、缺省历史 Citation。

验收：在隔离空库一次安装成功；不依赖 runtime 建表、旧字段双写或手工 stamp。

## 阶段 B：证据完整性和内容保护

### T2：重嵌入当前内容

依赖 T1。

- [ ] 扩展 Base rebuild 准入：持 Base/有序 Document 锁，拒绝上传/开放索引/删除冲突；content_initialized 区分已删空与未成功发布。
- [ ] Base 改绑、Document.version++ 和 reembed Task 同事务；已初始化 ready/failed 入队，未初始化 failed 明确跳过，不擅自从原文件解析。
- [ ] 新 handler 读取当前完整 Segment/Child，包括 disabled；general 嵌入父段，parent_child 只嵌入 Child；零条内容有效成功。
- [ ] 发布仅更新向量/代次、published_version及状态，保留 UUID、文本、位置、enabled、source_position、metadata 和计数；失败保留原published_version，旧向量在非 ready 期间不能通过任何召回路使用。
- [ ] retry、failure derivation、expired recovery、Worker 注册、Project retention 的开放任务扫描识别新 kind。手动 retry 继承失败语义。
- [ ] 修正 Segment edit/add 的隐含保护：明确比较调用模型前的 Document.version、绑定和内容状态；保留 UUID 后迟到编辑仍必须冲突。

测试：新增 `test_reembedding.py`，扩展 `test_bases.py`、`test_governance.py`、`test_tasks.py`、`test_worker.py`。先证明当前实现丢失手工内容，再证明修复；覆盖两种模式、删空、禁用、失败重试、同维度异空间、失租/删除、迟到编辑。

验收：重嵌入前后内容/身份/启停完全一致，只有目标向量和处理代次变化；不调用 extractor 或 MinIO download。

### T3：显式原文件重新解析

依赖 T2。

- [ ] 增加 reparse-preview：服务器按 Document 权威下载原文件，共用现有 extract/clean/split；权限复核与临时文件清理完整。
- [ ] 增加 reparse 准入：expected_version、完整参数校验、ready/failed 和开放任务冲突；禁止借该接口换模型。
- [ ] 确认后将新参数固化到 Task 专用 reparse_settings，version++、入 ingest；retry 继承此次任务参数。Document 参数只在成功发布时与 Segment/Child 同事务替换，失败保持旧内容模式，不让新参数解释旧行。
- [ ] 文档维护只读投影可明确展示失败重处理后残留的“旧内容”；正常搜索仍只读当前 ready 版本，不恢复旧索引。

测试：扩展 `test_ingestion.py`、`test_governance.py`、`test_upload.py`、`test_authority.py`；参数预览/实际发布一致、CAS、下载后撤权、临时文件清理、人工文本覆盖仅发生在此操作、失败不丢旧行；general↔parent_child 重新解析失败后重嵌入，仍按已发布模式生成向量。

验收：两个按钮对应不同数据来源和任务语义，不能只靠不同文案区分。

### T4：真实进度和尝试状态

依赖 T2/T3。

- [ ] Task stage/计数更新验证 claim、target_version 与当前 attempt；领取新 attempt 清零，不累计旧尝试。
- [ ] embed/rerank 每个真实HTTP批次及重试前执行相应authority/lease guard，embedding成功批次另更新进度；集中在现有客户端/handler协作处，不重复外部重试，不持事务等待模型。
- [ ] Document 列表/详情返回绑定当前代次的 task_progress；queued/retry_wait/failed/done 正交，失败保留失败阶段。
- [ ] 部分批次成功不能提前 ready；最终 publishing/done 和 Task success 仍与正式内容/向量发布同事务。

测试：新增 `test_task_progress.py`，扩展 `test_models.py`、`test_worker.py`；第二批失败/撤权、429重试前撤权、取消、失租、旧进度晚到、重试归零、Project pending deletion 不消耗尝试、不泄露执行材料；rerank同样停止未派发批次。

验收：能够解释当前在做什么；没有可测总数时不显示百分比，失败不显示嵌入完成。

### T5：完整 passage、详情与引用投影

依赖 T1，可与 T2–T4 的内部实现并行，集成后一起验收。

- [ ] 搜索保留完整候选快照、document_version、content_digest 和真实 Child 命中；最终统一复核内容、权限及当前 metadata/builtin 硬过滤后生成 SearchHit。
- [ ] Agent 用完整 passage 构造正文，UTF-8 JSON 上限64KiB，整段选择；additional_kwargs 只带实际发送项的短引用，给 omitted_count，不新增第二套 LLM 计数器。
- [ ] 更新工具说明，不把排名分数说成正确性概率；保留现有错误 ToolMessage 无引用规则及宿主 Provider capacity guard。
- [ ] 新增单 Segment 详情和 Child 分页，验证资源关系和期望版本/digest；普通浏览与旧检索详情过期语义明确区分。
- [ ] Query/hit count 仍是检索结果统计，不谎称全部已发送给模型；工具单独报告发送数量。
- [ ] HTTP 普通结果不泄露 passage；debug 按最终引用投影逐hit安全诊断、真实matched_children和局部分数，不返回Child/未入选候选正文。历史 Citation 缺新字段依然可显示短引用，不反推旧来源。

测试：新增 `test_search_details.py`，扩展 `test_retrieval.py`、`test_agent_tool.py` 和前端 citation 单测。覆盖答案在320字符后、4000字符中英文段、全包字节预算、结果/引用一致、同ID内容改动、重嵌入/重解析后旧详情、跨项目及失权；rerank等待期间批量赋值、字段改名/删除及文档改名不得漏过最终过滤。

验收：模型可读正文与界面摘要分离；测试/Agent 排名相同，只有预算投影不同。

## 阶段 C：元数据与检索核心

### T6：字段发现与批量赋值

依赖 T1。

- [ ] 实现 builtin/custom 字段发现与类型/操作说明，内建值从 Document 权威动态投影，禁止伪造 uploader 历史。
- [ ] metadata filter 增加 field_kind，默认 custom；未知 custom 字段仍按不匹配，AND/max10/类型保护不变；全部召回路复用同一个过滤构造。
- [ ] 实现同库最多100文档、20字段的共同 patch，未传保留/null清空；全事务回滚，不用逐文档提交。
- [ ] 统一字段增改删与单/批量赋值锁序，防止 rename/delete 后旧字段回流；元数据变更不触发 embedding。
- [ ] 增加绑定 Project authority 的只读 knowledge_metadata_fields 工具，仅返回字段定义，不扫描值、不添加写能力；超过发现总量明确提示缩小库范围。

测试：扩展 `test_metadata.py`、`test_retrieval.py`、`test_agent_tool.py`、`test_authority.py`。覆盖内建同名冲突、禁止写 builtin、时间/number 类型、批量越权全回滚、并发字段改名、工具缺能力与旧字段定义后续失效。

验收：用户和 Agent 能发现允许过滤的字段；批量维护无需逐文档操作，也不会误清空未编辑字段。

### T7：分库预算、分数来源与诊断基础

依赖 T5/T6。

- [ ] 按设计公式计算每库/每路 C 和全局400父段预算，保留同 Embedding query vector 复用；Child 先回卷去重，不先用原始子块占满父段预算。
- [ ] 捕获每次搜索的有效模型/参数快照；模型调用前与结果返回前复核，不能将途中改变的绑定混为一次结果。
- [ ] 原生局部分数/阈值与最终 ranking_score 分开；先保持单一比分域的 semantic 原生语义，准备融合分及 Query provenance 字段。
- [ ] safe debug 记录实际计数/阶段耗时和空结果原因；模型/数据库失败保持错误而非空成功。检索正文不写诊断表或普通日志。
- [ ] 取top_k后统一最终复核，stale剔除不补位；计数/日志/空原因与实际返回一致，不能为补齐top_k再次调用模型。

测试：新增 `test_search_ranking.py`，扩展 `test_retrieval.py`。覆盖大库+小库、同 E 不同 R、同 R 不同 E、NULL R、全局预算、负 cosine、阈值0/正值、共享名次与稳定破同分、交换输入顺序、stale剔除不补位、绑定变更、日志最高分及类型同源。

验收：候选预算不再由同组大库独占；原始分数不丢失，也不会被错误解释成融合分。

### T8：词法索引、hybrid 与最终融合

依赖 T7，和 T2/T3 内容发布路径联调。

- [ ] 实现包内 lexical_v1 纯规范化/词元生成，固定中文二元词元、英文/数字、完整业务标识符/IP 规则及方案约定的字节上限、长词元哈希；词元安全编码，原文保持原样。
- [ ] 用 PostgreSQL simple tsvector + GIN、参数化 OR tsquery、固定 ts_rank_cd 规则实现词法路；不声称 BM25、不依赖数据库中文扩展或下载词典。
- [ ] 正式入库、人工增改、Child 重切、reparse 同事务维护词法字段；parent_child 的父 Segment 仍生成派生字段用于统一排序，reembed 不改内容或词法。用真实 PostgreSQL 证明索引表达式/ORM快照一致。
- [ ] Base 默认 semantic，显式 hybrid 才增加词法候选；检索测试可覆盖本次模式，不能将临时选择保存到 Base。
- [ ] 两路按父段 RRF 合并；无 Reranker 的词法新增项仍计算 cosine 并应用原阈值，parent_child 取全部当前Child最大cosine，不能用NULL父向量或只用词法命中Child。
- [ ] 按方案第8.3节接通统一 Reranker、同域原生、异域/无统一Reranker融合三分支；不新增模型选择配置、不按UUID隐式挑模型。
- [ ] 有hybrid库且需融合时，所有入围父段统一计算词法排序证据，不偏向词法top C；全semantic不执行词法查询/128词元限制，不收紧原query长度契约。
- [ ] 固定词元识别顺序、重复/位置规则及输入→词元→排名快照；使用RANK共享名次，原生阈值先于最终域内/词法排名，同分不由输入顺序改变。
- [ ] 为纯语义异域无词法证据返回明确质量限制；策略常量只在评测后统一修订，不临时增加权重调参面板。

测试：新增 `test_lexical_retrieval.py` 和纯 tokenizer 测试；覆盖中文无空格、IP、接口名、错误码、大小写/全半角、标点注入、超长词元/查询、零词元、禁用/删除/metadata、同父多Child、词法索引随编辑更新及真实 query plan。

验收：功能性验证与 T14 质量验证均通过才完成 F09/F10；不是“SQL能返回结果”就宣布效果提升。

## 阶段 D：前端工作区

### T9：URL 状态、文档搜索和分页

依赖 T1；可在后端最终契约冻结后准备，不先发布。

- [ ] 新增 navigation 纯解析/构造函数，保留现有 Project shell/client；URL 字段白名单为 kb/view/doc/segment/status/sort/page。
- [ ] 用 push 表示资源前进/返回、replace 表示筛选/分页；文件关键词和检索/metadata内容不进URL或持久浏览器存储。
- [ ] 在完整权威列表上关键词/状态过滤、稳定排序及20条分页；不能把默认500配额当成不检查分页完整性的理由。
- [ ] 修正listAllPages提前空页/触顶仍成功返回部分数据的分支：未满足total时返回明确不完整错误，不用部分列表进行全量筛选或覆盖已有完整缓存。
- [ ] 当前页选择、切页/筛选清空、删除末页回退；恢复列表关键词只限当前scope内状态。
- [ ] 直达Segment通过详情读取定位，不遍历整个库；跨库ID组合/已删资源不得恢复旧对象。

测试：新增 navigation/document-list 单测，扩展 `document-cache-authority.test.ts`、Project scope 测试及 mock E2E。覆盖超过100条跨后端页筛选、提前空页/触顶不完整、刷新、浏览器前后退、非法UUID/页码、相同slug不同Project、失权/删除和乱序读取。

### T10：预览文件选择

依赖 T1。

- [ ] 向导选择任一已选 File，切换时仅自动预览一次；参数编辑标stale，显式刷新，不实时重复上传文件。
- [ ] 预览身份含File对象、参数、scope generation和请求序号；删除/替换文件、快速A→B、重新提交旧参数时仍不能被迟到响应覆盖。
- [ ] 文件名、展示数/总数、父子关系、加载/失败/过期明确可见；失败不继续展示成当前有效预览。

测试：新增 preview identity 单测，扩展 `project-knowledge.spec.ts`；构造A慢B快、同文件参数变更、移除文件、scope切换、错误刷新。replay 验证所选文件预览与实际摄取一致。

### T11：检索结果详情和诊断

依赖 T5/T7/T8/T9。

- [ ] 结果显示本次 score_kind 与最终排名，原生阈值分、实际模型/参数/数量/耗时置于折叠诊断；不显示“可信度百分比”。
- [ ] 原分段详情和匹配Child从本次结果/受权威读取取得；过期提示重新检索，历史引用保留原摘要。
- [ ] 结果进入维护页和返回定位闭环；跨库、重建、参数变化、换Reranker后清理旧结果/详情；新请求胜出，迟到响应不能恢复旧结果。
- [ ] 从未测试、无命中、过滤为空、未ready、内容过期、模型失败分别呈现；错误在结果区持续可见并允许重试。
- [ ] 桌面并排、窄屏堆叠；键盘打开/关闭详情、焦点返回、错误关联和loading可访问性完整。

测试：扩展 citation/knowledge 单测、mock E2E；real-backend 验证parent_child真正命中非首Child、完整原段、版本变化后409、不同评分分支及无内容泄漏，不能由详情Child列表回推命中。

### T12：进度、元数据与重处理操作

依赖 T2–T4/T6/T9。

- [ ] 逐文件上传结果继续保留；汇总处理中/成功/失败/等待自动重试，不能把全部终态当成成功。
- [ ] 显示安全的真实阶段/计数/尝试，未知总数不定进度；旧attempt进度不得倒灌；deleting错误保留现有停轮询规则。
- [ ] 批量metadata只操作当前选择，显示混合值/保持/设置/清空和覆盖数量，一次共同patch；只读成员仅查看定义。
- [ ] 库设置显示独立的重嵌入操作；文档处显示重新解析操作，改参数可先预览，确认明确保留/覆盖、费用和暂不可检索范围。
- [ ] 服务端冲突保留未保存表单，刷新权威信息后让用户重新确认；401/403/404清除失权数据，不在新scope恢复旧表单。

测试：mock E2E 全失败、部分失败、重试中、迟到进度、批量混合值/全批回滚、reparse冲突；real-backend 重嵌入后UUID/文本/启停保持，reparse才替换。

## 阶段 E：验证与交付

### T13：确定性全链路与安全门

依赖 T1–T12。

- [ ] 扩展 replay Provider：支持可控分段向量、两种评分域、分批失败/延迟及调用计数。fixture 不调用外部模型，固定ID/时间。
- [ ] 临时 PostgreSQL + MinIO + replay Worker 完成：上传 → 指定文件预览 → ready → 搜索 → 原段定位 → 人工编辑/禁用 → 重嵌入保持 → 原文件重解析覆盖 → 检索验证。
- [ ] 同时覆盖字段发现/批量metadata、查询owner retention、Project删除/恢复、失租和旧响应；先明确失败归属，不扩大到其他域改造。
- [ ] 单独验收 Agent真实工具装配与 ToolMessage 历史引用、正文尾部答案、budget omitted_count、已有 Provider guard，没有新增权限或明文材料泄漏。
- [ ] 实际执行数量/失败/跳过逐项记录，不将mock浏览器当真实后端，也不将replay模型当真实质量。

候选执行命令（从标注目录运行；只在具备对应测试前提时执行）：

```bash
# backend/：核心runner只在随机隔离库执行测试DDL
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/ -q -m "not provider_integration"
make test
make lint
uv run python scripts/generate_schema_comments.py --check

# frontend/：不同Playwright模式分开
pnpm check
pnpm test
pnpm exec playwright test tests/e2e/project-knowledge.spec.ts
pnpm exec playwright test --config playwright.real-backend.config.ts tests/e2e-real-backend/knowledge-real-backend.spec.ts
pnpm build:production
```

实现变更还须按 owning guide 执行格式化和受影响的边界门。`make check-db` 是指定目标的只读 readiness，不代替空库 Schema 契约；不把 `make reset-db` 放进普通测试/启动命令块。临时服务使用现有 replay 测试启动方式，不能指向用户业务库试错。

### T14：真实质量、性能与最终文档

依赖 T13。

- [ ] 提交脱敏父段三级标注样本、稳定来源及计算方法；至少60条，分层冻结开发集与独立验收集（至少30题，其中标识符至少20题），覆盖设计方案第11节类别。真实模型调用事先确认使用条件和预算。
- [ ] 记录M9/M10、semantic/hybrid、同域/异域、无词法证据四类对照；按方案阶段分别报告实际候选数、Recall@candidate、最终Recall@10、nDCG@10、P95、调用条目数和费用；无答案IDCG=0样本单报误召回，不混入普通均值。
- [ ] 执行设计方案门槛，特别是标识符候选/最终Recall均≥95%、自然语言召回/nDCG及无答案回归限制、P95回归复审。纯语义异构域若变差，不能用“秩融合已统一分数”解释为通过。
- [ ] 调参只用开发集；验收失败须记录原因、修正并重新组织独立验收，不能用同一验收集反复选参数。若要取消/延后F09或F10，须先修订方案范围并取得确认，不能自行标记M10完成。
- [ ] 更新 README、Install、backend/frontend AGENTS、知识库需求/架构/摄取/检索文档及总计划；明确重嵌入/重解析、模型正文/短引用、原生阈值/最终排序、真实进度与部署约束。需要新增领域词条时按仓库词汇流程处理，不恢复旧 Knowledge Model Configuration。
- [ ] 确认操作者目标库、停服/数据处置及新版本Schema readiness；本计划本身不是reset授权。旧数据必须保留且无受支持升级路径时，部署仍阻塞。

## 依赖与并行安排

- T0 → T1 是共同前置。
- 内容保护链：T2 → T3 → T4；T5、T6可在契约冻结后并行准备，避免共同修改contracts/Schema时冲突。
- 检索链：T5/T6 → T7 → T8；与T2/T3在同一发布点联调词法字段。
- 前端纯状态工作T9/T10可与后端并行；T11等待检索最终DTO，T12等待任务/重处理/metadata契约。
- T13 → T14 为交付门；整个M10前后端与Schema同步发布，不能把任何“暂时只有后端”的中间态交付用户。

## 完成清单

- [ ] F01–F10逐项有实现与验收证据，已具备的M8/M9能力未被重复实现或退化。
- [ ] 没有模型正文截断、人工内容意外重建、旧版本向量写回、跨项目/owner泄漏。
- [ ] 排名/阈值/日志/历史/UI语义一致；融合分不是置信概率，质量限制可见。
- [ ] 所有确定性门通过，真实质量/性能门有有效结果；缺权限/数据时明确未完成。
- [ ] Schema、DTO、装配、模型引用、retention、文档一致，已有数据库处置已单独确认。
- [ ] M10总体状态最后才从“计划中”更新为“已完成”。

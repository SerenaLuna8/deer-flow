# RAG 知识库需求文档

> 本文整合自原《RAG知识库系统需求文档》与《RAG知识库MVP执行计划》中的需求性内容,
> 描述 M0–M10 全部交付后的当前功能需求。架构与实现细节见
> [《RAG知识库设计文档》](RAG知识库设计文档.md)。功能行为以代码与
> `backend/tests/knowledge/` 聚焦测试为准。

## 1. 产品目标

用户可以在 Project 中:

1. 创建 Knowledge Base,选择 Embedding 模型、可选 Reranker 与检索模式;
2. 上传文档、实时预览分块效果并查看真实处理进度;
3. 治理文档与 Segment:启停、编辑、重命名、批量操作与元数据维护;
4. 对失败文档重试,对已发布内容重嵌入,或按新参数重解析原文件;
5. 测试知识检索,查看最终排名、分数来源、命中详情与安全诊断;
6. 让 Agent 在回答时搜索当前 Project 的知识库;
7. 查看回答引用的 Knowledge Document 并定位原文;
8. 下载原始文件,删除文档或知识库。

管理员在宿主模型注册表中维护 Model Provider 与 Embedding/Reranker 模型。

## 2. 功能范围

### 包含

- 独立 `actweave-knowledge` Python Package;
- 宿主模型注册表(`model_providers` / `model_provider_models`),SiliconFlow
  `/embeddings` 与 `/rerank` 契约;
- MinIO 对象存储;
- PDF、DOCX、TXT、Markdown、CSV、XLSX、HTML(`.html`/`.htm`)、PPTX、EPUB;
- 递归分隔符切分、预处理规则(压缩空白、删 URL/邮箱)、general/parent_child
  两种分块模式、Gateway 同步分块预览;
- pgvector exact cosine 候选召回;库级可选 Reranker 精排;
- 库级检索模式 semantic/hybrid(`lexical_v1` 词法路 + RRF 合并,请求级可单次覆盖);
- 分库候选预算与三分支最终排序(原生排序或异构域秩融合),`score_kind` 标注分数来源;
- 元数据字段定义、单文档与批量赋值、检索过滤(eq/contains/gte/lte);
- 检索最低相关度阈值;库级默认 `top_k` 与 `score_threshold`;
- 检索安全诊断(`debug`)、单段详情、命中钉住与文档定位;
- Agent `knowledge_search` 与 `knowledge_metadata_fields` 工具;引用返回
  64KiB 预算内完整原文正文;
- Segment 启停/编辑/手工新增/删除;文档启停/重命名/批量启停删除;
- 文档列表关键词搜索、状态过滤、排序与分页;
- 失败重试、库级重嵌入(rebuild)、文档级原文件重解析(reparse);
- 查询日志与分段/文档命中统计(owner 私有);
- 异步摄取、异步删除、索引任务真实进度;
- 原始文件下载;
- Knowledge Base 数量、Document 数量和单文档向量条目的基础配额。

### 不包含

- 网页抓取、URL/Notion 导入、OCR、音视频转写、多模态图片段;
- QA 模式与摘要索引;
- 文档内容去重、embedding cache;
- HNSW 等向量索引(召回保持 exact cosine);
- 历史检索回放、外部知识库 API;
- 多 Provider 插件体系(实现直接面向 SiliconFlow 双接口契约);
- 库级 only_me 权限与标签、文档暂停/归档、RAG Pipeline 工作流摄取。

## 3. 核心对象

### Model Provider 与 Provider Model(宿主模型注册表)

管理员维护的检索模型来源,由宿主 `app/model_registry/` 持有,不属于 Knowledge Package:

- `model_providers`:名称、Base URL、请求超时、行内加密的供应商级 API Key;
- `model_provider_models`:`model_type ∈ embedding|rerank`、模型名、维度
  (embedding)、批量上限、`active|disabled`。

规则:模型行所属 Provider、类型、名称、维度建后不可变;被 Knowledge Base 引用的
模型不可停用或删除;有子模型的 Provider 不可删除;存在被引用 embedding 模型时
Provider 的 `base_url` 冻结,换端点必须新建 Provider/模型并显式重建。原
Knowledge Model Configuration 已于 M9 退役。

### Knowledge Base

Project 内的文档集合:

- `name`(Project 内唯一)、`description`、`status=active|disabled|deleting`;
- `embedding_model_id` 必填;`reranker_model_id` 可选,换绑/解绑即时生效,不触发重建;
- `retrieval_mode=semantic|hybrid`(默认 semantic);
- 库级默认 `default_top_k` 与 `default_score_threshold`,检索测试与 Agent 工具
  未传参时生效;
- 更换 Embedding 模型必须走重建(rebuild):同步换绑后对已发布文档逐个重嵌入。

`KnowledgeBaseView` 还包含 `document_count` 和可选 `delete_error`:没有 open
Base 删除 Task 时才从最近一次最终失败的删除 Task 派生。

### Knowledge Document

上传到一个 Knowledge Base 的文件:

- 展示名称与原始文件名、MinIO object key、文件大小、媒体类型;
- 8 个切分参数,上传时一次性冻结:`chunk_size`、`chunk_overlap`、
  `chunk_separator`、`remove_extra_spaces`、`remove_urls_emails`、
  `chunking_mode(general|parent_child)`、`child_chunk_size`、
  `child_chunk_separator`;
- 当前 `version`、处理状态与错误信息、Segment 数量、字数统计;
- `enabled` 启停开关(停用不删向量,重新启用即恢复可检索);
- `doc_metadata`(JSONB)保存元数据字段值。

状态:

```text
uploading -> queued -> processing -> ready
                                -> failed
任意非删除状态 -> deleting -> 物理删除
```

`KnowledgeDocumentView` 同样含可选 `delete_error`;处理中的文档展示真实阶段、
已验证批次计数与尝试次数。

### Knowledge Segment 与 Segment Child

从一个 Document version 生成的有序文本块,保存正文、位置、来源位置、启停状态、
字数与 `content_digest`:

- general 模式:Segment 平铺并直接携带向量;
- parent_child 模式:父 Segment 承载返回内容,子块
  (`knowledge_segment_children`)承载向量,命中按父块内最高子块分回卷去重;
- 词法两列 `lexical_tsv`/`lexical_version` 与内容写入同事务维护。

### Knowledge Metadata Field

库级元数据字段定义(string/number/time)。字段重命名/删除在同一事务改写全部
文档键;内建字段只读,通过字段发现接口与自定义字段一起返回。

### Knowledge Query

每次有可搜索 Base 的完成检索,把原始 query 记录到当前可信用户自己的历史
(来源 `retrieval_test|agent`)。最近查询只能由同一用户读取,不作为 Project
共享内容向其他成员或管理员展示。

### Knowledge Task

后台执行摄取、重嵌入与删除的持久任务。Task 最多尝试 3 次,失败后允许用户再次
触发;索引任务(摄取/重嵌入)携带真实进度且仅投影当前文档代次。
`delete_document_object` 使用不依赖 Document 外键的精确 `storage_key`,可在原
Document 行已消失时继续清理,并可与普通 Document 删除 Task 并存。

### Knowledge Citation

一次检索命中的来源:Base、Document、Segment、snippet、`score`、
`score_kind(cosine|rerank|rank_fusion)`、`local_score`(原生分)、
`document_version`、`content_digest`、页码/行号等来源位置,以及父子模式下的
真实 `matched_children`。

## 4. 功能需求

### FR-01 检索模型管理(管理员)

管理员在系统设置的模型注册表中:

- 新建/编辑/删除 Model Provider(名称、Base URL、请求超时、API Key);
- 在 Provider 下新建/编辑/停用/删除 Embedding 或 Reranker 模型
  (类型、模型名、维度、批量上限);
- 按模型类型执行连接测试(`/embeddings` 或 `/rerank`);
- Key 与超时可修改,遵循"冻结材料 → 事务外探活 → 重新锁定复核并提交";
- 允许的 `base_url` 更新必须重新提交 API Key;
- 被引用模型不可停用/删除;有子模型的 Provider 不可删除。

### FR-02 Knowledge Base 管理

Project 用户可以:

- 创建 Base:名称、描述、必选 embedding 模型、可选 reranker、检索模式;
  无 reranker 不阻止建库;
- 查看 Base 列表和详情;
- 编辑名称、描述、状态、库级默认 `top_k`/分数阈值、检索模式;换绑或解绑
  Reranker(即时生效);
- 重建(rebuild):换绑 embedding 模型并对已发布文档重嵌入;
- 删除 Base。

规则:

- 同一 Project 内 Base 名称唯一;
- Project 内 Base 数量达到 `max_knowledge_bases_per_project`(默认 20)时创建
  返回 `KNOWLEDGE_QUOTA_EXCEEDED`;
- disabled Base 不参与检索,也不接受上传和文档重试;仍允许编辑、读取、删除;
- deleting Base 不再接受上传、重试和检索。

### FR-03 上传文档

- 支持单文件上传;批量上传由前端逐文件调用;
- 默认最大文件大小和配置硬上限均为 50 MiB;
- 所有合法文件强制单 PUT,每个对象存储实例只执行一个并发 PUT,以限制 MinIO SDK
  的整 part 内存,并禁止产生普通对象列表和删除接口不可见的 incomplete
  multipart upload;
- 允许扩展名:`.pdf`、`.docx`、`.txt`、`.md`、`.csv`、`.xlsx`、`.html`、
  `.htm`、`.pptx`、`.epub`;
- 每次上传创建新的 Knowledge Document,不做内容去重;
- 切分参数(8 项)按 Document 在上传时设置并一次性固定;普通重试沿用原参数,
  调整参数走显式重解析入口(FR-05);
- 创建向导提供同步分块预览:选择预览文件后按当前参数走
  抽取→清洗→切分,不写库不入队,防抖刷新并隔离迟到响应,与实际摄取所见即所得;
- 只有 active Base 接受上传;Base 内 Document 数量达到
  `max_documents_per_knowledge_base`(默认 500)时返回 `KNOWLEDGE_QUOTA_EXCEEDED`;
- 上传期间并发删除必须获胜;即使删除 Worker 先移除 Document 行、put 随后完成且
  即时对象清理失败,也必须保留携带精确 storage key 的 `delete_document_object`
  任务,Base 尚存时恢复 deleting tombstone,不能产生无行无任务孤儿;
- Gateway 先把请求写入单次请求临时文件并校验大小;成功、失败或取消后删除。

### FR-04 摄取

摄取顺序固定为:

```text
从 MinIO 下载临时文件 -> 提取文本 -> 清洗 -> 切分 -> 批量 embedding -> 发布
```

- TXT/Markdown 解码顺序:UTF-16 BOM → UTF-8 → GB18030;
- PDF 保留页码;CSV/XLSX 保留 sheet 和行号;PPTX 保留 slide;EPUB 保留
  chapter;其他格式来源位置可以为空;
- 空文本进入 `failed`;
- `max_segments_per_document` 是单文档向量条目预算,默认值和可配置硬上限均为
  5000:general 模式按 Segment 数量计,parent_child 模式按携带向量的 Child
  数量计;超限时在调用 Embedding 前进入 `failed` 并说明超限。同一预算适用于
  手工 Segment 新增/编辑:Embedding 前检查一次,Provider 返回后持 Document 锁
  再次检查;
- embedding 返回数量、维度、有限数值或非零校验不通过时进入 `failed`;
- 成功发布时,同一事务替换该 Document version 的 Segment 与向量,并同事务维护
  词法两列;
- 摄取与重嵌入的 embedding 循环在每个 Provider 批次前重检查权限与 lease;
- 旧 version 的任务不得覆盖新 version 或已删除的 Document。

### FR-05 失败重试与重新处理

- 系统任务自动尝试不超过 3 次;耗尽后 Document 显示 `failed` 和错误信息;
- 重试:递增 Document version 并创建新任务,要求所属 Base 处于 active;继承
  最近一次索引任务的类型与冻结的重解析参数;
- 重嵌入(库级 rebuild):保留 Segment UUID、文本、位置、启停与人工编辑,仅
  重算向量并整版翻转;未发布文档在准入时跳过并报真实接受/跳过计数;词法两列
  逐字节不动;
- 重解析(文档级):`reparse-preview` 服务端预览 + `reparse` 按
  `expected_version` CAS 提交;可修改全部 8 个切分参数,参数冻结在任务上,
  发布成功时写回文档行并整体替换分段行(人工编辑与启停被覆盖);这不是模型
  变更入口。

### FR-06 文档治理与列表

- 文档重命名;单文档与批量启停、批量删除(有界批量,一次全量成功或回滚);
- 停用的文档不参与检索候选,不删除向量;
- 文档列表支持关键词搜索、状态筛选、排序与分页;工作区状态入 URL,不将业务
  内容写入 URL 或持久浏览器存储。

### FR-07 Segment 治理

- ready 文档上支持 Segment 启停、编辑、手工新增与删除;
- 内容编辑与手工新增在写事务前用所属 Base 的模型重算 embedding;写事务重查
  Document version,竞争失败返回 `KNOWLEDGE_CONFLICT`;parent_child 模式编辑/
  新增会重切子块;
- 单段详情:权威单段读取,Child 分页;调用方期望的 `document_version`/
  `content_digest` 漂移时返回 409(`KNOWLEDGE_CONFLICT`);
- 分段与文档保持字数统计。

### FR-08 元数据

- 库级字段定义管理:string/number/time 三类;字段重命名/删除同事务改写文档键;
- 单文档元数据编辑;同库多选批量保持/设置/清空,只写显式提交的字段,一次全量
  成功或回滚;
- 字段发现返回内建与自定义定义(含 `field_kind`),内建字段只读;
- 检索过滤:eq/contains/gte/lte 条件按 AND 组合,上限 10 条,提交前按库内字段
  定义校验。

### FR-09 检索测试

用户可从 Project Knowledge 页面输入 query,选择一个或多个 active Base:

- 未选择 Base 时默认搜索当前 Project 全部 active Base;
- query strip 后不能为空,且不超过 2000 字符;
- `top_k` 范围 1..20,未传时使用各库级默认;`score_threshold` 范围 0..1,
  0 表示不过滤(含负分),未传时使用各库级默认;
- 有 Reranker 的库:最终分为其相关性分(`[0,1]`),Reranker 失败即整次失败,
  不静默退回 cosine-only;无 Reranker 的库:最终分为余弦相似度(`[-1,1]`);
- 检索模式取库级 `retrieval_mode`,请求可单次覆盖 semantic/hybrid,不落库;
- 结果展示最终排名与分数来源(Cosine/Rerank/秩融合)、实际参数、候选计数与
  耗时等安全诊断、命中详情钉住与文档定位闭环,以及六类空/错误态区分;
- 每次有可搜索 Base 的完成检索把原始 query 记录到当前可信用户自己的历史;
- 搜索在 Provider 工作前后重验证成员关系与 `shared_assets.read`;中途撤权时
  不得返回已计算的 Citation,也不得写入查询历史或命中计数。

### FR-10 Agent 搜索

Knowledge 功能启用时,Lead Agent 获得:

```text
knowledge_search(query, top_k=None, metadata_filters=None)
knowledge_metadata_fields()
```

- Project id 与查询历史 owner 由宿主当前 Run 的可信上下文提供,不暴露为模型
  工具参数;
- `top_k` 省略时使用各库级默认;不向模型暴露 `score_threshold`;
- `metadata_filters` 与 HTTP 检索同一套 eq/contains/gte/lte 语义;
  `knowledge_metadata_fields` 只读返回字段定义,不返回值;
- 搜索当前时刻 active 的 Knowledge Base 和 ready Document;
- 引用在 64KiB UTF-8 JSON 预算内装包完整原文正文,装不下的命中计入
  `omitted_count`;
- 没有命中或全部候选低于阈值时返回空结果,而不是报错;模型或数据库调用失败
  返回明确的工具错误;
- 工具成功时把结构化引用写入 ToolMessage 的
  `additional_kwargs.knowledge_citations`;消息投影按同一 Run 把引用附到最终
  Agent 消息,按 `segment_id` 去重,实时消息和刷新 replay 使用同一逻辑。

### FR-11 删除

- 删除 Document 后不再参与检索,并异步删除其 MinIO 对象、Segment 和向量;
- 删除 Base 后不再接受操作,并异步删除其全部 Document 和 MinIO 对象;
- Project 删除前调用 `purge_project` 清理该 Project 的 Knowledge 数据:先按
  Document 行执行对象优先清理,再清扫数据库签发的 Knowledge Project 对象
  prefix,最后删除其余 Knowledge 行;MinIO 列举或删除失败时不得继续最终
  Project 删除;
- Project purge 遇到近期 uploading 行时,本轮不得删除任何 Knowledge 对象或
  关系行;超过一天 settlement grace 的遗留上传仅转为 deleting 并创建
  exact-key 清理任务,下一轮才执行清理。时效判断使用 PostgreSQL 时钟;
- MinIO bucket 必须关闭 versioning 和 Object Lock。启动健康检查以及
  MinIO-backed 删除路径在删除对象前读取 bucket versioning;Enabled、
  Suspended 或缺少 GetBucketVersioning 权限均失败关闭;
- `knowledge.enabled=false` 只停用路由、Agent 工具和 Knowledge Task worker,
  不停用独立 Project purger。未配置 MinIO 时,只要仍有 Document 行或状态不是
  `succeeded` 的 `delete_document_object` Task,Project purge 必须返回未完成;
- 删除任务失败时自动重试;最终失败时资源保持 `deleting` 并显示 Task 错误。
  用户再次触发删除后,新 Task 处理期间不再显示旧错误。

### FR-12 页面

Project Knowledge 页面包括:

- Base 列表、创建向导(模型选择、切分参数、实时分块预览)、"创建空知识库"
  独立入口、编辑和删除;
- Base 详情与文档工作区:列表搜索/过滤/排序/分页、上传、状态与真实进度、
  错误、重试、重解析、启停、重命名、批量操作、元数据维护、原文下载、删除;
- 分段浏览页:Segment 列表、启停、编辑、新增、删除与单段详情;
- 库设置:名称/描述/默认检索参数/检索模式/Reranker 换绑/重建确认;
- 检索测试面板:参数覆盖、命中列表与分数来源、命中钉住、文档定位、诊断展示、
  当前用户最近查询回填;
- 处理中的自动刷新。

管理员系统设置页提供 Model Provider 与模型的管理和连接测试。聊天消息中的
Citation 可展开显示 Base、Document、snippet、score 和来源位置,并可跳转定位。

### FR-13 原文下载

- 用户可以下载 Knowledge Document 的原始文件,响应使用原始文件名和记录的
  媒体类型;
- 仅 `queued|processing|ready|failed` 状态可下载;`uploading` 和 `deleting`
  返回 `KNOWLEDGE_INVALID_REQUEST`;
- MinIO 对象缺失时返回 `KNOWLEDGE_STORAGE_UNAVAILABLE`;
- Gateway 先把对象下载到单次请求临时文件再返回,响应结束后删除临时文件。

## 5. API 范围

### Project API

全部位于 `/api/projects/{project_id}/knowledge` 前缀下:

```text
GET        /model-options
GET        /health
GET/POST   /bases
GET/PATCH/DELETE /bases/{base_id}
POST       /bases/{base_id}/rebuild
GET/POST   /bases/{base_id}/documents
GET        /bases/{base_id}/documents/{document_id}/segments/{segment_id}
GET        /bases/{base_id}/queries
GET/POST   /bases/{base_id}/metadata-fields
PATCH/DELETE /metadata-fields/{field_id}
GET        /filter-fields
POST       /chunk-preview
GET/PATCH/DELETE /documents/{document_id}
GET        /documents/{document_id}/download
POST       /documents/{document_id}/retry
POST       /documents/{document_id}/reparse-preview
POST       /documents/{document_id}/reparse
PATCH      /documents/{document_id}/metadata
PATCH      /bases/{base_id}/documents/metadata
POST       /documents/batch-status
POST       /documents/batch-delete
GET/POST   /documents/{document_id}/segments
PATCH/DELETE /segments/{segment_id}
POST       /search
```

`model-options` 从宿主模型注册表返回 active 模型供创建/换绑选择。列表 API 使用
普通 `page` 和 `page_size`。

### Admin API

检索模型管理并入系统设置,位于 `/api/admin/settings` 前缀下:

```text
GET/POST   /model-providers
PATCH/DELETE /model-providers/{provider_id}
GET/POST   /model-providers/{provider_id}/models
PATCH/DELETE /provider-models/{model_id}
POST       /provider-models/{model_id}/test
```

### 现有能力映射

- `shared_assets.read`:Knowledge 页面、列表、详情、Segment 浏览与详情、
  原文下载、`model-options`、字段发现、最近查询、health 和检索测试;
- `shared_assets.edit`:Base/Document/Segment/元数据的创建、修改、上传、
  重试、重建、重解析和删除;
- `shared_assets.execute`:Agent Run 注入和调用 `knowledge_search`;
- system admin:模型注册表管理。

不新增 `knowledge.*` 能力。成员缺能力返回 403 `KNOWLEDGE_FORBIDDEN`;
Project 外调用者与缺失资源一律 404。

## 6. 基础错误码

```text
KNOWLEDGE_DISABLED
KNOWLEDGE_FORBIDDEN
KNOWLEDGE_NOT_FOUND
KNOWLEDGE_NAME_CONFLICT
KNOWLEDGE_INVALID_REQUEST
KNOWLEDGE_CONFLICT
KNOWLEDGE_QUOTA_EXCEEDED
KNOWLEDGE_MODEL_UNAVAILABLE
KNOWLEDGE_STORAGE_UNAVAILABLE
KNOWLEDGE_PARSE_FAILED
KNOWLEDGE_EMBEDDING_FAILED
KNOWLEDGE_RERANK_FAILED
KNOWLEDGE_SEARCH_FAILED
KNOWLEDGE_TASK_FAILED
```

错误响应包含 `code` 和可展示 `message`,不设计字段路径协议或错误摘要协议。
`KNOWLEDGE_CONFLICT` 用于版本/digest 漂移、检索中途模型改绑、词法行未派生等
并发与一致性冲突。

## 7. 配额与限制

| 项 | 值 |
| --- | --- |
| `max_knowledge_bases_per_project` | 默认 20 |
| `max_documents_per_knowledge_base` | 默认 500 |
| `max_segments_per_document`(向量条目预算) | 默认与硬上限 5000 |
| `upload_max_bytes` | 默认与硬上限 50 MiB |
| query 长度 | ≤ 2000 字符 |
| `top_k` | 1..20,默认 4(库级默认可配) |
| 元数据过滤条件 | ≤ 10 条(AND) |
| 词法查询词元(去重后) | ≤ 128,超出显式拒绝 |
| `chunk_size` | 200..4000,默认 1000 |
| `chunk_overlap` | 0..500 且小于 `chunk_size`,默认 100 |

## 8. 验收场景

1. 空库初始化后,管理员可在系统设置创建 Model Provider、添加 Embedding 与
   Reranker 模型并分别通过连接测试。
2. 用户创建 Knowledge Base(必选 embedding、可选 reranker、检索模式),
   并上传九种支持格式。
3. 创建向导分块预览与实际摄取结果逐字节一致。
4. Worker 将文档处理为 `ready`,页面显示 Segment 数量与处理中的真实阶段/
   批次/尝试。
5. 检索测试:mock Reranker 改变候选顺序,结果按最终分排序,`score_kind`
   正确标注来源;低于阈值的候选不出现,全部低于阈值返回空结果。
6. hybrid 库对业务标识符类 query 通过词法路命中;semantic 库行为不变。
7. Agent 调用 `knowledge_search` 返回 64KiB 预算内完整正文引用,回答显示
   Citation,刷新页面后仍在。
8. 元数据过滤只命中匹配文档;字段重命名后旧键同事务改写。
9. 重嵌入后人工编辑、手工新增、启停状态保持;重解析按新参数整体替换分段。
10. Segment 编辑后按新内容命中;并发冲突返回 `KNOWLEDGE_CONFLICT`。
11. 损坏文件进入 `failed` 后可以删除;mock Provider 持续失败至三次自动尝试
    耗尽,恢复后用户重试成功。
12. 删除 Document 后搜索不到其内容且 MinIO 对象被删除;删除 Base 和 Project
    后不残留对应 MinIO 对象或 Knowledge 数据。
13. 用户下载 ready 或 failed Document 得到与上传一致的字节;uploading/deleting
    不可下载。
14. 超过 Base/Document 数量配额的创建/上传返回 `KNOWLEDGE_QUOTA_EXCEEDED`。
15. `knowledge.enabled=false` 时不显示 Knowledge 导航,也不向 Agent 注入工具。
16. 与知识库内容无关的 query 在检索测试和 Agent 工具都返回空结果;检索期间
    撤权时不返回已计算结果、不写查询历史。

## 9. 宿主既有约束

- Gateway 继续使用现有 Project 上下文和能力判断。Knowledge 的启用、Worker、
  上传、配额和 MinIO 参数来自根 `config.yaml`,由 `backend/app/knowledge/`
  校验后传给 Package,不进入 System Runtime Settings。
- 检索模型与凭据由宿主模型注册表(PostgreSQL)持有,API Key 使用宿主
  `SecretKey`/`SecretEnvelope` 行内加密,只写不读,不进入日志与响应。
- 本机 MinIO S3 API 为 `127.0.0.1:9000`,Console 为 `http://127.0.0.1:9001`;
  程序 endpoint 必须使用 S3 API 地址。启用前由管理员创建 `actweave-knowledge`
  bucket;Runtime 不自动建 bucket 或修改其策略。bucket 必须关闭
  versioning/Object Lock,凭据必须允许 `GetBucketVersioning`、对象读写删除和
  Knowledge Project prefix 列举;Gateway/Worker 启动及所有 MinIO-backed 删除
  路径据此失败关闭。
- Gateway/Worker 位于同一宿主机时可使用 `127.0.0.1:9000`;分布在不同主机时
  必须配置两个进程都可达的 S3 API 地址。

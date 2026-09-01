# Fluva

Weave intelligence into action.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](./backend/pyproject.toml)
[![Node.js](https://img.shields.io/badge/Node.js-22%2B-339933?logo=node.js&logoColor=white)](./Makefile)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

Fluva 是一个面向多账户、多项目协作的全栈 Super Agent 系统。它以
LangGraph Agent harness 为执行核心，提供项目级权限、Agent/Skill/MCP 资产、长期
Memory、Sub-Agent、Sandbox、Automation、外部 Channel 和可恢复的持久化会话。

系统采用 project-first 架构：Gateway 负责认证、授权和 Run 准入，Worker 独占
Agent graph 执行，Scheduler 只负责到期 Automation 准入；PostgreSQL 保存应用状态、
运行记录和受治理的资产版本。

> 当前代码线源自 DeerFlow 2 的重写，与最初的 Deep Research 实现不共用代码。
> 原始版本见 ByteDance 上游 [`main-1.x`](https://github.com/bytedance/deer-flow/tree/main-1.x)。

## 核心能力

- 多账户、多项目工作区，包含成员、角色、邀请、配额、审计和通知。
- 项目私有 Thread/Run、持久化 SSE、断线恢复、取消、重试和文件交付。删除会话会
  立即撤销该会话仍在运行的服务端执行权限并结束其 durable stream；已经发出的外部
  操作无法由平台召回。删除后的会话、checkpoint、既有文件、制品和 Run 记录继续保留，
  普通会话接口不再显示或读取它们；保留文件继续占用存储配额，直到项目、账号或原成员
  retention 执行最终清理。会话输入框选择、粘贴或拖入附件后会立即在后台预上传；发送
  消息时复用同一上传结果，不会重复上传。已经持久化的完整回复不会因运行时或沙箱
  回收失败被改写为 Agent 执行失败；此类回收问题作为 Worker 运维错误重试和记录。
  Agent 图执行步数耗尽时明确显示“已达到图执行步数上限”，保留具体失败原因和已有
  过程输出；当前结果可能不完整，界面不提供直接重放入口，以免重复已经执行的操作。
  每次模型调用的思考内容可独立折叠；思考、过程输出和工具调用按模型调用顺序逐项展示，
  普通执行过程不再使用“查看其他 N 个步骤”或“执行过程 · N 个步骤”的聚合折叠，
  也不会在 Run 完成时隐藏或重排。
  Run 内的依赖虚拟环境属于临时运行状态，不作为会话文件保存；其他工作区符号链接
  仍安全失败。
  单个 MCP 服务在远端工具发现阶段不可用或返回非法目录时，只会禁用该 MCP 的本次
  Run 工具并向 Agent 提供安全的能力降级提示；其他能力和主回复继续执行。授权撤销、
  冻结快照漂移、Secret 材料化不确定等安全边界仍会终止 Run。Skill 脚本及普通
  工具失败以错误结果返回 Agent，由其使用现有上下文继续或明确说明未完成部分。
- System/Project Agent、Skill、MCP 的受治理定义和不可变准入快照。Model、Skill、MCP、
  Channels 分别拥有自己的加密 Secret 与生命周期。Project
  MCP 的访问凭证按请求参数逐行声明，每行选择 Header 或 Query；两类参数可在同一
  凭证组中同时配置并一次加密保存，值不写入 MCP URL、资产定义或浏览器缓存。
  Project Agent 只有一个可变 Definition，保存后增加 revision 并立即影响后续新 Run，
  不再创建 Candidate/Historical Version；System Agent 仍是平台管理的单一定义。
  Project Skill 保存后生成不可变 Candidate Version；显式激活会原子设置
  `current_version_id` 并启用资产，历史版本只读且不能恢复、复制或重新激活。System
  Skill 只有自动成为 Current Version 的 v1，用户不能创建、保存或手工激活版本。
  项目 Skill 仅可通过 AI 对话创建/修订，或上传 `.zip`、`.skill`、`.tar`、`.tar.gz`
  或 `.tgz` 包；常见 macOS 归档元数据会被忽略。超出上传、解压、单文件或成员数限制
  的包会以明确的大小限制错误拒绝。项目与系统 Skill 的上传、Builder、导入、保存、安装和激活
  均不执行静态内容安全扫描，也不提供风险确认或扫描结论；仍校验归档路径、大小、文件数、格式、
  `SKILL.md`、frontmatter、权限以及版本文件与 checksum 完整性。Skill 内容需要由上传、保存和
  激活它的项目成员自行审查。Skill 详情可把
  当前选中的已持久化版本导出为
  `<slug>-v<version_number>.zip` 标准分发包；`SKILL.md` 位于包根目录，包内不含
  Secret 值、密钥或版本历史，被治理撤销的 System Skill v1 不可导出。
  项目 Agent 的删除采用软归档：从项目目录移除并拒绝后续
  Run，既有会话、运行记录及已准入的执行快照继续保留；已归档 Agent 不再占用项目
  名称，同一名称可用于创建具有新 ID 的 Agent。Agent 详情直接编辑唯一 Definition，
  保存使用 revision 做并发控制。项目 Skill 删除同样是不可恢复的归档：它从普通页面隐藏，
  自动从全部 Agent Definition 解绑且不改变 Agent 的启用/停用状态；同名 Skill 可重新创建。
  归档保留 Current Version 指针、全部 Skill Version 与文件、存储配额占用，以及 Secret
  状态、各 Generation 与密文；删除 Run、会话，以及清理原成员或账号私有数据时都不会清理
  这些内容，删除 Skill 本身也不会释放存储空间。只有整个项目被最终删除时，以上内容才会
  一并销毁并释放配额。
  Skill 可在 `SKILL.md` 中通过
  `required-secrets` 声明敏感环境变量；版本工作台和 AI Builder 提供结构化表单并与
  同一源码副本同步。版本工作台的“运行秘密”按精确 Skill Version 和变量名保存项目独有
  密文：声明仍只写入 `SKILL.md`，值只存 PostgreSQL。Candidate Version 激活前必须完成
  全部必需值；Editor 只能看到完整性，Project Admin 负责配置。向前保存的新版本会把仍兼容
  的值解密后重新加密为独立副本，新增或变化的声明必须重新输入。明文只在
  授权 Sandbox 执行边界解密，并以 Skill 声明的目标变量名注入；Local Provider
  与本地 AIO Provider 均不把它写入版本、快照或浏览器状态。
- Agent Builder 以独立于普通会话的设计会话展示真实模型思考和校验、保存阶段，支持按模型
  能力选择思考强度、停止当前生成并继续设计，以及断线后完整回放过程。它不调用工具，也不
  展示 Provider 原始响应、系统提示或最终 JSON；生成后的四份指令文档在独立设计稿侧栏中
  查看和编辑，聊天区只保留摘要入口。最终确认只创建 suspended Agent 及其初始
  Definition，不会自动启用。每次模型生成回合的默认值和硬上限均为 600 秒，不限制整个 Agent
  设计会话。
- AI Skill Builder 由仅供专用解析器访问的内置 Agent 执行，不出现在项目、全局管理或
  运行时 Agent 目录及其常规 API。它复用普通 Agent 的 Web、文件、Sandbox 和任务委派
  装配，并遵循 Local/AIO Provider 各自的安全策略；候选文件只能经受管工具、检查和
  显式提交进入不可变版本历史。
- 每个 Run（包括既有 Thread 的后续消息、编辑/重新生成、Automation 和 Channel）都在
  准入时重新解析 Agent Definition 与 Skill 的 Current Version，并冻结为不可变执行闭包。Agent 与
  MCP 继续保存精确快照；Skill 文件只在不可变 Skill Version 中保存一次，Run 保存无文件
  内容的 v4 manifest 和受数据库保护的精确 Version 引用。Worker 按该引用校验、物化并
  只读挂载 Run 专属 Skill tree，执行、重试和恢复期间都不会重新查询 Current Version。
  Agent Definition 对 Skill 的依赖仍只保存 Skill Asset ID，只有新 Run 准入时解析其 Current Version；
  MCP 仍绑定精确配置。
  `run_skill_snapshots.writer_mode` 是进程启动时冻结的发布选择，默认且正常发布必须使用
  `v4_reference`。仅在受控 R1 回滚期间，operator 才可让全部 Gateway 与 Scheduler 同时切换
  为 `legacy_v3`，并配置发布内置的 artifact version
  `run-skill-snapshot-writer-v2` 和 policy digest
  `e01a816a3f20a4ecf088e2f0d37b92ba16634e5969860b900a14924312edb6e8`；已被资源验收否决的
  v1 坐标、缺失、混用或不匹配都会拒绝启动。v2 固定限制为单 Skill source 36 MiB、单 Skill
  codec envelope 256 MiB、单 Run 累计 encoded Skill JSON 48 MiB。Legacy 模式在读取 Skill
  文件前先按固定上界拒绝过大请求，并用 PostgreSQL
  全库非阻塞单写 gate 限制含内容的准入；busy 返回可重试 503，交互式过大请求返回 413，
  Scheduler 的 busy/过大 occurrence 都保留为 due/retryable。平台管理运维页会显示当前
  mode、artifact、policy digest 与 ready 状态，供发布读回核对。
- 项目主会话可为下一次 Run 一次性选择 Interactive 或 Research Workload Profile；
  Gateway 把服务端确认的 effective profile 冻结进 Run Snapshot，隐藏 continuation 与
  Job retry 继承同一选择。Research 提高 Sub-Agent 工作量上限，但不增加工具、项目或数据
  访问权限。System Runtime Policy v6 分别冻结主 Agent 每个 Run 的内部工具调用上限和每个
  Sub-Agent Task 的上限：`task` 委托调用本身计入主 Agent，同一 Run 的主 Agent 跨隐藏
  Graph Turn 持续累计；每个 Sub-Agent Task 按自己的执行标识独立计数，多个并行 Task
  互不占用额度，也没有额外的 Run 汇总上限。已准入 Run 及历史 v2–v5 策略继续使用其冻结的
  Lead/Sub-Agent 全 Run 共享上限，不会在执行中改变语义。
- 长期 Memory、上下文压缩、Dream 整理、归档检索和账号级个性化控制。超大的完整
  工具 turn 通过有界分层 SNIP 逐 turn 压缩，receipt 仍绑定原始 checkpoint source；
  Dream 模型不可用会显示明确失败，不会伪装成“没有待整理 Memory”。上下文用量由
  Thread 自己的只追加 Context Evidence 和可重建 Projection Head 提供：主 Agent 与每个
  Sub-Agent Task 独立计量，系统提示、Agent 指令、工具、Skill、MCP、摘要、对话、图片和
  Provider framing 按固定分类显示。切换下一次 Run 的模型、Agent、Skill、MCP 或策略不会
  改写已有 idle 读数；新 Run 由 Worker 对冻结后的真实请求做最终容量保护，必要时先压缩
  再重新计算。尚无视觉 Token 上界时仍显示含图片数量的上下文下界，但不会绕过 Provider
  容量保护。Token 用量展示开关只影响累计明细和诊断展示，不关闭 Context Evidence、
  Projection、自动压缩或最终容量判断。
- Sub-Agent、Guardrail、Tool Search、ToolCallControl 和可扩展工具链。每个
  Sub-Agent Task 直接写入的输出先进入各自的隔离草稿，只有 Lead 明确复制并调用
  `present_files` 后才作为主会话文件交付；文件变更的校验与持久化仍由后端执行，无法
  可靠计算的行数保持未知，但普通会话不再渲染文件变更卡片。
- 文本 lead model 的受治理图片识别桥接：按 Run 冻结辅助视觉模型，使用
  `inspect_image` 返回有界、不可信视觉分析；视觉调用与其他模型调用共用唯一
  `ModelRuntime` 和所选模型已有的 Provider adapter。
- Local、容器、BoxLite 和可选 Provisioner/Kubernetes Sandbox provider。
- 可选的项目 RAG 知识库（Knowledge）：独立 `actweave-knowledge` 软件包提供文档上传、
  摄取切分、向量召回加 Reranker 精排检索和 Agent `knowledge_search` 引用；文件存储在
  外部 MinIO。全新 `setup-db` 同时收到完整的
  `ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT`、`ACT_WEAVE_KNOWLEDGE_MINIO_BUCKET`、
  `ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY` 与 `ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY`
  时，会在建库前探测管理员预建的未版本化 bucket，并加密写入配置、默认启用；
  四项全无则保持默认关闭，部分配置会在任何 DDL 前失败。管理员仍可在
  `/admin/settings/knowledge` 的“知识库配置”页修改开关和配置。
  配置存入 PostgreSQL，MinIO 密钥加密且只写不回显；开启时保存必须通过存储探测，
  存储、配额与缓存设置在 Gateway/Worker 重启后生效。旧 YAML `knowledge` 块已移除，
  迁移顺序见 [Install.md](Install.md#knowledge-configuration-migration)。关闭功能会停用
  路由、Agent 工具和 Knowledge Task worker，但 Worker 仍保留 Project 最终删除所需的
  清理能力；曾经写入过文档的部署必须保留原 MinIO 配置直到相关 retention 清理完成，
  且 bucket 必须关闭 versioning/Object Lock，凭据需允许读取 versioning 状态、列举前缀
  和删除对象；存储启动检查失败只停用 Knowledge，管理页和其他功能仍可用，运维页显示
  Knowledge `unavailable`；每次上传的 PUT 前检查和 Project purge 仍失败关闭。
  单文件上限硬限制为 50 MiB，单 PUT 在每个对象存储实例内串行执行，以约束 MinIO SDK
  的整 part 内存峰值。原件上传先提交字节配额预留，确认 PUT 后结算；响应丢失或清理失败时
  保留原对象身份与计量事实，只有确认对象删除后才释放配额。派生附件与完整 manifest 缓存按
  数据库登记的精确对象身份存储、校验和回收；删除文档时先撤回已发布 Segment 与 Extraction
  指针，再逐个确认附件、manifest 和原件已不存在，随后释放各自配额并删除权威行。上传仍在
  `pending` 结算时保留 tombstone 与预留，待补偿路径落定 `stored/delete_pending` 后由重试清理。
  关闭 Knowledge 不会关闭独立的 Project retention 清理能力。已发布 Segment 的图片通过两条认证 GET 路径读取：
  管理读取可查看停用内容和失败重处理保留的已发布图片，引用读取只允许当前 ready 且启用的 Base、Document 和 Segment。
  两者都必须提供 Segment 已发布版本与内容 SHA-256，不使用 Document 最新失败目标版本。服务端只返回授权绑定的图片字节，
  不下发 MinIO locator 或签名 URL，响应使用 `private, no-store` 和 `nosniff`。摄取与重嵌入在每批模型请求和发布前复核 Project 状态及任务租约，
  失效后停止未派发工作、禁止发布。Project 进入待删除状态后，Knowledge Task 不消耗重试预算地暂停；
  恢复 Project 会自动继续，最终清理则等待所有仍在运行的 Task 静默后才删除对象。启用后项目侧
  提供知识库/文档管理与检索测试页面（导航按模块可用性自动显示，页面状态随 URL
  可深链接/前进后退），支持文档与分段级
  治理：启停开关（停用即从检索与引用中排除，不删除向量）、文档重命名与批量启停/删除、
  分段列表可在侧栏查看全文、编辑或手工新增分段，删除需确认
  （内容修改同步重算 Token、索引文本、向量、词法索引、父子块和当前已发布图片绑定，
  每批及重试前复核权限；人工替换的文字不保留无法证明的原文件位置）
  及字数统计，维护操作同步刷新已打开的分段定位；文档列表支持搜索/状态过滤/排序/分页，
  跨页读取发生总数变化或重复条目时明确提示刷新，不显示伪完整列表。处理中的文档展示真实任务进度（阶段、已验证批次
  计数、尝试次数与自动重试等待），列表上方汇总处理中/等待重试/失败/就绪数量；上传按
  递归分隔符切分，可自定义分隔符（默认 `\n\n`）并选择预处理规则（压缩多余空白、
  删除 URL 与邮箱），创建向导使用分段模式卡片和左右独立滚动的配置／预览区（预览与实际摄取一致，
  多文件时可指定预览文件，参数修改标记过期后显式刷新），
  全部分块参数上传时固化、重试沿用；多文件上传逐文件给出结果，失败文件保留在
  队列中重试且不重传已成功者；支持父子分段模式（父块承载返回内容、
  子块承载向量，命中按父块内最高子块分回卷去重）；空白知识库仅填写名称和描述，
  不绑定模型，也不要求选择检索方式。已有知识库上传文档与新建共用“选择文件 → 分段预览 → 处理结果”三步向导。
  未配置空库在第二步设置 Embedding、检索方式及可选 Reranker，或提前在设置页配置；
  已配置知识库沿用现有模型与检索设置，多文件上传失败时仅重试失败文件；未配置的空库不参与检索。文档创建向导及设置页可保存检索模式，
  设置页可配置检索默认参数
  （top_k 与分数阈值，检索测试与 Agent 工具未显式传参时生效）与检索模式
  （向量检索 `semantic` 或混合检索 `hybrid`：词法 `lexical_v1` 分词走 PostgreSQL tsvector/GIN 与
  向量路 RRF 合并，检索测试可单次覆盖模式），检索自动记录
  查询日志（含来源、命中数与最高分，检索测试页展示最近查询并可点击回填）
  并累计分段/文档命中次数；统计写入失败不影响已通过最终权限及内容验证的检索，
  检索期间实际使用的库级参数变化则返回冲突，要求重新检索。检索测试结果标注最终排名与分数来源
  （Cosine/Rerank/秩融合），可展开安全诊断（策略/预算/计数/耗时/模型，
  不含正文），命中详情钉住检索时的完整原段并高亮真实命中的子块，内容漂移
  时提示重新检索，且可一键定位到文档维护页；空结果按从未检索/无命中/
  过滤为空/未就绪/内容过期区分，模型失败持续可见并可重试；上传支持
  PDF/DOCX/TXT/MD/CSV/XLSX/HTML/PPTX/EPUB，DOCX 按正文顺序提取普通段落与
  表格行，行内单元格保留在一起并标注表号/行号，合并单元格只提取一次；
  此前漏提取表格的 DOCX 需显式重解析才会补入表格内容，该操作会替换人工分段；
  知识库可定义元数据字段（文本/数字/时间）并为文档赋值（支持多选文档批量
  保持/设置/清空，一次全量成功或全量回滚），检索测试与
  Agent 工具支持按元数据条件过滤（等于/包含/范围，AND 组合）；
  设置页可换绑 Embedding 模型并对已发布文档重嵌入（不读取或重解析原文件，保留分段 UUID/Markdown/
  来源、图片绑定、解析参数、人工编辑和启停状态，仅从已发布索引文本重算向量，处理期间退出召回，未发布文档跳过并报
  真实计数），文档操作里可从原文件重新解析（可改切分参数并先服务端预览，
  确认后替换全部分段、覆盖人工编辑与启停），并可按库选配或解绑 Reranker
  模型（保存即生效，无需重建）；创建向导也可直接选择可选的 Reranker 模型，
  向量检索与混合检索均支持，不选择则沿用无重排序的流程。Agent `knowledge_search` 引用返回 64KiB
  预算内的完整原文正文（超出以 `omitted_count` 记数），会话中的
  最终回答下方展示知识库引用；管理员在 `/admin/settings/models` 的统一
  “模型供应商”页面维护供应商及其文本模型与 Embedding/Reranker 模型并做连接测试。
  知识库还可启用“摘要索引”：系统配置的文本 System Model 为长分段生成检索摘要，
  作为第三个语义来源按最高余弦分回卷原段；摘要失败不改变文档就绪状态，可显式重试。
  编辑会刷新受影响摘要，重嵌入保留摘要文本、只重算向量，重新解析替换后再生成。
  引用与工具正文始终返回真实原段，摘要仅在详情中单独标注；是否提升真实召回效果仍须通过
  [M11 质量门](docs/superpowers/plans/2026-08-31-rag-knowledge-m11.md#t11真实质量门文档与交付确认)。
  查询向量使用进程内 LRU+TTL 缓存，同模型同查询命中时不重复请求 Embedding；缓存
  不跳过召回与终审的权限检查，诊断面板显示缓存命中、摘要候选和命中来源。
  RAG 文件解析的预览与 Worker 摄取共用本地 `extraction`、Knowledge Token
  分段和原子发布路径；处理结果保留来源位置、解析警告和受权图片绑定。
  格式解析限制为本地文件，默认 `builtin`，可选 `unstructured_local`；
  不执行 OCR，不调用解析 API，也不在运行时下载 Pandoc 或 NLP 资源。
  本地环境准备需安装固定的 `extraction-local` 依赖与平台 libmagic，并生成、
  审查解析资源锁。
  子进程必须通过 macOS `sandbox-exec` 或 Linux bubblewrap 隔离；缺失资源或不具备
  隔离权限时明确不可用，不能退回裸进程。部署到 Linux 主机前必须在目标机验证
  bubblewrap、libmagic 和当前平台资源锁；macOS 或单格式测试不代替该验收。
- 一次性或 Cron Automation，以及 Feishu、Slack、Telegram 等外部 Channel。
- 平台管理员的系统设置、模型目录、资产治理和运维界面。
  System Runtime Policy v6 在系统设置中分别配置主 Agent 每 Run 与每个 Sub-Agent Task 的
  内部工具调用 hard limit，并配置委托上限；
  管理员每次保存都会在同一事务中创建不可变的新版本并将其设为当前生效版本，只影响之后
  准入的 Run。

项目菜单按服务端下发的 capability 分层，而不是仅按角色名称在浏览器中推断：

| 项目角色 | 项目菜单             | 主要边界                                          |
| -------- | -------------------- | ------------------------------------------------- |
| Admin    | 工作、能力、项目管理 | 项目治理、成员、各域秘密配置、审计及资产生命周期  |
| Editor   | 工作、能力           | 运行工作，保存/启停 Agent，并保存/激活/启停 Skill |
| Runner   | 工作、Agent（只读）  | 运行工作；只读查看 Agent                          |
| Viewer   | 工作、Agent（只读）  | 查看自己的既有工作和 Agent；不能运行              |

“工作”包含会话、Automation 和 Memory；渠道连接属于“项目管理”，只对具备
`project.channels.manage` 的项目管理员显示。Runner 和 Viewer 可只读查看 Agent 目录，以及
本人已有且未完成的 Agent Builder 会话；创建、继续设计、编辑、取消、提交和生命周期操作仍
按编辑或治理 capability 拒绝。Skill/MCP 作者工作台也不向只读角色开放。

会话列表的标题、时间和行内留白均可点击切换会话；重命名与删除按钮独立操作。

## 运行架构

| 组件        |   默认端口 | 职责                                        |
| ----------- | ---------: | ------------------------------------------- |
| Nginx       |     `2026` | 唯一浏览器入口，代理前端和 `/api/*`         |
| Frontend    |     `3000` | Next.js Web UI                              |
| Gateway     |     `8001` | 认证、项目 API、Run 准入、查询和 SSE replay |
| Worker      | 无公开端口 | 唯一 Agent graph 执行进程                   |
| Scheduler   | 无公开端口 | 可选 Automation 轮询与准入进程              |
| Provisioner |     `8002` | 仅特定 Kubernetes Sandbox 模式需要          |

```text
Browser / Channel
        |
        v
Nginx :2026 ----> Frontend :3000
        |
        +--------> Gateway :8001 ----> PostgreSQL
                                      ^          ^
                                      |          |
                                   Worker    Scheduler
```

Gateway 不执行 Agent graph；Worker 不提供浏览器业务 API。私有资源按账户、项目和
owner 作用域隔离，浏览器状态和请求字段都不是授权依据。

后端内部继续保持 HTTP Adapter、应用域事务与 Harness Runtime 的单向依赖。
为兼容既有调用保留的 Python 入口只转发 owning module 的同一对象，不维护第二套业务实现。

## 快速开始

### 环境要求

- Python 3.12+
- Node.js 22+
- pnpm 10.26.2+
- `uv`
- PostgreSQL
- 本地全栈模式所需的 Nginx

从获准的内部代码源取得项目并进入仓库根目录：

```bash
make check
make install
make setup
```

`make setup` 会引导生成根目录 `config.yaml` 和所需环境配置。不要提交
`config.yaml`、`.env`、数据库密码或 provider key。完整字段见
[配置模板](./config.example.yaml)，安装和升级规则见 [安装流程](./Install.md)。

### 初始化数据库

为一个新的空 PostgreSQL 目标配置 `DATABASE_URL`、`POSTGRES_ADMIN_URL`、
`ACT_WEAVE_SECRET_KEY` 和 `ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY`，并为
SiliconFlow 供应商 seed 提供 `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY`
（不使用 Knowledge 的部署可改设
`ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP=1` 显式跳过），然后运行：

```bash
make setup-db
make check-db
```

- `make setup-db` 只初始化空目标库。安装器会先校验结构模板
  `full_schema.sql` 与唯一的静态注释来源 `schema_comments.sql`，再在内存中
  组合为一个 `BEGIN/COMMIT` 批次执行；两个 SQL 文件都不是可单独执行的安装入口。
- 可选的 Knowledge 自动初始化需要在同一环境中完整提供
  `ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT`、`ACT_WEAVE_KNOWLEDGE_MINIO_BUCKET`、
  `ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY` 与 `ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY`。
  全新数据库应使用部署专属的空 bucket；该 bucket 必须由管理员预先创建、关闭
  versioning/Object Lock，并可被当前凭据探测。初始化不会清理、自动创建或猜测
  bucket，当前只支持非 TLS 的 bootstrap endpoint。四项全无
  时 Knowledge 保持关闭，任一项缺失或为空都会在建库前失败。
- Schema 升级只通过显式维护命令执行：

  ```bash
  make stop
  make upgrade-db
  make check-db
  ```

  当前 head 仍是 `schema_v1`，迁移 Registry 为空，因此该命令会校验精确 Catalog
  后幂等返回“已是当前版本”。未来版本只有在同时提供线性、前向的打包迁移时才能提升
  marker；未知版本或 Catalog drift 会失败关闭。命令不读取安装期模型或存储密钥，
  也不会自动启动服务。

- 空库初始化的首个 System Runtime Policy 使用 schema v6：主 Agent 每个 Run 的内部工具
  调用上限为 `200`，每个 Sub-Agent Task 为 `50`。`task` 本身计入主 Agent；并行 Task
  分别计数。重复执行初始化不会覆盖既有数据库的当前策略版本，已准入 Run 和历史 v2–v5
  策略仍保留旧的全 Run 共享计数语义。
- 初始化会为应用表、Schema V1 标记表、LangGraph 表及每个 `run_events` 物理分区写入
  非空的中文表注释和字段注释；结构 DDL、静态注释及初始分区在同一事务内完成，缺失或
  漂移的注释会在建库或未来升级 DDL 前安全失败。
- 未知 marker 或 catalog drift 都会安全失败；开发阶段请改用新的空目标库。
- Context Evidence / Projection v2 是 Schema V1 的直接切换，没有旧 authority API、Profile
  证明或数据库兼容 Adapter；它属于当前 Schema V1 新基线，不提供旧库迁移。未来受支持
  的 Schema 前驱通过 `make upgrade-db` 迁移，未知旧库应改用新的空目标库。
- Gateway、Worker 和 Scheduler 从不自动创建、升级或修复 schema。
- 升级打包 System Agent/Skill 时，先停止运行服务，在维护窗口执行
  `make upgrade-system-assets`；该命令从标准运行环境读取 `DATABASE_URL`，以相同确定性
  UUID 原地替换唯一 v1 并保持它为 Current Version，不追加版本，可幂等重跑。System MCP
  仍遵循自己的配置治理。
- 全局管理员可对 System Skill v1 执行不可逆治理撤销。撤销不改变 Current Version；新绑定
  和新 Run 会拒绝该定义。软件包内容变化后的升级会清除旧定义的撤销，同字节幂等升级则
  保留撤销。已经准入的 Run 继续使用冻结快照，不会被强制中断。
- 生产运维应通过平台外部的 cron、systemd timer 或编排器每日运行（至少每个 UTC 月
  成功一次）`make prepare-run-event-partitions`；该幂等命令只在当前 schema head 上
  预创建 UTC 当前月至 N+2 月分区，锁等待超时后可安全重试。不要把它挂到 Gateway、
  Worker 或 Scheduler 的启动路径。

详细准备步骤见 [Install.md](./Install.md)。

### 启动与停止

```bash
make dev      # 热更新开发模式
make start    # 本地优化构建模式
make dev-daemon    # 后台开发模式
make start-daemon  # 后台本地生产模式
make stop
```

在 macOS 上，两个 `*-daemon` 命令会交由当前登录用户的 `launchd` 持续托管一个前台
启动器；命令返回后服务仍由该启动器负责，避免终端或自动化执行会话结束时一并回收子进程。
用 `make stop` 停止服务并卸载该托管任务。服务日志仍在 `logs/`，启动器日志为
`logs/{dev,prod}-daemon-supervisor.{out,err}.log`。本地启动器只会在 Gateway、Worker、
Scheduler、Frontend、Nginx 五个子进程仍存活，且数据库中出现 fresh、可执行
`private_run` 的 Worker 后报告启动成功；schema 不匹配或 Worker 超时会明确失败。

浏览器访问 <http://localhost:2026>。常见入口：

- `/workspace`：账户级多项目工作区，以紧凑卡片浏览和进入项目，显示项目创建时间，支持搜索、置顶、编辑与恢复项目。
- `/projects/{project_slug}`：项目会话、资产、Memory、Automation 和设置。
- `/admin`：仅 system admin 可访问的平台治理与运维页面。

### 系统模型适配器

管理端当前可创建 OpenAI、OpenAI 兼容增强、Anthropic、DeepSeek、DeepSeek
兼容增强和 vLLM 模型。Vision Bridge 不再定义专用模型适配器；
`vision_openai_compatible_v1` 不在生产适配器注册表，`vision_bridge_fake` 仅供测试注入。
模型编辑器按适配器描述符显示独立表单字段，不要求管理员编写 Provider JSON；常用设置直接显示，
其余设置默认收在“高级设置”中。平台已固定的默认值会直接展示，未固定的参数明确使用
“Provider 默认”，已有结构化兼容设置在同一 Provider 下编辑时原样保留。
OpenAI、OpenAI 兼容增强和 vLLM 适配器的通用表单选项为 `none`、`low`、
`medium`、`high`、`xhigh`、`max`。
DeepSeek 与 DeepSeek 兼容增强适配器的思考强度只提供 `low`、`high`、`max`；运行时
`thinking`、`pro`、`ultra` 分别映射到这三档，`flash` 使用思考关闭参数且不发送强度值。
MiMo、MiniMax、StepFun、MindIE、Claude Code CLI
和 Codex CLI 模型适配器已停止支持，不再允许新建、启用、设为默认或准入新 Run；已有
历史目录记录仍可由管理员查看并改配到受支持适配器。

API Key 与服务地址统一由“模型供应商”持有：每个文本模型必须绑定一个供应商，
模型自身不再单独填写 Key 或 Base URL。更换供应商的 Key 或端点会在同一事务中为其
全部绑定文本模型重新加密凭据，已冻结旧凭据的 Run 会失效；供应商弹窗提供不落库的
候选连接测试，文本模型的连接测试直接使用供应商已保存的 Key。管理页按供应商分组
展示文本模型与 Embedding/Reranker 模型，但会话与 sidecar 仍按具体模型选择。
文本模型与 Embedding/Reranker 模型的“删除”统一为不可恢复的逻辑删除：从当前目录、
默认选择和新任务中隐藏，但保留数据库身份与历史引用。已接纳的 Run 继续使用冻结的文本
模型配置与凭据代次；检索模型若仍被任一知识库引用，服务端会拒绝删除。供应商只有在其
全部活动子模型都已逻辑删除后才能逻辑删除，同名供应商或同一类型的同名检索模型随后可
作为新身份重新创建。

全新数据库默认初始化两个供应商并分别沿用各自的引导 Key：DeepSeek 供应商挂
三个 `deepseek` 文本模型配置（`deepseek-v4-flash`、`deepseek-v4-pro` 和
`deepseek-v4-flash-vision-exp`），SiliconFlow 供应商挂默认检索模型（可显式跳过）；
其他供应商从管理页添加。Flash 是默认 System Model；Vision Exp 是默认
Vision Bridge；三者的必填最大输入上限均初始化为 `1,000,000` tokens，独立于
`settings.max_tokens=51,200` 的输出上限。管理员新建、更新或测试 System Model 时必须
填写 `max_input_tokens`（`1..2,000,000`）；它表示该 Provider Model 可接收的最大输入，
并作为上下文占比与自动压缩容量保护的模型分母，不是 Run 的 token budget。

### 文本模型图片识别桥接

该能力不在 `config.yaml` 声明工具、模型或开关。System admin 先在
`/admin/settings/models` 检查内置模型或创建 active、`supports_vision=true` 的视觉模型，
并使用“测试连接”验证其多模态调用，再到 `/admin/settings/system` 的“图片识别桥接”
确认或选择该模型。非空选择即对后续 text-only project Run 启用，清空即关闭。全新安装
默认选择已内置的 `deepseek-v4-flash-vision-exp`；原生视觉 lead model 继续使用现有
`view_image`，不会同时注册
`inspect_image`。该 bootstrap 默认值不替代供应商数据政策审批；生产部署尚未批准外发
时，须在接收项目 Run 前清空选择。
全新安装把单次 `inspect_image` 端到端截止时间初始化为 60 秒；管理员可在 5–120 秒内
调整，更新只会冻结到之后新建的 Run。
每个 Run 的视觉请求次数、累计图片字节或像素额度耗尽时，工具返回
`VISION_BUDGET_EXHAUSTED`，明确要求本次 Run 不再重试，等待不会恢复额度。
供应商暂时限流仍返回 `VISION_RATE_LIMITED`；两者不再混用。

Bridge 不解析或重写厂商协议。`inspect_image` 通过唯一 `ModelRuntime` 向所选模型发送标准
LangChain 多模态 content block；OpenAI、Anthropic、DeepSeek、vLLM 或其他已支持
Provider 的现有 adapter 负责 Secret、Endpoint、请求序列化和响应解析。任意 active、
`supports_vision=true` 且 adapter 仍可新绑定的系统模型都可以被选择，不按模型名称或
Luna 身份硬编码。

工具只发送规范化后的单张图片、固定 system prompt、固定 mode 指令和 lead 根据当前用户
问题生成的必填 `analysis_goal`，不发送完整对话或文件路径。服务端把普通 `AIMessage`
文本包装成有界的 `inspect_image.result.v2` ToolMessage，并明确标记为不可信内容。管理端“测试连接”使用
平台生成的无敏感 64×64 蓝色方块 PNG 经过同一个 Runtime 和 adapter；成功只证明当次
连接可用，生产启用前仍须完成供应商政策和真实 API 质量、延迟与限流验收。

Run 仍冻结精确 `purpose="vision"` 模型配置载荷和 Secret Generation，Worker 调用前后仍使用
durable dispatch authority；暂停模型或清除 API Key 会阻断后续调用，但不能召回已经
在途的请求。当前架构、历史兼容和验收约束见
[后端开发约定](./backend/AGENTS.md)。

## 本机运行与 Sandbox

应用只支持本机进程运行，不提供其他整栈启动方式。
使用前文的 `make dev`、`make start` 和 `make stop` 管理 Gateway、Worker、Scheduler、
Frontend 与 Nginx。

Docker 仅作为可选 Sandbox runtime。需要 AIO Docker Sandbox 时可预拉取镜像：

```bash
make setup-sandbox
```

Docker Sandbox 的 Worker 与 Docker daemon 必须共享同一文件系统视图；路径视图不同
时 Run Skill 只读挂载直接 fail closed，不提供配置绕过。可选 Kubernetes Sandbox
Provisioner 位于 `sandbox/provisioner/`，作为独立控制服务运行，不是完整应用的部署方案。

BoxLite P-04 与 E2B P-05 使用相同的版本化门禁，默认分别为
`sandbox.boxlite_p04_v1_verified: false` 和
`sandbox.e2b_p05_v1_verified: false`。只有在目标 Linux/KVM 主机或受控 E2B
账号上执行对应真实非特权 guest 读写、exact release/readback 和跨进程 owner
reconcile 探测并退出 `0` 后，才可把当前部署的字段改为字面量 YAML boolean
`true` 并重启 Worker：

```bash
cd backend
ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION=1 \
ACT_WEAVE_CONFIG_PATH="${ACTWEAVE_BOXLITE_TEST_CONFIG:?set a disposable BoxLite test config}" \
uv run pytest tests/test_boxlite_run_skill_mount_lease.py \
  -m 'provider_integration and p04_boxlite' -q

ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION=1 \
ACT_WEAVE_CONFIG_PATH="${ACTWEAVE_E2B_TEST_CONFIG:?set a disposable E2B test config}" \
uv run pytest tests/test_e2b_run_skill_mount_lease.py \
  -m 'provider_integration and p05_e2b' -q
```

P-04 还要求本机 BoxLite registry/runtime 启动探测成功；P-05 要求配置凭据并在
5 秒内完成 E2B control-plane metadata probe。依赖、虚拟化能力、凭据、网络或
provider readback 任一不可用时 readiness 保持 false。mock contract、请求字段和
环境字符串都不能启用这两个门。

## 常用开发命令

| 命令                                              | 用途                           |
| ------------------------------------------------- | ------------------------------ |
| `make doctor`                                     | 检查配置、数据库和运行环境     |
| `make support-bundle`                             | 生成脱敏诊断包和内部事件草稿   |
| `make gateway` / `make worker` / `make scheduler` | 单独启动后端进程               |
| `make test`                                       | 使用隔离测试库运行后端核心测试 |
| `cd backend && make lint && make format`          | 后端静态检查和格式化           |
| `cd frontend && pnpm check && pnpm test`          | 前端 lint、类型检查和单元测试  |

完整命令见 `make help`。本私有仓库没有托管 CI；集成或发布前必须针对当前
checkout 手工执行相关 PostgreSQL、前端、浏览器、安全、容器和目标环境门禁。

## 安全边界

- 对外只开放受 TLS 和网络策略保护的 Nginx；不要直接公开 Gateway、Frontend 或
  Provisioner 端口。
- `LocalSandboxProvider` 在 Worker 的 OS namespace 执行命令，不是强隔离边界；
  它使用运行 Worker 的宿主账号，只用于可信环境。
- 如确需 Local Bash，可配置逐条 `allow_once` / `deny` 审批；批准仍是宿主 RCE，
  不会变成沙箱，也没有会话级授权。配置字段见
  [配置模板](./config.example.yaml)，执行边界见 [后端开发约定](./backend/AGENTS.md)。
- Apple silicon Mac 上需要执行 Agent 生成的 Bash/Python 时，优先使用
  `AioSandboxProvider` + Apple Container；配置与 Provider 验收坐标见
  [配置模板](./config.example.yaml) 和 [后端开发约定](./backend/AGENTS.md)。
- API key、Cookie、Secret、数据库密码和完整连接 URL 不得进入代码、日志、
  截图、浏览器缓存或诊断材料。
- System admin 不自动拥有项目权限；项目访问必须服从服务端返回的 membership 和
  capability。

## 文档导航

- [安装流程](./Install.md)
- [后端概览](./backend/README.md)
- [前端概览](./frontend/README.md)
- [领域术语](./CONTEXT.md)
- [架构决策](./docs/adr/)
- [后端开发约定](./backend/AGENTS.md)
- [前端开发约定](./frontend/AGENTS.md)

## 许可证与致谢

本项目采用 [MIT License](./LICENSE)。感谢
[ByteDance DeerFlow 上游项目](https://github.com/bytedance/deer-flow)及其贡献者奠定的
Agent harness、工具和前端基础。

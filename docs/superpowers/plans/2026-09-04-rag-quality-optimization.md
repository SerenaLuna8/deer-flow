# RAG Quality Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付已审阅规格的首期 A1–A4：正文 overlap、格式字面量保真、Word 自定义标题及准确的用户提示，并保持来源、预算和版本约束。

**Architecture:** 沿用现有 extraction → split_documents → index_text → 原子发布路径。按分段契约、格式保真、前端说明拆成三个子计划，各自完成 TDD，再由单一集成人员更新指南并运行跨路径验收；不增加新的默认处理路径。

**Tech Stack:** Python 3.12+、pytest、markdown-it-py 4.2.0、tiktoken 0.12.0、现有 PDF/Office/HTML/Unstructured Adapter、PostgreSQL/MinIO；Next.js、React、TypeScript、现有 Rstest/Playwright。

**Spec:** `docs/superpowers/specs/2026-09-04-rag-quality-optimization-design.md`。用户已请求基于该规格进入实施计划阶段；本次产物仅为计划，不代表开始实现或授权部署。

## Global Constraints

以下条款取自规格第 4 节，三个子计划及其全部任务共同继承：

1. 预览、摄取和显式重新解析继续共用现有 extraction 与 `split_documents`，不增加第二条默认处理路径。
2. `content` 是显示 Markdown；Embedding、词法和 Reranker 消费 `index_text`。不得通过向量侧偷偷删字或截断绕过预算。
3. `SourceSpan` 和附件出现位置必须准确对应最终文本；原文属于 `source`，重复标题或字段标签等上下文属于 `context_prefix`。来源归属不是访问授权。
4. 保留服务器身份、Project 权限、任务租约、版本比较和原子发布；任何失败不得混用新旧分段、图片或向量。
5. token profile 父段默认 1000、overlap 默认 100；父子模式子块默认 500。父段范围 200..4000、overlap 0..500 且小于父段、子块范围 100..2000 且小于父段，不变；历史 character 值不套用这一 Token 口径。
6. token profile 的父段和 Child 分别满足显示 Markdown Token、`index_text` Token 及 16000 字符上限；标题、表头、分隔符和保护字符均参与相关预算。overlap 不是额外赠送的预算。
7. 每文档父分段最多 5000，父子模式的累计 Child 向量条目另限 5000；两者不是合计 5000。沿用当前其余配额检查，不增加上限。
8. Child 在各自父段内零重叠。父段重叠后，不同父段的 Children 可能覆盖相同原文；不新增跨父段全局去重。
9. 冻结 character 算法不改写为 token 算法；重新向量化不读取原文件、不重新切分、不丢失人工编辑。
10. 解析继续本地、离线、受 OS 沙箱约束；复用现有依赖、来源映射和错误机制，不新建解析服务、数据库表或通用框架。

额外执行约束：只在用户授权实施后修改代码；提交、部署、目标数据库操作和真实模型调用分别需要相应授权。每个任务的提交步骤改为“审查交付点”；没有明确授权不执行 `git add`、`git commit`、`git push`。

---

## 子计划与文件所有权

| 子计划 | 交付 | 代码所有权 | 顺序 |
| --- | --- | --- | --- |
| [分段与版本约束](2026-09-04-rag-quality-chunking.md) | A1、splitter-v3、旧版本直接二次切分拒绝、相关纯函数及发布测试 | `ingestion/splitter.py`、`ingestion/structure.py`、`ingestion/profiles.py`、相应后端测试 | 优先执行 |
| [格式保真与 Word 标题](2026-09-04-rag-quality-format-fidelity.md) | A2/A3、原文叶节点保护、Word 大纲级别、token 清洗表示兼容、adapter-v2 与资源锁 | `extraction/` Adapter、literal helper、`ingestion/cleaner.py`、资源版本与相应后端测试 | 可并行实现；集成时依赖分段计划的 escape/entity 原子边界与版本检查 |
| [前端提示与错误交互](2026-09-04-rag-quality-ui-guidance.md) | A4 单位/overlap 说明、旧版本失败保留输入 | 现有知识库配置/编辑组件、翻译和相应前端测试 | 前端重构落点核对后独立执行 |
| 本总计划 | 基线、文档、跨路径门禁、最终审查 | `README.md`、`backend/AGENTS.md`、`frontend/AGENTS.md` | 单一集成人员拥有，不并行改指南 |

这些是同一次未发布变更的任务边界，不是允许每个中间任务独立部署的许可。特别是 `adapter-v2` / `splitter-v3` 所声明的完整行为必须在最终交付中一次闭合，不能先发布版本身份，再往同一身份追加行为。

### 规划阶段验证出的必要组合细化

新的字面量序列化会产生 `a\_b@example.test` 及 `&#32;`/`&#9;`。内存探针确认，原 token 清洗器可能只删邮箱后半段，或不能按既有规则压缩实体表示的多余空白。因此格式子计划包含 token-only 的表示兼容修复，不增加清洗开关或改变用户规则；raw/character 路径保持原样。

这属于规格 A2 的端到端保真要求，但清洗实现已有可观察变化，不能继续伪称 cleaner-v1。最终处理身份为 `splitter-v3 + cleaner-v2 + adapter-v2`；profiles.py 由分段计划唯一修改，Adapter/清洗实现由格式计划拥有。提取缓存不含 cleaner 身份，仍可复用同 ParseProfile；预览/冻结处理身份必须随 cleaner 改变。原规格未被本轮改写，此细化在实施前随本计划一起审阅。

### 当前工作区注意事项

- 代码基线为 `680b0fe45d006460cfd67e814fc84ae4f0de9b26` 加当前未提交修改，不能只检出 HEAD 后声称包含相同基线。
- `PrefixTokenCounter`、Markdown parser 缓存及其测试属于已有修改，需保留。
- 前端已接入 `knowledge-chunk-settings-fields.tsx`、`knowledge-chunk-preview-list.tsx`、`knowledge-base-configuration-summary.tsx`；实施前复核确认显式重新解析配置已移到 `knowledge-document-chunk-settings.tsx` 独立页面。UI 子计划已据此更新共享字段落点、菜单、页面选择器和自动预览断言；实际接线若继续变化，以执行前调用链为准，不接管或回退用户重构。
- 隔离工作区在实际执行时按 `superpowers:using-git-worktrees` 处理。所选工作区必须包含经用户确认的有关未提交基线，不能默默丢弃，也不能为隔离而擅自提交或 stash 用户修改。
- 跨任务共用 `test_markdown_chunking.py`、`test_parsing_profiles.py` 等文件时，由当前文件所有者合入，不能让两个 agent 同时写。

### 本轮规划校验记录（2026-09-04）

- 在当前代码上重新运行 I1 Step 3 的完整命令：`81 passed in 5.80s`。这是未实施优化的基线，不是修复结果。
- 将 C1–C3 的计划片段仅注入独立 Python 进程内存：20 个新增场景及 25 个既有无 fixture 场景通过；字面量 helper 与 token 清洗的 36 个计划样例也通过内存验证。没有将这些片段写入生产或测试源码，不替代实施阶段的 TDD、Adapter 集成、数据库和浏览器测试。
- 已核对规格 A01–A18 覆盖、现有文件引用、前端脚本及离线 matrix 参数；49 个完整 Python 代码块可解析，其余为标明上下文的插入片段或接口签名。计划文档无未填占位项，Markdown 代码围栏及内部子计划链接检查通过。

### 实施前复核记录（2026-09-04，尚未修改业务代码）

- 当前仍为原工作区 `main`，未创建 linked worktree；隔离工作区与相关未提交基线的带入方式尚待用户确认，不在此状态直接改业务代码。
- 使用已有 `.venv/bin/python -m pytest` 重跑 I1 的同组测试：`81 passed in 5.92s`；未安装依赖。
- PostgreSQL 与 MinIO 的配置目标均为 loopback，TCP 探测可达，前端依赖目录存在。此结果不证明凭据、schema、资源创建权限或集成测试通过；本轮未创建数据库、bucket，未连接真实模型。
- UI-1 原有 Dialog、菜单 key 和手动首次预览的假设已被当前源码否定，已将子计划改为共享字段及独立分段设置页；行为范围不变。

## Task I1：冻结可比较的实施基线

**Files:**
- Read: `AGENTS.md`、`backend/AGENTS.md`、`frontend/AGENTS.md`、`CONTEXT.md`、完整 Spec 和三个子计划。
- Read: 当前 `git diff`、前端字段组件调用者。
- Modify/Create: 无业务文件。

**Interfaces:**
- Consumes: 当前工作区内容及规格 A1–A4。
- Produces: 经核对的文件所有权、版本值和一份带命令/结果的实施基线记录；记录在本次任务输出中，不新增日志框架。

- [ ] **Step 1：确认实现授权与范围。** 用户只要求生成计划时，到本计划交付为止；收到实施指令后才继续下列步骤。B1–B4 不在本计划范围。
- [ ] **Step 2：读取工作区和实际接线，核对计划锚点。** 从仓库根目录执行：

```bash
git status --short
git diff --stat
rg -n 'SPLITTER_VERSION|CLEANER_VERSION|ADAPTER_REVISION|NORMALIZATION_VERSION' backend/packages/knowledge/actweave_knowledge/ingestion/profiles.py backend/packages/knowledge/actweave_knowledge/extraction/runtime_resources.py
rg -n 'KnowledgeChunkSettingsFields|knowledgeTokenUnit|chunkOverlapTokenLabel' frontend/src/components/projects/knowledge
```

预期：确认现有改动归属；尚未实施本计划时 splitter 为 v2、cleaner/adapter 为 v1。若行为或调用拓扑已变化，先更新计划的受影响锚点与样例，不把已有新行为覆盖回旧代码。

- [ ] **Step 3：运行不需要数据库的基线测试。** 从 `backend/` 执行：

```bash
uv run pytest tests/knowledge/test_markdown_chunking.py tests/knowledge/test_index_text.py tests/knowledge/test_knowledge_tokenizer.py tests/knowledge/test_parsing_profiles.py::test_frozen_profile_uses_original_etl_and_refuses_unknown_runtime_versions -q
```

预期：现有断言通过；记录实际数量与耗时。前一轮 81 项通过是历史基线，不替代这次输出。

- [ ] **Step 4：记录可用验证层级。** 确认是否有开发 PostgreSQL、测试用 MinIO、目标平台解析资源、前端依赖及浏览器。不得输出 `.env`、密钥或连接串。缺少某项只标记该门未验证，不编造通过，也不触发安装、建库或部署。
- [ ] **Step 5：审查交付点。** 确认可在不覆盖用户修改的前提下推进指定子计划；无法安全合并重叠改动时暂停并请求协调。

## Task I2：按子计划交付首期行为

**Files:**
- Modify/Test: 仅各子计划明确列出的文件。
- Read: 本总计划的共同约束及覆盖矩阵。

**Interfaces:**
- Consumes: I1 基线。
- Produces: 经各自红绿测试验证的分段、格式和 UI 变更；子计划完成不等于发布完成。

- [ ] **Step 1：执行分段子计划。** 每项测试先看到预期断言失败，再写最小实现；版本拒绝、source/attachment 保留、无空格中文及列表边界必须同时覆盖。
- [ ] **Step 2：执行格式子计划。** 按其 helper/Adapter/Word 任务顺序推进；不能把 helper 套到整段 Markdown，不能只在 Word 一个格式修复字面量。
- [ ] **Step 3：执行 UI 子计划。** 核对最新活跃渲染位置后修改说明；不为添加说明接管字段组件重构，不增加客户端猜测的模型容量或版本阻断。
- [ ] **Step 4：核对公开错误合同。** 后端内部 `ExtractionError.reason_code` 可以是 `PROCESSING_PROFILE_UNAVAILABLE`，但当前 HTTP envelope 使用 `code=KNOWLEDGE_PARSE_FAILED` 和安全 `message`。前端测试不得伪造不存在的 reason_code 字段。
- [ ] **Step 5：审查交付点。** 核对各子计划新增的 symbol 与调用方名称一致，未加入任何 B1–B4 功能；保留各任务实际红绿输出，不提前部署中间状态。

## Task I3：同步用户文档和所有权指南

**Files:**
- Modify: `README.md` 的 Knowledge 段。
- Modify: `backend/AGENTS.md` 的 Knowledge 版本/边界说明。
- Modify: `frontend/AGENTS.md` 的 Knowledge 参数说明。
- Test: 实现版本、文档关键词与最终 diff。

**Interfaces:**
- Consumes: 三个子计划的最终通过行为及版本值。
- Produces: 与实现一致的短说明；不新增功能历史列表，不改其他段落。

- [ ] **Step 1：读当前文档对应段，保留其他任务修改。** 定位命令：

```bash
rg -n 'splitter-v2|splitter-v3|RAG 文件解析|Knowledge Token|Knowledge bases|历史文档' README.md backend/AGENTS.md frontend/AGENTS.md
```

- [ ] **Step 2：在 README 原 Knowledge 说明处用下面的行为文字替换受影响说明。** 保留现有预算、附件与沙箱说明，不重复追加整节：

```text
Token 分段器 splitter-v3 在同一页面和标题组内支持普通正文的尾部重叠，
包括 PDF 页内正文及长段落拆分片段；重叠不跨表格或页面，不复制图片。
非 Markdown 格式的原文字面符号在解析时保留；Word 自定义样式可按明确
大纲级别识别一级至六级标题，不根据字号、加粗或文本外观猜测。
“知识库 Token”不等于所选 Embedding 模型的输入 Token，分段预览不验证
模型输入上限。旧配置不可执行时需显式重新解析；旧 token 父子文档的
手工二次切分同样不会偷偷改用新算法。重新解析会替换人工分段，重新向量化
则保留现有正文与分段，不会自动获得新的解析和分段修复。
```

- [ ] **Step 3：在 backend/AGENTS.md 相邻已有规则中整合以下边界。** 不复制详细算法到指南：

```text
The token splitter (splitter-v3) retains bounded ordinary-prose suffixes
inside the existing page/section boundaries, including PDF page prose and
oversized text fragments. It never overlaps code, table records, or image
occurrences; display/index Token budgets and the 16000-character ceiling
remain independent checks. Direct token re-splitting also rejects an
unsupported frozen splitter or cleaner version instead of executing current logic under
an old identity. Published text and re-embedding do not implicitly reparse.
Adapter literal-text serialization and Word outline-level handling belong to
extraction; adapter-v2 and the verified platform resource lock ship together.
Token cleaner-v2 understands the literal serializer's escaped email characters
and whitespace entities without changing the retained character cleaner.
```

- [ ] **Step 4：在 frontend/AGENTS.md Knowledge 说明中整合以下两句。** 不恢复内部元数据行：

```text
Upload and reparse forms explain that Knowledge Tokens are a fixed local
chunking unit, not the selected embedding model's input capacity, and that
preview does not validate that capacity. Overlap is a bounded maximum within
the supported structural boundaries; parsing-profile failures preserve the
unsaved editor input and never trigger reparse automatically.
```

- [ ] **Step 5：审查交付点。** 阅读三个文件的完整新增 diff；文案只能陈述已通过的行为。尚未验证目标平台的资源锁或真实服务时，在交付记录说明，不把文档变成验证凭证。

## Task I4：跨路径验证与最终交付

**Files:**
- Test: 三个子计划中的新增测试与下列既有门禁。
- Read: 所有修改文件 diff、目标平台资源锁、当前 Spec。
- Modify/Create: 不新增生产功能。

**Interfaces:**
- Consumes: I2/I3 完成的代码、测试与文档。
- Produces: 分层验证结果、残余风险和未授权操作清单。

- [ ] **Step 1：运行后端定向回归。** 从 `backend/` 执行，使用开发环境和框架隔离的测试库：

```bash
uv run pytest tests/knowledge/test_markdown_chunking.py tests/knowledge/test_index_text.py tests/knowledge/test_knowledge_tokenizer.py tests/knowledge/test_parsing_profiles.py tests/knowledge/test_literal_markdown.py tests/knowledge/test_literal_cleaning.py tests/knowledge/test_builtin_text_extractors.py tests/knowledge/test_builtin_office_pdf.py tests/knowledge/test_local_unstructured.py tests/knowledge/test_extraction_resources.py -q
uv run --env-file ../.env pytest tests/knowledge/test_profile_admission.py tests/knowledge/test_parsing_pipeline.py tests/knowledge/test_parsing_governance.py tests/knowledge/test_extraction_cache.py tests/knowledge/test_reembedding.py tests/knowledge/test_authority.py tests/knowledge/test_summaries.py -q
```

预期：受影响断言通过。第二组需要 PostgreSQL；先确认根目录 `.env` 指向获准使用的开发测试服务，不显示其内容。若配置由环境提供，省略 `--env-file`。其中 ingestion harness 使用测试对象存储和假模型，不能据此声称真实 MinIO 或 Provider 已验证。

- [ ] **Step 2：运行独立的真实存储与浏览器门。** 仅在确认配置是测试服务、允许测试资源创建/清理后执行：

```bash
# backend/：测试在临时 knowledge-test-* bucket 中创建并清理自己的对象
uv run --env-file ../.env pytest tests/knowledge/test_storage.py -q
```

```bash
# frontend/
pnpm check
pnpm test
pnpm test:e2e tests/e2e/project-knowledge.spec.ts
pnpm exec playwright test --config playwright.real-backend.config.ts tests/e2e-real-backend/knowledge-real-backend.spec.ts
```

预期：分别记录真实 MinIO、mock 浏览器、真实后端浏览器的结果；任何跳过项明确列出。真实后端浏览器门不得连接生产服务。

- [ ] **Step 3：完成格式与后端本地门。** 在已隔离并能保护用户修改的实施工作区执行：

```bash
# backend/
make format
make lint
make detect-blocking-io
make test
```

`make format` 会修改文件；执行后复查每个变更，只交付本任务所属修改，不能在用户当前脏工作区无差别格式化后回退其他工作。`make test` 依赖开发数据库并创建隔离测试目标，不授权 reset 现有数据库。任何新增格式 diff 都需重新跑对应测试。

- [ ] **Step 4：在实际目标平台验证解析能力。** 按格式子计划的资源构建/校验命令验证各受支持平台。macOS 的离线测试不替代 Linux bubblewrap/资源锁验证；不伪造未获得的摘要或更改资源检查以绕过失败。
- [ ] **Step 5：核对规格覆盖与真实质量边界。** 对照下表逐项附上测试节点及实际结果。无授权真实模型和固定业务查询集时，只报告确定性缺陷修复；不报告 Hit@5/MRR@5 提升。
- [ ] **Step 6：审查最终工作区。** 从仓库根目录执行：

```bash
git diff --check
git diff --stat
git status --short
```

确认未改 schema、依赖、检索融合或配额，未删除 `attach_children`，未覆盖已有前端重构和分词缓存优化。
- [ ] **Step 7：交付并停止。** 汇报真实通过/失败/未验证门、版本升级影响及仍存在的模型容量限制。没有另行授权，不提交、推送、重解析用户文档、调用真实模型或部署。

## 规格验收覆盖矩阵

| Spec 验收 | 子计划/集成责任 |
| --- | --- |
| A01–A03 普通正文、真实 page、预算与进展 | 分段子计划的后缀保留与入站片段重打包测试 |
| A04–A06 原子、配额、来源不丢失 | 分段子计划 + 既有 chunking/source/attachment 回归 |
| A07–A09 字面量、缩进、原生结构旁路 | 格式子计划的 literal helper、Adapter 与 token 清洗表示兼容样例 + 分段子计划的 escape/entity 原子边界 |
| A10 Word 大纲级别 | 格式子计划的 Word 样式继承测试 |
| A11 预览/解析/发布一致 | 两个后端子计划的集成测试 + I4 |
| A12 版本与缓存身份 | 分段子计划的 splitter-v3/cleaner-v2 与 chunk/preview 指纹测试 + 格式子计划的 adapter-v2/parse/资源锁测试 |
| A13 旧配置拒绝与零发送/零发布 | 分段子计划的直接调用/手工治理测试 + 前端错误交互测试 |
| A14 手工父块、null/character | 分段子计划的手工派生与 character 回归 |
| A15 重新向量化、重新解析、摘要发布 | I4 既有 reembedding/parsing_pipeline/summaries 门 |
| A16 三入口、语言、payload、错误输入保留 | 前端子计划 + I4 浏览器门 |
| A17 权限、资源、失败保护 | I4 authority、pipeline、资源与目标平台门 |
| A18 缓存优化数值等价、保留已有改动 | 分段子计划 + I1/I4 |

## 不在本轮自动执行的事项

- B1 表格行合并：需先批准调整逐行语义并提供精确行/跨行查询评测。
- B2 模型容量保护：需先确认真实模型/服务计数和输入模板契约；提示文案不等于超限防护。
- B3 PDF 版面与其他 Word 结构：需有真实失败文件及预期来源标注。
- B4 性能清理：需测量证据，不删除仍用于 character 的代码，不扩展现有缓存优化范围。
- Git 集成与部署：本计划给出技术验收，不授予提交、发布、回退服务或数据迁移权限。

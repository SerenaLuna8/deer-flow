# AGENTS.md 精简设计

日期：2026-09-03

## 目标

三份开发期指南（`AGENTS.md`、`backend/AGENTS.md`、`frontend/AGENTS.md`）只保留
跨模块的硬边界与工作方式，删除实现叙述、历史/路线图说明和重复内容。功能行为
的权威仍是代码与聚焦测试，不再在指南里复述。

## 范围

- 修改：`AGENTS.md`、`backend/AGENTS.md`、`frontend/AGENTS.md`。
- 不修改：`CONTEXT.md`、各级 `README.md`、`CLAUDE.md`、`Install.md`。
- 不新建归档文档；被删除的细节直接删除。
- 不触碰工作区中其他未提交修改。

## 统一格式

- 80 列换行，表格列对齐，章节间一个空行。
- 单条 bullet 不超过约 6 行；只写"必须/禁止 + 边界"，不写实现步骤、
  内部锁顺序细节、历史演进。
- 保留现有章节顺序与 Guide map，方便旧链接和读者习惯。

## 各文件目标

### 根 `AGENTS.md`（约 111 → 90 行）

- 结构不变。
- Schema V1 "显式操作员动作、运行时从不建/改库"目前出现在 Ownership map、
  Command boundaries、Repository-wide rules 三处，合并为一处。

### `backend/AGENTS.md`（约 1290 → 450 行）

- "Where changes live"：把逐文件、逐符号的所有者叙述压成一张
  _路径 → 所有者_ 表，加一条通用规则："兼容 façade 只做 re-export；测试
  patch 拥有者模块，façade 中仍由 façade 调用的 seam 才 patch façade"。
- Jobs/Runs/streams、Governed assets、Memory、Configuration：每条压到规则
  本身；删除 scheduler epoch/finalizer、dead-Job keyset 扫描、v2/v3 legacy
  行细节、`patched_*` 退役说明、"ship separately" 等内容。
- Knowledge：约 370 → 80 行。保留可选模块与 setup 语义、host-agnostic
  依赖方向、MinIO 仅 unversioned/50 MiB 单上传槽、quota port、任务 lease 与
  Project 围栏、所有读写都携带服务端授权、`KNOWLEDGE_CONFLICT` 语义、测试
  conftest 约定。删除分块算法、打分公式、解析器格式清单、字段级说明。
- 原样保留 `tests/test_agents_md_constants.py` 钉住的全部句子（"Guarded
  operational limits" 一节、`current marker is \`schema_v1\``、
`config_version: 1`、`Version 1 is the initial`）。
- Common change paths、Tests and code quality 保留。

### `frontend/AGENTS.md`（约 539 → 280 行）

- 结构不变。
- Knowledge bases 小节约 180 → 40 行：保留授权门、scope 注册、轮询与停止
  规则、晚到响应不得覆盖、409 处理、citation 校验；删除向导布局、
  `overlayClassName`、对话框字段级描述。
- Streams、Governed assets、MCP/models 各 bullet 压到规则本身。

## 验证

- `cd backend && uv run pytest tests/test_agents_md_constants.py -q` 通过。
- 三个文件的 Markdown 链接目标存在。
- 如仓库有 Markdown 格式工具则运行之；否则人工检查表格与换行。

# M11 续作与验证记录

记录日期：2026-09-01。状态：T2–T10 已实现并通过确定性验证；T11 已执行一次真实尝试，但摘要生成未完整，检索质量对照尚未运行。真实质量及目标部署尚未放行。

## 工作区与范围

- 接续提交为 `b9658197`，其中已有 T1 Schema 与包契约；本次补齐数据库设置服务及管理 API、启动装配与迁移、查询向量缓存、摘要生成与生命周期、摘要召回、前后端管理与知识库 UI。
- 代码位于隔离 detached worktree `/Users/jiangfeng/workspace/deer-flow-m11`。没有提交、推送或合回 `/Users/jiangfeng/workspace/deer-flow`。
- 原工作区仍有会话列表、子任务样式等无关修改，且运行配置包含旧 `knowledge` YAML。没有覆盖这些修改、修改原配置或重启原服务。
- T0 历史记录中的目标库名称已过时；本次只读核对实际目标为 `deerflow`。没有重置、迁移或修补该库。数据库测试使用随机 `deerflow_test_*`，对象存储验证使用独立临时 bucket。

## 最终确定性验证

以下为本次代码冻结后的执行结果。Knowledge 专项是后端全量的子集，不累加计数。

| 验证 | 命令或入口 | 结果 |
| --- | --- | --- |
| 后端 core gate | `cd backend && make test` | 5423 passed，0 failed，0 skipped，7 provider_integration deselected；543.69 秒 |
| Knowledge 专项 | `PYTHONPATH=. uv run --no-sync python tests/support/core_gate_plugin.py tests/knowledge -q -m 'not provider_integration'` | 868 passed，0 failed，0 skipped，2 deselected；221.65 秒 |
| Python lint 与格式 | `cd backend && make lint` | 通过；1315 个文件格式一致 |
| Schema 注释 | `PYTHONPATH=. uv run --no-sync python scripts/generate_schema_comments.py --check` | 通过；110 表、1384 列 |
| 前端静态检查 | `cd frontend && pnpm check` | ESLint 与 TypeScript 通过 |
| 前端单测 | `cd frontend && pnpm test` | 1161 passed，206 files，0 failed，0 skipped |
| 前端生产构建 | `cd frontend && pnpm build:production` | 通过；build ID `wvRxVroIeM6rOpcyz06Ee` |
| Chromium mock | `project-knowledge.spec.ts` + `admin-knowledge-settings.spec.ts` | 67 + 6 = 73 passed，0 failed，0 skipped；约 2.1 分钟 |
| Chromium 实际后端 | `knowledge-real-backend.spec.ts` | 11 passed，0 failed，0 skipped；约 1.5 分钟 |
| 隔离安装与配置迁移 CLI | setup → check-db → migrate 两次 → check-db | 五次命令均 exit 0，schema_v1 / ready，详情见下文 |
| 补丁空白检查 | `git diff --check` | 通过 |

后端命令执行前在隔离工作区的 `backend/` 中运行以下环境加载。测试 fixture 会替换应用连接并派生随机测试库；不要把此方式误用为授权操作目标库。

```sh
set -a
source ../.env
set +a
```

日志留存于本机临时目录：

- `/tmp/deer-flow-m11-backend-delivery.log`
- `/tmp/deer-flow-m11-knowledge-delivery.log`
- `/tmp/deer-flow-m11-frontend-final-check.log`
- `/tmp/deer-flow-m11-frontend-final-unit.log`
- `/tmp/actweave-m11-final-build.log`
- `/tmp/deer-flow-m11-build-production-delivery.log`
- `/tmp/deer-flow-m11-mock-browser-final.log`
- `/tmp/deer-flow-m11-knowledge-real-browser-final.log`

浏览器实际后端使用随机 PostgreSQL、独立 MinIO bucket、独立 Worker 及 loopback replay HTTP 模型。它验证真实 HTTP、持久任务、存储和页面联动；它不验证外部模型的召回质量、费用或 Provider 可用性。没有执行全部 Provider 集成门、全浏览器矩阵或目标部署门。

浏览器用例运行于 build ID `dWRuWcZI2mfpWrVVPQSs9`，由 Playwright webServer 的 `pnpm build` 生成；仓库 `next.config.js` 的默认 BUILD_MODE 即 production。之后对同一冻结代码另跑显式 `pnpm build:production`，得到上表中的构建 ID；没有把两个构建混记为同一个 artifact。

实际浏览器命令（工作目录 `frontend/`）：

```sh
E2E_REUSE_EXISTING_SERVER=1 pnpm exec playwright test \
  --config playwright.real-backend.config.ts \
  tests/e2e-real-backend/knowledge-real-backend.spec.ts --reporter=line

PLAYWRIGHT_SKIP_WEB_SERVER=1 PLAYWRIGHT_BASE_URL=http://127.0.0.1:3128 \
  pnpm exec playwright test tests/e2e/project-knowledge.spec.ts \
  tests/e2e/admin-knowledge-settings.spec.ts --workers=1 --reporter=line
```

第一个命令只复用了本任务自建的 Gateway 8117 和 Next 3317。Gateway 使用 `run_replay_gateway.py --port 8117 --cors http://localhost:3317`，设置 `ACT_WEAVE_REPLAY_BOOTSTRAP_SCHEMA=1`、`ACT_WEAVE_REPLAY_REPEAT_COUNT=2`、`E2E_REPLAY_WORKER_MODE=immediate` 与临时 readback 路径。Next 设置 `PORT=3317`、`SKIP_ENV_VALIDATION=1`、`ACT_WEAVE_AUTH_DISABLED=1`、测试占位 `BETTER_AUTH_SECRET`、`ACT_WEAVE_INTERNAL_GATEWAY_BASE_URL=http://127.0.0.1:8117`。

第二个命令复用同一构建的临时 Next 3128；它运行时 8117 无监听，两份 mock spec 拦截 `/api/**`。所有自建服务均已停止，不复用用户的 3000/8001 服务。

## 隔离安装与迁移演练

使用随机库 `deerflow_test_68251_1ea0495261864dd4ac69a84c4350b5ce`；每个子进程显式设置该库的 `DATABASE_URL`，未使用 `uv --env-file`，避免被原配置覆盖。临时 master key 与合成 bootstrap 凭据只用于这个库，无 Provider 或 MinIO 调用。

从 `backend/` 执行的五次命令均退出 0：

```text
uv run python scripts/setup_postgres.py --database <随机库>
make check-db
uv run python scripts/migrate_knowledge_config.py --config <临时合成YAML>
uv run python scripts/migrate_knowledge_config.py --config <同一临时合成YAML>
make check-db
```

- 安装后及迁移后均为 `schema_v1 / ready / 健康`。
- 默认设置一行，revision 1、enabled false、无 secret；两次迁移后仍一行，revision 2→3、enabled true，非敏感字段与输入一致。
- nonce 长度、密文不含明文、解密匹配和两次重加密产生不同 envelope 均验证通过。
- 独立维护连接复查清理后数据库记录数 0、该库连接数 0，临时 YAML 已删除。
- 安全证据：`/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/actweave-m11-cli-proof-1ea0495261864dd4ac69a84c4350b5ce.json`，完成于北京时间 2026-09-01 00:08。

这是当前 Schema 上的安装和配置数据迁移演练，不表示可以原地升级不兼容的旧数据库。

## 安全与行为证据

| 约束 | 主要回归证据 |
| --- | --- |
| 设置凭据只写、加密持久化，响应/日志/审计/repr/诊断不泄漏 | `backend/tests/test_knowledge_settings_postgres.py`、`tests/knowledge/test_host_config.py`、前端管理设置单测和 Chromium 管理页用例；浏览器写操作不进入 mutation/query cache |
| 探测在事务外，返回后重查管理员权限与 revision | PostgreSQL 设置测试中的 CAS 冲突、探测中并发写入、撤权、失败保持旧设置；修改 endpoint 必须重输 secret |
| 缓存只省查询 Embedding 调用，不缓存检索授权或结果 | `tests/knowledge/test_retrieval_query_cache.py` 中开关、冷/热、并发、模型分组、重建模型切换和两阶段撤权回归 |
| 摘要只参与召回，不替代真实分段引用或工具正文 | `tests/knowledge/test_summary_retrieval.py`、`test_summary_gateway.py`；真实浏览器检索同时验证 `matched_via=summary` 与原文内容 |
| 摘要须满足 Project、lease、版本、开关、绑定和内容摘要约束 | `tests/knowledge/test_summaries.py` 中迟到发布、过期 lease、Project 失活、生成中编辑/新增、旧行保留和后续补偿任务回归 |
| 文档 ready 不因摘要失败消失；重嵌入不重复生成摘要 | 摘要生命周期单测及实际后端浏览器失败重试、重嵌入 chat_calls 不增加、重解析旧分段消失用例 |
| 热缓存中途改变摘要开关仍触发策略冲突 | `test_disabling_summary_index_during_warm_search_conflicts_before_citations_return`；策略快照包含 summary flag |
| 存储异常可降级，留存清理不能假成功 | `test_host_config.py`、`test_worker.py`、`test_knowledge_settings_postgres.py`；包括非法 endpoint 和接受连接但不回应的 S3 peer；readiness 只公开状态，不含端点 |

MinIO 探测复用其依赖中的 HTTP 客户端，为探测单独设置 2 秒 socket 超时并关闭 SDK 重试，外层保留 10 秒等待边界。正常上传与清理客户端行为不变。该改动修复原来外层取消仍等待 SDK 长时间 socket/retry 结束的问题。

首次完整后端运行出现 1 项 replay descriptor 旧预期失败；修正后最终全量通过。测试现在明确要求保留原生 Provider descriptor 的所有字段，仅替换 replay class_path。没有放宽生产凭据验证。

## 与原计划的必要调整

- 包内原来有七张 Knowledge 表，M11 增为八张，另有宿主设置表；不是原文所写的八到九。
- 既有 replay 向量约定对含 marker 的原文给低相似度，对不含 marker 的摘要给高相似度。因此摘要输出使用 `摘要索引回放 <source-sha256-prefix>`，保留旧 Embedding/Rerank 合同，验证 summary 单独命中；没有反转旧分数规则来迁就计划示例。
- 摘要关闭 SDK 内部重试，交给持久任务在 lease guard 后重试；ModelRuntime 仅增加可向下收紧的调用级重试上限，既有调用默认不变。
- 管理设置含 secret 的写请求采用有账户代次约束的直接 API，不使用持久 mutation 状态。已有账户切换 abort 对合法开发账户 ID 的清理异常按同域惯例改为无请求的安全返回；请求 schema 验证不变。
- Replay 旧文本模型种子和旧 UI helper 与 `b9658197` 的现有 Provider 所有权、空库创建和上传向导已经不一致；修复测试基础设施，未回退产品流程。
- 结构化管理请求的格式校验沿用公共管理路由的 `RELIABILITY_INVALID` 422；Knowledge 设置服务的业务校验使用自身错误码。没有为一个管理页复制公共认证/校验路由。

## T11 语料与首次真实执行

- 语料共 85 题、40 份文档，其中 dev 36、holdout 49。新增 question_style 的 dev/holdout 各 10 题；原 65 题和 20 份文档的冻结内容由 hash 回归保护。
- 扩充语料 SHA-256：`2195184ccf91ead63318b26183e3fe8501b64a38de3c3079a1505165bdbef83e`。
- 评测走生产 Base 开关、持久摘要任务和真实 ModelRuntime；支持 semantic/hybrid × summary on/off、两次查询的缓存验证、逐次调用预算、质量/无答案/P95 与冻结 M10 基线对照。
- 初始未执行报告已由真实运行报告替换；不能把测试中的模拟指标用于放行 F02。

用户随后回复“确认执行”。本次按一轮新 M10 等价基线与一轮 M11 尝试执行；摘要沿用当前默认 DeepSeek V4 Flash 的原生 `deepseek` 配置和正确 Provider 绑定，将摘要尝试硬上限限定为此前报价范围的最低数量 24。没有自动增加该上限。

### M10 等价基线

- [M10 报告](m10-quality-eval-report.md)：原 65 题、130 次检索、10002 个检索单元，实测无请求错误，召回质量通过；标注 `fresh_m10_equivalent`，不冒充历史实测。
- 命令：在 `backend/` 加载测试环境后，`ACT_WEAVE_KNOWLEDGE_QUALITY_EVAL=1 PYTHONPATH=.:tests:tests/knowledge uv run --no-sync pytest tests/knowledge/test_m10_quality_eval.py::test_m10_holdout_quality_gates_against_real_models -q -s --tb=short`。日志：`/tmp/deer-flow-m11-fresh-m10-quality.log`。原运行 793.32 秒。
- 自然语言非 Provider P95：semantic 111.59ms、hybrid 566.63ms，约 5.08 倍。该测量不证明具体耗时原因，也不是隔离整台机器负载的性能实验。
- 旧评测器把复审触发条件自动写成“产品接受”，因此原 pytest 曾报 1 passed。这一错误的审批判断已纠正：当前 `quality_passed=true`、`all_passed=false`、`p95_review_pending=true`、`p95_review_recorded=false`。没有重新调用 Provider；仅从原始 summary 离线重算 gate 并重新渲染，除 gates 外全部 JSON 顶层数据与原始副本严格一致。
- 旧费用估计使用未核验的历史费率，不是实际账单；不将其作为当前已确认费用。

### M11 第一次尝试

- [保留的第一次报告](m11-quality-eval-attempt-1.md)及其 JSON：`measurement_provenance=authorized_provider_run`，模型 `deepseek-v4-flash`，原生 adapter `deepseek`。
- 摘要尝试计数 **24/24**，发布 **18/24** 条摘要，**3** 个文档任务未完成。计数在运行时调用前递增，不等同于 24 个成功 HTTP 响应。
- 单文档的摘要全部生成后才原子发布；其中 `tail-answers` 有 4 个分段。因此缺少 6 条摘要不能解释为 6 次 API 失败。
- 生成中只读观察到 3 个任务在 `summarizing` 阶段、第一次尝试后进入重试；最终重试因预算耗尽终止。旧诊断先脱敏再覆盖错误，现有证据不能确定首次错误是超时、限流、空响应还是其他原因。
- 由于生成未完整，评测主动跳过后续 **680 次**检索：`outcomes=0`、`rerank_calls=0`。当前没有 M11 Recall/nDCG/P95 对照结果，不能将其描述为实测召回退化。
- 只读离线构建真实默认模型的请求 payload，验证 `thinking=disabled`、`max_tokens=1024`、`reasoning_effort=null`、SDK `max_retries=0`；禁止了 HTTP 发送，未新增模型调用。
- 日志：`/tmp/deer-flow-m11-real-quality.log`。任务脚本为 `.superpowers/sdd/2026-08-31-rag-knowledge-m11/run-real-m11.py`，通过 `run_m11_quality_eval` 显式传 native template、正确 endpoint 和内存中的独立 key；未走默认 SiliconFlow pytest 摘要配置。
- 临时库 `deerflow_test_74797_d119a2a6d77e49fca62c058afd84d6df` 已清理；独立维护连接复核该库记录和连接数均为 0。没有对象存储或浏览器服务需要保留。

继续条件：补齐不泄漏内容/凭据的评测诊断后，需要单独确认下一轮摘要调用上限（已询问是否授权新一轮最多 36 次；此前 24 次另计）。M10 测量可复用，无需重复计费补测。性能批准和目标部署仍独立待确认。

目标数据库处置和代码切换是另一项操作者决定。原库缺少当前 Schema，旧 YAML 也尚未迁移；现在直接覆盖原运行目录会使热重载失败。因此代码保持隔离，前后端与 Schema 应按 [Install.md](../../Install.md#knowledge-configuration-migration) 一起切换。此前的重置授权不延伸至本次。

## 清理

本次开的浏览器上下文与页面均已关闭，未关闭用户自己的 tab。独立 Next、Gateway 和 Worker 已停止；回读确认本次 replay 数据库及临时 MinIO bucket 不再存在。清理不影响原工作区服务。

# RAG 附件、完整缓存与配额 Implementation Plan（P2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可授权读取、可重试删除、按真实对象事实计费的 Extraction/Attachment 生命周期，供 P3 原子发布使用。

**Architecture:** PostgreSQL 先登记身份、配额和任务引用，MinIO 只保存字节。对象 I/O 位于短事务之间；完整 manifest 是唯一解析缓存，发布指针、Segment 和附件绑定通过同文档复合外键与同一发布事务一致切换。宿主实现项目配额端口，包内不导入宿主。

**Tech Stack:** Python 3.12、SQLAlchemy async、PostgreSQL/Schema V1、MinIO 单 PUT、pytest。

**Spec:** [权威规格](../specs/2026-08-31-rag-document-parsing-design.md) §5、§7.2–7.3、A14–A20/A23/A26/A28；同时阅读[总计划](2026-08-31-rag-document-parsing.md) §3.2、§4。

## Global Constraints

- ActWeave 核对基线 `b96581974b057c0ae4d853815130d99c0ed23823`；本包接入前必须先完成 M11 既有计划的 PostgreSQL 配置读取与摘要生命周期整合，记录实际新基线。不重新实现 M11，不新增 YAML 配置来源。
- P1 已交付总计划 §3 的 frozen 类型、manifest codec、`run_extraction` 和测试 helpers；本包不能发明第二套 `Document` 或 `ParseProfile`。
- 原文件继续 ≤50 MiB，MinIO 每次单 PUT，复用现有每 store 单上传槽。
- 图片最多 100 个独立字节对象，每张 ≤5 MiB、≤20,000,000 像素，单文档图片合计 ≤50 MiB。
- manifest 规范 JSON ≤50 MiB；每次解析工作目录合计 ≤512 MiB。
- 当前 published extraction 不回收；至多保留一个完整未发布缓存 24 小时，数据库时间判定，活跃任务引用阻止回收。
- `actweave_knowledge` 不得导入 `app.*` 或 `deerflow.*`；宿主依赖注入必须是必需参数，不以生产 no-op quota 实现补齐构造参数。
- 新 schema 只在新空测试数据库安装；不运行目标库 reset/ALTER、启动补表或降级。需要保留已有数据库时另立部署迁移方案。
- 本轮只写计划，不实施、安装依赖、访问数据库/模型或提交。执行时先建隔离工作区；只在当时用户明确授权时提交对应任务文件，禁止 `git add -A` 或自动 push。

## 已核对的接入位置与实现边界

| 当前源码 | 本包负责的变化 |
| --- | --- |
| `persistence/models.py` 已有 M11 `KnowledgeSegmentSummaryRow`、Task summarize kind | 三张新表及 P3 要用的列在 P2 一次交付；保留已有 M11 schema，不按旧“八张表”口径推断 |
| `documents/service.py::_create_uploading_row/_publish_queued_document/_cleanup_failed_upload` | 原件纳入同一配额与上传事实；不只给派生图片计量 |
| `storage/minio_store.py::upload_from` | 保留 `_upload_slot`、`require_unversioned_bucket`、`run_sync_to_completion`、单 PUT；补统一实际字节上限和有界校验读取 |
| `tasks/worker.py::KnowledgeTaskClaim` | 继续使用现有 claim；持久 pin 在 Task 行，不新增另一种 claim |
| `tasks/deletion.py::_drain_documents/purge_project_knowledge` | 清理 manifest/图片/staging 后才清理原件和权威行；保留 Project quiescence 与一日上传 settlement grace |
| `app/quotas/service.py` | `ProjectUsageCounterRow` 已有 `used/reserved`，但当前 storage reserve/release 全写 reserved，校准把 used 清零。需要真正扩展两轴结算，不能把新增端口叫作已经实现 |
| `app/quotas/integration.py::reconcile_project_storage` | 当前只统计 ready PrivateFile 与 Project Skill；补 Knowledge 三类对象，保留既有资源口径 |
| `segments/service.py::get_segment_detail` | 当前传任一 expected 参数就要求 ready/current；不能直接用它实现“有 expected 的管理图片”读取，需抽出明确管理/引用 guard |
| `app/knowledge/composition.py` | 把真实 quota adapter 同时注入 feature module 与独立 Project purger；disabled retention 不依赖启用解析模块 |

所有生产路径都以数据库行的 project/base/document 作用域为准。`base_id` 是 Python 参数名，数据库列保持现有 `knowledge_base_id`；document 的数据库列为 `knowledge_document_id`。三类对象使用唯一数据库 UUID 作 quota key：document.id、attachment.id、extraction.id（manifest）；服务器不重用已释放 ID。

## P2-T1：同项目、同文档、同提取世代的 schema（A15/A16/A26/A28）

**Files**

- Modify: `backend/packages/knowledge/actweave_knowledge/persistence/models.py`
- Modify: `backend/packages/harness/deerflow/persistence/knowledge_settings/model.py`
- Modify: `backend/packages/harness/deerflow/persistence/full_schema.sql`
- Modify: `backend/packages/harness/deerflow/persistence/final_schema_contract.py`
- Modify: `backend/packages/harness/deerflow/persistence/final_schema_digest.py`
- Modify: `backend/scripts/check_postgres.py`
- Modify: `backend/scripts/generate_schema_comments.py`
- Regenerate: `backend/packages/harness/deerflow/persistence/schema_comments.sql`
- Modify: `backend/tests/knowledge/test_schema_repository.py`, `CONTEXT.md`, `backend/AGENTS.md`
- Create: `backend/tests/knowledge/extraction_test_helpers.py`（本任务先提供安装上下文）
- Create: `backend/tests/knowledge/test_extraction_schema.py`

**Consumes:** 当前 `KnowledgeOrmBase`、真实 `_install_full_schema(engine)`、`postgres_database_url` 随机测试库 fixture；P1 `ProcessingProfile`/`SourceSpan`。

**Produces:** `KnowledgeExtractionRow`、`KnowledgeAttachmentRow`、`KnowledgeSegmentAttachmentRow`；下表的唯一字段命名；`installed_knowledge_sessions(url)` 异步上下文管理器。P3 只消费本任务新增 schema。

- [x] **先添加 schema red 测试和可复用安装上下文。** helper 不导入任何 `test_*.py` 的私有 fixture：

```python
# tests/knowledge/extraction_test_helpers.py
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from deerflow.persistence.bootstrap import _install_full_schema

@asynccontextmanager
async def installed_knowledge_sessions(url: str):
    engine = create_async_engine(url)
    try:
        await _install_full_schema(engine)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
```

```python
# tests/knowledge/test_extraction_schema.py
import pytest
from sqlalchemy import text
from extraction_test_helpers import installed_knowledge_sessions

@pytest.mark.asyncio
async def test_same_document_and_generation_constraints(postgres_database_url):
    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        async with sessions() as session:
            rows = (await session.execute(text("""
                SELECT conname, pg_get_constraintdef(oid) AS definition
                FROM pg_constraint
                WHERE conname IN (
                  'fk_knowledge_extractions_document',
                  'fk_knowledge_attachments_extraction',
                  'fk_knowledge_segment_attachments_segment',
                  'fk_knowledge_segment_attachments_attachment',
                  'fk_knowledge_documents_published_extraction',
                  'fk_knowledge_segments_published_extraction')
            """))).mappings().all()
        assert len(rows) == 6
        constraints = {row['conname']: row['definition'] for row in rows}
        assert 'project_id, knowledge_base_id, knowledge_document_id' in constraints[
            'fk_knowledge_attachments_extraction']
        assert 'DEFERRABLE INITIALLY DEFERRED' in constraints[
            'fk_knowledge_segments_published_extraction']
```

Run: `cd backend && PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_extraction_schema.py -q`。期望约束数量断言失败，不把数据库未配置当作 red。

- [x] **一次补全 ORM 与 SQL，采用以下列契约。** 所有 scope UUID 非空，所有 JSON 列有 `jsonb_typeof` 检查；SHA 是 64 位小写十六进制；size 用 BigInteger，非负且受对象上限约束。

| 对象 | 新字段及约束 |
| --- | --- |
| Extraction | `id, project_id, knowledge_base_id, knowledge_document_id, source_sha256, parser_fingerprint, normalization_version, state, manifest_storage_key, manifest_sha256, manifest_size_bytes, manifest_upload_state, manifest_quota_state, created_task_id, created_attempt, created_claim_token, target_document_version, created_at, completed_at, unpublished_expires_at, delete_error` |
| Attachment | `id, extraction_id, project_id, knowledge_base_id, knowledge_document_id, sha256, media_type, size_bytes, width, height, storage_key, state, upload_state, quota_state, delete_error`；唯一 `(extraction_id,sha256)`；只允许 image/png、image/jpeg、image/webp |
| SegmentAttachment | `project_id, knowledge_base_id, knowledge_document_id, extraction_id, segment_id, attachment_id, position, alt_text`；主键 `(segment_id,position)`，position 从 1 起；不能以 attachment_id 作 position 唯一键而丢重复出现 |
| Document | `source_sha256` nullable、`published_extraction_id` nullable、`parsing_profile` nullable JSON object（完整 `ProcessingProfile={parse,chunk}`）、`parse_warnings` JSON array、`capability_revision` nullable、`upload_state`、`quota_state` |
| Segment | `extraction_id` nullable、`index_text` Text 默认空串、`token_count` Integer ≥0 默认0、`source_spans` JSON array 默认[] |
| Child | `index_text`、`token_count`、`source_spans`，同 Segment 的类型/默认值 |
| Task | `extraction_id` nullable；仅 ingest/reembed/summarize 可带 pin，且只允许 open status；增加 `delete_extraction` kind、resource_id 唯一 open-delete partial index；该 kind 的 target_version/storage_key 均为NULL |
| KnowledgeSystemSettingsRow | `etl_type` String(32) NOT NULL DEFAULT 'dify'，CHECK值为dify/unstructured_local；`extraction_cache_enabled` Boolean NOT NULL DEFAULT true。P3只实现读取/管理投影，不再新增DDL |

状态闭包：Extraction/Attachment `state=staging|ready|deleting`；三类对象的 upload fact 是 `pending|stored|delete_pending|deleted`；三类 quota fact 是 `unreserved|reserved|committed|released`。manifest 未登记前 key/hash/size/quota 为 NULL/NULL/0/unreserved；manifest 登记后 key、hash、size必须同时存在。`ready` 必须 manifest stored、committed、completed_at 非空。Attachment `ready` 必须 stored+committed。`deleted` 对象可短暂保留 reserved/committed 供失败后校准释放；`released` 必须 deleted。pending/delete_pending 不能据父行 state 推断用量。

`created_task_id/attempt/claim_token` 是不可变来源证据，不给 created_task_id 加阻止现有终结任务清理的 FK；新建时在 Store 事务验证三者。`Task.extraction_id` 是真正活跃引用，使用 `(project_id,extraction_id)` 复合 FK；终结/恢复过期 claim 必须清空 pin，不能由 CASCADE 删除活跃 task。

- [x] **添加复合约束，避免仅靠 Python 检查跨文档绑定。** 关键 ORM 形状如下（其余列按上表逐一声明）：

```python
# KnowledgeAttachmentRow.__table_args__
ForeignKeyConstraint(
    ['project_id', 'knowledge_base_id', 'knowledge_document_id', 'extraction_id'],
    ['knowledge_extractions.project_id', 'knowledge_extractions.knowledge_base_id',
     'knowledge_extractions.knowledge_document_id', 'knowledge_extractions.id'],
    name='fk_knowledge_attachments_extraction', ondelete='RESTRICT'),
UniqueConstraint('extraction_id', 'sha256', name='uq_knowledge_attachments_hash'),
UniqueConstraint('project_id', 'knowledge_base_id', 'knowledge_document_id',
                 'extraction_id', 'id', name='uq_knowledge_attachments_scope'),
```

Extraction 对 Document 使用现有三列 scope FK，ondelete RESTRICT。Extraction 添加 `(project_id,id)` 与 `(project_id,knowledge_base_id,knowledge_document_id,id)` 唯一约束。Document 的 published pointer 使用四列复合 FK 指向 Extraction，`DEFERRABLE INITIALLY DEFERRED`。Document 再添加 `(project_id,knowledge_base_id,id,published_extraction_id)` 唯一约束；Segment 的 `(project_id,knowledge_base_id,knowledge_document_id,extraction_id)` 以 deferred FK 引用它，确保已提交段只属于当前 published extraction。旧 character 段可保持 extraction_id NULL；有绑定的段不允许 NULL。

Segment 添加 `(project_id,knowledge_base_id,knowledge_document_id,extraction_id,id)` 唯一约束；SegmentAttachment 分别以 scope+extraction+segment_id、scope+extraction+attachment_id 引用 Segment/Attachment。Segment 删除可以 CASCADE 删除纯关系，Attachment/Extraction 的字节所有权 FK 保持 RESTRICT。不要把需要延迟验证的 FK 写成不可延迟的 RESTRICT。

- [x] **完成行为级 SQL 测试。** 在本文件添加测试自用 `seed_scope(sessions)->tuple[project_id,base_id,document_id]`：独立事务 INSERT users（id/email/username/system_role/created_at/needs_setup/token_version）、projects（id/slug/display_name/created_by_user_id），用公开 `registry_helpers.seed_provider/seed_embedding_model` 建库所需模型，再 INSERT Base/Document。每次 UUID 和 email/username 唯一。用同一函数生成两套作用域，显式 INSERT Extraction/Attachment/Segment 后验证：跨 project、跨 base、跨 document、跨 extraction 的 binding 各报 `sqlalchemy.exc.IntegrityError`；更换 published pointer 而保留旧 Segment 在 commit 时失败；同一事务替换全部段/绑定/指针成功；重复图片两处 position 可成功但重复 `(extraction_id,sha256)` 失败。这些 INSERT 必须指定实际持久化事实，不能靠默认 pending 冒充对象已存储。

- [x] **同步 catalog、中文注释、领域术语。** `CONTEXT.md` 增加 Knowledge Extraction / Knowledge Attachment / Attachment Occurrence，说明 ready extraction 不等于 ready document。更新 `FINAL_APP_TABLES`、REQUIRED_TABLES、`KNOWLEDGE_TABLES`，通过现有 schema 测试输出取得新 catalog signature/digest；不能手写猜测摘要，也不修改 `schema_v1` 名称。中文注释生成器补所有新列和状态含义，使用 `uv run python scripts/generate_schema_comments.py` 生成 SQL。

- [x] **green 与本任务审阅。**

```bash
cd backend
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_extraction_schema.py tests/knowledge/test_schema_repository.py -q
uv run python scripts/generate_schema_comments.py --check
uvx ruff check packages/knowledge/actweave_knowledge/persistence tests/knowledge/test_extraction_schema.py tests/knowledge/extraction_test_helpers.py
```

检查 `git diff --check`，审阅 deferred FK、NULL 兼容与全部字段。记录随机测试库门的实际结果；只有获授权才提交本任务列出的文件。

## P2-T2：真实宿主字节配额与两轴校准（A28）

**Files**

- Create: `backend/packages/knowledge/actweave_knowledge/storage/quota.py`
- Create: `backend/app/knowledge/quota_port.py`
- Modify: `backend/app/quotas/models.py`, `backend/app/quotas/service.py`, `backend/app/quotas/integration.py`
- Modify: `backend/app/knowledge/composition.py`, `backend/app/gateway/deps.py`, `backend/app/worker/app.py`, `backend/packages/knowledge/actweave_knowledge/module.py`, `backend/packages/knowledge/actweave_knowledge/project_retention.py`
- Modify: `backend/tests/knowledge/extraction_test_helpers.py`
- Create: `backend/tests/knowledge/test_knowledge_storage_quota.py`
- Modify: `backend/tests/test_private_upload_discard_postgres.py`

**Consumes:** P2-T1 对象事实；`QuotaService` 的当前 policy reader、HMAC source ref 与 append-only ledger；总计划 §3.2 的三个 quota 方法。

**Produces:** 必需注入 `KnowledgeStorageQuotaPort`；`HostKnowledgeStorageQuotaPort(quotas:QuotaService)`；`StorageUsageTotals(used:int,reserved:int)`；保持 PrivateFile/Skill 原有 reserved 口径的混合校准。

- [x] **定义完整共用 harness（不使用冒充配额的 fake）。** 继续扩展 `extraction_test_helpers.py`。新增 `ExtractionHarness` dataclass：`session_factory, claim, project_id, base_id, document_id, object_store, quota, quota_service`；`store` 是延迟 property，首次访问从 P2-T3 的 `storage.extractions` 创建 `ExtractionStore(session_factory=...,object_store=...,quota=...,project_active_check=is_knowledge_project_active,cache_enabled=True)`。这样 T2 配额测试不依赖尚未实现的 Store。

`extraction_harness(postgres_database_url, *, quota_bytes=524288000)` 使用 `installed_knowledge_sessions`，以 T1 公开化后的 `seed_scope` 建 users/project/base/document；原件为固定 `b'original'`，size=8、source_sha256=SHA256(original)、upload_state=stored。新增一个 target_version=1 的 queued ingest Task，调用现有 `claim_next_task(session,lease_seconds=600)`，逐字段构造既有 `KnowledgeTaskClaim`，不要凭空制造无对应数据库 claim。

真实 quota service 构造如下；静态 config 只限测试，生产沿用宿主 current_policy_reader：

```python
import hashlib
import hmac
from app.quotas.models import QuotaSourceRef
from app.quotas.service import QuotaService
from deerflow.config.quota_config import QuotaConfig
from app.knowledge.quota_port import HostKnowledgeStorageQuotaPort

def test_quota_hash(payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(key_id='extraction-test',
        hmac_hex=hmac.new(b'extraction-fixture-key', payload, hashlib.sha256).hexdigest())

quota_service = QuotaService(
    session_factory=sessions,
    config=QuotaConfig(default_storage_bytes_limit=quota_bytes),
    source_ref_hasher=test_quota_hash,
)
quota = HostKnowledgeStorageQuotaPort(quota_service)
# 同一事务：给已插入的原件 reserve，然后 commit；object_store预置原件字节。
```

harness 公开 `read_rows()`：用 session 查询本 scope 的 Documents/Extractions/Attachments/SegmentAttachments/Tasks，返回固定键 `documents/extractions/attachments/bindings/tasks`；离开 session 前完整加载，不能触发脱离会话 lazy read。`published_result()` 在 P2-T4 定义，之后返回 `StoredExtraction`。`seed_scope` 必须移到 helper 并在 T1 测试改为公开导入。

- [x] **写真实两轴 red 测试。** 在 T2 helper 增加 `register_test_attachment(size_bytes, upload_state='pending')`：当前 scope 插入 staging Extraction 和 Attachment，服务器生成两个 UUID，返回 attachment_id；只登记不 reserve，方便校验准入顺序。该 fixture manifest 保持未登记状态，不编造存储对象。

```python
import pytest
from sqlalchemy import select
from deerflow.persistence.quotas.model import ProjectUsageCounterRow
from app.quotas.integration import ProjectQuotaEnforcer
from extraction_test_helpers import extraction_harness

@pytest.mark.asyncio
async def test_commit_moves_axes_and_reconcile_keeps_knowledge(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        object_id = await h.register_test_attachment(size_bytes=17)
        async with h.session_factory() as s, s.begin():
            await h.quota.reserve(s, project_id=h.project_id, object_id=object_id, size_bytes=17)
            await h.quota.reserve(s, project_id=h.project_id, object_id=object_id, size_bytes=17)
        async with h.session_factory() as s, s.begin():
            from actweave_knowledge.persistence.models import KnowledgeAttachmentRow
            row = await s.get(KnowledgeAttachmentRow, object_id, with_for_update=True)
            row.upload_state = 'stored'
            await h.quota.commit(s, object_id=object_id)
            await h.quota.commit(s, object_id=object_id)
        async with h.session_factory() as s, s.begin():
            await ProjectQuotaEnforcer(h.quota_service).reconcile_project_storage(s, h.project_id)
            counter = await s.scalar(select(ProjectUsageCounterRow).where(
                ProjectUsageCounterRow.project_id == h.project_id,
                ProjectUsageCounterRow.dimension == 'storage_bytes'))
            assert (counter.used, counter.reserved) == (25, 0)  # 8原件 + 17附件
```

Run: `cd backend && PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_knowledge_storage_quota.py -q`。期望缺少端口或轴迁移断言失败。

- [x] **实现宿主 port 与精确幂等规则。** 包的 `storage/quota.py` 仅定义 Protocol，三个签名与总计划 §3.2 逐字一致。宿主 `quota_port.py` 的 `load_object_fact(session,object_id,*,for_update)->KnowledgeObjectFact` 依次查询 Document/Attachment/Extraction manifest，恰好命中一个且对象已登记才有效；`KnowledgeObjectFact` 提供 `project_id,size_bytes,upload_state,quota_state` 及拥有行引用。同一 UUID 命中多种实体、reserve 的 project/size 不匹配、已 released 后再次 reserve 都映射 `KNOWLEDGE_CONFLICT`。不存在对象不是可用于释放的客户端凭证。

`reserve` 在拥有行登记事务调用 `_issue_project_storage_quota_authority(project_id,operation='reserve')` 与 `QuotaService.mutate_project_storage(...,size,'knowledge-object:'+str(object_id))`。只有创建新 reservation 才把 fact 改 reserved；重复原值不新增账目。`QuotaExceeded`→`KNOWLEDGE_QUOTA_EXCEEDED`，`QuotaConflict`→`KNOWLEDGE_CONFLICT`，`QuotaUnavailable`→`KNOWLEDGE_STORAGE_UNAVAILABLE`，公开消息不含原异常、ID或key。

- [x] **扩展宿主 storage 专用 commit/release，而不重写全域配额。** 新增 `QuotaService.commit_project_storage(session,authority,amount,idempotency_key)->None`，trusted authority 的 operation 增加 `commit`。锁 project、同一个 storage counter，复验 exact reserve ledger，并用既有 `_source_ref/_idempotency_digest` 为 `storage_commit_debit` 与 `storage_commit_credit` 生成成对 ledger（-amount、+amount，满足 delta<>0）；已存在两条且匹配则直接返回，只有一条是冲突。移动 `counter.reserved -= amount; counter.used += amount`，总量不变，不能再次做“当前限额是否仍够”的新准入。port 仅在 upload_state=stored 且 quota_state=reserved 时调用，然后改 committed。

```python
# commit_project_storage 锁 counter、验证reserve及commit幂等之后的关键部分
if counter.reserved < amount:
    raise QuotaConflict('storage reservation is missing')
counter.reserved -= amount
counter.used += amount
counter.version += 1
# 使用现有 repository.append_ledger 两次追加上述 debit/credit；不修改旧ledger。
```

现有 `mutate_project_storage` 增加仅内部使用的 `storage_axis:Literal['reserved','used']='reserved'`；release 根据对象 quota_state 选择轴，仍要求 exact reserve ledger，保留现有 release 幂等 key。除 port 外现有调用不传该参数，行为不变。port `release` 只接受 upload_state=deleted；已 released 幂等返回；数据库删除确认和 release 同事务，完成后才可删拥有行。零字节原件保留事实但不写 delta=0 ledger。

- [x] **让校准保留两轴并能修复“已删但未释放”。** `StorageUsageTotals` 放 `app/quotas/models.py`，字段校验非负。`QuotaService.reconcile_project_storage` 的 expected_loader 扩为返回 `int|StorageUsageTotals`；原 int 仍表示 `(used=0,reserved=int)`。新的 storage 分支按两个目标轴分别计算差异并追加既有 reconciliation 类型账目；不得走旧 `_reconcile_locked` 将 used 清零。别的 quota dimension 不变。

`ProjectQuotaEnforcer.reconcile_project_storage` 的 loader：PrivateFile/Skill 保留原统计全部加入 reserved；Knowledge `reserved`+未确认删除对象加入 reserved；`committed`+未确认删除对象加入 used，`delete_pending` 不改变原轴；deleted 行在已锁同 counter 的事务中用 exact source key 完成 release（或确认已有 release）后标 released，不再加入任何轴。每个对象只统计一次，manifest是否完整不影响已 stored 的字节。锁顺序采用 project→所属业务行→counter，校准先取 project 锁再枚举业务行，禁止 counter→业务行与对象登记形成反向锁。

- [x] **补充混合门并注入生产路径。** 增加测试：原件+pending附件+stored附件+stored但未完整manifest+delete_pending的混合轴总量；同ID不同size/project冲突；quota不足零PUT；存储删除失败不release；校准两次不新增计量；PrivateFile/Skill既有用量不清零；已deleted但release前崩溃由校准修复；commit后额度收紧不使已准入对象失败。所有 constructor/factory 接收必需 quota port，M11 后实际 composition 接收 Gateway `app.state.project_quota_service`；Worker在 `app/worker/app.py` 将现有嵌套构造提为局部 `quota_service`，同一实例供ProjectQuotaEnforcer和Knowledge port，继续传 `SystemQuotaPolicyReader` 与 `AuditHmacKeyring`，不创建第二份policy；项目 purger 即使 feature_module=None 仍注入真实 port。

```bash
cd backend
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_knowledge_storage_quota.py tests/test_private_upload_discard_postgres.py -q
uvx ruff check app/quotas app/knowledge/quota_port.py tests/knowledge/test_knowledge_storage_quota.py
```

检查 diff 与账目非零约束；只在获授权后提交这些明确文件。

## P2-T3：先登记再 PUT、单对象幂等与 I/O 结算（A15/A16/A18/A23/A28）

**Files**

- Create: `backend/packages/knowledge/actweave_knowledge/storage/extractions.py`
- Create: `backend/packages/knowledge/actweave_knowledge/storage/extraction_keys.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/storage/minio_store.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/documents/service.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/persistence/tasks.py`
- Modify: `backend/tests/knowledge/extraction_test_helpers.py`, `backend/tests/knowledge/test_storage.py`, `backend/tests/knowledge/test_upload.py`
- Create: `backend/tests/knowledge/test_knowledge_attachments.py`

**Consumes:** P1 `LocalAttachment/ParseProfile`；P2-T1 行；P2-T2 quota；现有 `KnowledgeTaskClaim`、`ProjectActiveCheck`、`_lock_live_task_claim`。

**Produces:** 总计划的 `ExtractionReservation/StoredExtraction`；`ExtractionStore.begin/persist_attachment/enqueue_cleanup`（本任务先交付耐久排队，T5交付执行器与GC）；`lock_extraction_claim(session,claim)->tuple[KnowledgeTaskRow,KnowledgeDocumentRow]`（只供包内部并供 P3 发布复用）；派生 key grammar。

- [x] **实现可观察的对象 double 与 barrier，再写 red。** helper 中 `RecordingObjectStore` 持有 `objects:dict[str,bytes]`、`calls:list[tuple[str,str]]`、`failures:set[str]`、`barriers:dict[str,IOBarrier]`。实现现有 store 的 upload_from/download_to/delete/delete_many/require_unversioned_bucket/delete_project_objects，文件读写只在测试中进行；operation 固定 `put|get|delete|bucket`。`fail_next(operation)` 在下一次匹配调用消费并抛安全 KnowledgeError。`pause(operation)` 返回下面的 barrier；每个操作记录 calls 后 `await barrier.enter()`，再执行/故障；不依赖 sleep。

```python
import asyncio
from dataclasses import dataclass, field

@dataclass
class IOBarrier:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)
    async def enter(self):
        self.entered.set()
        await self.released.wait()
```

用 P1 数据类型构造一张测试图；这里字节可用固定 Pillow 生成的 1×1 PNG，生产 sink 已负责安全归一化。新增 helper `write_test_asset(work_dir)->LocalAttachment`：Pillow 保存1×1 PNG到 `asset.png`，计算真实sha/size并返回 `LocalAttachment(attachment=Attachment(ref=sha,media_type='image/png',size_bytes=size,width=1,height=1),relative_path='asset.png')`。

```python
import asyncio
import pytest
from extraction_test_helpers import extraction_harness, write_test_asset
from parsing_test_helpers import make_parse_profile

@pytest.mark.asyncio
async def test_put_has_committed_registration_and_reservation(postgres_database_url, tmp_path):
    async with extraction_harness(postgres_database_url) as h:
        asset = write_test_asset(tmp_path)
        source = (await h.read_rows())['documents'][0].source_sha256
        reservation = await h.store.begin(h.claim, source_sha256=source,
                                         profile=make_parse_profile('.pdf'))
        gate = h.object_store.pause('put')
        pending = asyncio.create_task(h.store.persist_attachment(reservation, asset, work_dir=tmp_path))
        await gate.entered.wait()
        rows = await h.read_rows()  # 另一连接可见，证明登记事务已提交
        assert len(rows['attachments']) == 1
        assert rows['attachments'][0].quota_state == 'reserved'
        assert rows['attachments'][0].upload_state == 'pending'
        gate.released.set()
        await pending
        await h.store.persist_attachment(reservation, asset, work_dir=tmp_path)
        assert len([call for call in h.object_store.calls if call[0] == 'put']) == 1
```

Run: `cd backend && PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_knowledge_attachments.py -q`，期望缺少 Store 接口失败。

- [x] **实现 claim/版本/作用域 guard 与 begin。** 将 `_lock_live_task_claim` 包装为明确内部可复用的 `lock_extraction_claim`：先 `project_active_check`（宿主锁Project）；再锁 live Task并以数据库 clock_timestamp 检查lease；核对 task.id/token/attempt/resource/project/target_version 等于 claim；锁 Base/Document 并检查非deleting、Document.version==target_version；锁等待后再次读 DB 时间验证lease。失租约用既有 `KNOWLEDGE_TASK_FAILED`，版本/对象不一致用 `KNOWLEDGE_CONFLICT`，inactive沿用 `KnowledgeProjectInactive`。

在T1 ORM/SQL同时添加 `(knowledge_document_id,created_task_id,created_attempt,created_claim_token)` 唯一约束。`begin` 检查 source_sha256 与 Document.source_sha256、canonical_parse_fingerprint(profile)，在同一事务插入 staging extraction、不可变创建 task/attempt/token/target_version，并将 Task.extraction_id 指向它。同 `(document_id,created_task_id,created_attempt,created_claim_token)` 重复 begin 返回原 reservation，不产生第二个 extraction；旧 claim 不可续写。项目暂停后的任务恢复沿用原重试次数但得到新 claim_token，因此必须创建独立 extraction，不接管旧 claim 仍可能结算的对象。保留暂停不消耗失败重试次数的既有契约。begin不提前预留未知manifest大小。

- [x] **严格分离登记、网络、settle。** `persist_attachment` 解出 `work_dir / relative_path`，拒绝绝对路径、`..`、symlink及resolve逃逸；实际size/sha/MIME/像素必须匹配 P1 Attachment，不能相信 IPC 的声明。server key 用独立 `attachment_storage_key(project_id,base_id,document_id,extraction_id,sha256,media_type)`，MIME固定映射 png/jpg/webp；另有 `manifest_storage_key(...)/manifest.json`。`is_extraction_storage_key` 核对全部scope与明确两种尾部，不能复用/放宽原件 `is_document_storage_key`。

关键控制流：

```python
# ExtractionStore.persist_attachment 中的事务边界；guard/登记在本任务定义。
async with self._sessions() as session, session.begin():
    task, document = await self._lock_reservation(session, reservation)
    row = await self._register_attachment(session, reservation, asset.attachment)
    if row.upload_state == 'stored':
        return
    await self._quota.reserve(session, project_id=row.project_id,
                              object_id=row.id, size_bytes=row.size_bytes)
    key, object_id = row.storage_key, row.id
# _register_attachment只接受本extraction，同sha重复必须验证全部metadata一致。
await self._objects.upload_from(key, local_path, media_type=asset.attachment.media_type)
async with self._sessions() as session, session.begin():
    task, document = await self._lock_reservation(session, reservation)
    row = await session.get(KnowledgeAttachmentRow, object_id, with_for_update=True)
    row.upload_state = 'stored'
    await self._quota.commit(session, object_id=object_id)
    # attachment ready仅表示该图片完整；extraction仍staging直至manifest完整。
    row.state = 'ready'
```

`_lock_reservation` 用 reservation.task_id读task并重建既有claim，核对persisted created_claim_token/attempt/target_document_version；不能仅凭 reservation 本身授权。`_register_attachment` 在 extraction 锁内检查独立图数量/累计size，再insert或load既有hash。相同hash两次回调不重复PUT，但 Document 中多个 occurrence 不去重。

- [x] **明确失败路径与原件配额。** 上传调用失败（包括“可能已写入但响应丢失”）不得把quota减回零；登记行转deleting/delete_pending，本任务实现 `enqueue_cleanup` 的耐久 admission：锁同scope Extraction、拒绝published/live pin、转换deleting并通过T1 partial index幂等INSERT kind=delete_extraction且storage_key=NULL；T5再交付执行handler与GC。该排队记录精确extraction；排队前在同一补偿事务只清除仍属于本task/token/attempt的pin；若新claim已经接管或其它活跃任务pin同一Extraction，不清它的pin、不排删除，由当前引用结束后的GC处理。传播存储错误，不能作为单图warning。PUT成功后失lease/版本，必须先 drain已发IO，再以只允许标记已登记对象为待删的补偿事务保存stored事实/结算并排cleanup；补偿无权发布/创建新asset。数据库不可达则已提交pending行与reservation保留，启动恢复处理。

取消使用现有 `run_sync_to_completion`，直到已开始PUT结束才把取消传上去。不得在async task取消时立刻删除工作目录。原件 upload 的三个现有阶段同样执行 reserve→PUT→commit；失败删除确认后release，不把上传行直接delete而漏释放。原件 late-put任务仍只收原件grammar；原件恢复tombstone必须恢复同object_id及计量事实。

- [x] **加入有界ObjectInfo验证与green。** `MinioObjectStore.upload_from` 在实际 `stat()` 后拒绝 `>50*1024*1024`（不只依赖上传HTTP准入）；保留one-slot与单PUT。新增 `stat_object(key)->StoredObjectInfo(size_bytes:int,sha256:str|None)`，SHA来自服务器设置的metadata（不是multipart ETag）；upload_from在上传槽内用分块读取计算SHA256，并在fput_object的metadata参数写入sha256，不能接收客户端任意metadata。`download_to` 支持 `max_bytes:int|None`，先stat拒绝超限、再复制、完成后复查实际size；copy不可信大对象时采用受限读到max+1后终止，不能先无界fget再检查。SDK调用全部在既有run_sync adapter中。完整SHA检查在T4/T7完成。

补测试：quota不足未PUT；同ID内容不一致冲突；两个同图occurrence一个对象；登记失败未PUT；PUT后失lease不ready；数据库settle失败保留pending预留；cancel等待barrier释放才完成；超过单PUT上限拒绝；跨scopekey拒绝；旧原件delete_document_object不接受派生key。

```bash
cd backend
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_knowledge_attachments.py tests/knowledge/test_upload.py tests/knowledge/test_storage.py -q
uvx ruff check packages/knowledge/actweave_knowledge/storage app/knowledge/quota_port.py
```

检查diff；只在授权后提交本任务文件。T3阶段不得把未完整Extraction接入摄取默认路径。

## P2-T4：完整 manifest 缓存、持久 pin 与一致性校验（A14/A15/A18/A23/A28）

**Files**

- Modify: `backend/packages/knowledge/actweave_knowledge/storage/extractions.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/persistence/tasks.py`
- Modify: `backend/tests/knowledge/extraction_test_helpers.py`
- Create: `backend/tests/knowledge/test_extraction_cache.py`

**Consumes:** P1 `encode_manifest/decode_manifest/canonical_parse_fingerprint`；P2-T3 Store；P2-T1 Task.extraction_id。

**Produces:** 总计划 `ExtractionStore.complete/find_ready` 与 `StoredExtraction`；完整性通过后才返回result；`published_result()` fixture。

- [x] **提供确定性的完整结果fixture与red。** helper新增 `make_extraction_result(profile,*,source_sha256,attachments=())`：用P1 `make_document` 生成两个独立Document（page=1、page=2），第二页含warning `ParseWarning(code='IMAGE_EXTERNAL_SKIPPED',message='未抓取外链图片',source_position={'page':2})`，如传asset则按ref在第一页、第二页分别生成有各自SourceSpan的occurrence；创建 `ExtractionResult(documents=...,attachments=tuple(asset.attachment...),warnings=...,source_sha256=...,parse_fingerprint=canonical_parse_fingerprint(profile))`。不能flatten成一个长字符串。

`published_result()`：取Document实际摘要、make_parse_profile('.pdf')，begin→complete无附件fixture；测试事务设置Document.published_extraction_id、published_version=version、status=ready，settle对应Task并clear pin；返回StoredExtraction。这只是建立供读取/删除用的持久fixture，不替代P3发布测试。

```python
import pytest
from actweave_knowledge.extraction.contracts import ExtractionLimits
from extraction_test_helpers import extraction_harness, make_extraction_result
from parsing_test_helpers import make_parse_profile

@pytest.mark.asyncio
async def test_cache_preserves_pages_warnings_and_has_no_second_put(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        source = (await h.read_rows())['documents'][0].source_sha256
        profile = make_parse_profile('.pdf')
        result = make_extraction_result(profile, source_sha256=source)
        reservation = await h.store.begin(h.claim, source_sha256=source, profile=profile)
        first = await h.store.complete(reservation, result)
        puts = len([c for c in h.object_store.calls if c[0] == 'put'])
        cached = await h.store.find_ready(h.claim, source_sha256=source,
                                         profile=profile, limits=ExtractionLimits())
        assert cached is not None
        assert cached.extraction_id == first.extraction_id
        assert cached.result == result
        assert len(cached.result.documents) == 2
        assert cached.result.documents[1].warnings
        assert len([c for c in h.object_store.calls if c[0] == 'put']) == puts
```

Run: `cd backend && PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_extraction_cache.py -q`，期望complete/find_ready未实现失败。

- [x] **完成manifest登记/PUT/完整ready。** complete验证result.source/fingerprint等于reservation行，去重Attachment清单与数据库ready附件集合精确一致，所有occurrence.ref存在且metadata一致。`encode_manifest`返回规范字节，size>50MiB直接失败；写入工作目录占512MiB预算，不能截断。短事务登记服务器manifest key、SHA、size、pending与quota reserve；提交后单PUT；下载/校验对象sha,size及 `decode_manifest` 后，重新锁Project→Task→Document→Extraction，确认附件对象与manifest全完整，manifest_upload_state=stored并quota.commit；设置Extraction.ready、completed_at=数据库clock_timestamp、unpublished_expires_at=completed_at+24h。complete本身不改published pointer、不建segment、不settle索引Task。

manifest_path仅存在Store控制的临时目录，不能进入StoredExtraction/result/数据库profile；网络/文件调用全部在事务外。

- [x] **缓存查找先pin再I/O。** find_ready只接受同document/source_sha256/parser_fingerprint/normalization_version，cache_enabled=false直接None；缓存键不含ChunkProfile、任务attempt、临时路径。锁当前任务/文档后选published或最新且未过期ready，并锁Extraction，将Task.extraction_id设置为候选后提交；随后进行有界manifest GET→hash→decode→条目/位置/附件清单校验，对每个附件stat size与SHA（没有可信SHA元数据时做有界GET并实际hash），全部检查成功才返回。最后再次锁原claim与同Extraction确认仍ready且pin一致。

```python
payload = await self._read_manifest_bounded(candidate, limits)
if len(payload) != candidate.manifest_size_bytes:
    raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, '提取缓存不完整')
if hashlib.sha256(payload).hexdigest() != candidate.manifest_sha256:
    raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, '提取缓存校验失败')
result = decode_manifest(payload, limits)
if result.source_sha256 != source_sha256 or result.parse_fingerprint != fingerprint:
    raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, '提取缓存身份不一致')
```

`_read_manifest_bounded` 在本文件实现：以独立TemporaryDirectory、max_bytes=manifest上限调用store.download_to，hash在实际下载文件计算，退出清理目录；始终无数据库事务。按规格 §7.3，已确认的缓存字节损坏或对象不存在，在有效 claim 下清除本 task pin 后返回未命中，由 P3 本次任务重新提取，不返回部分 Document 或把图片缺失当 warning。未发布损坏候选排耐久 cleanup；published 损坏保留旧发布身份和对象，直到新提取结果成功原子发布。权限/租约/版本失败、预算超限、存储传输或访问错误继续 typed 失败，不能视为未命中。无须用户关闭全局 cache 设置。跨项目/跨文档摘要相同也不能命中。

- [x] **保持pin生命周期与缓存容量。** 更新claim_next_task接管expired claim时先清旧attempt pin，并更新所有task success/failure/defer-expired最终路径：只有该claim停止外部IO后才清pin；正常embedding/publish期间保持pin，cache验证也在pin下。单纯heartbeat不更新completed_at/TTL。新ready完成时在同文档锁下按 `(completed_at DESC,id)` 选保留一个非published完整缓存，更旧且无活跃pin者排cleanup；正在执行的索引任务暂时持有的旧缓存可延期回收，引用结束后恢复上限。不能按created_task_id误删新attempt持有的缓存。

- [x] **green覆盖所有缓存身份轴。** 增加测试：chunk size/overlap变化仍命中（find_ready只传ParseProfile）；source/parser版本/normalization/image_policy/header规则变化miss；manifest单字节损坏/确认对象或附件缺失返回miss并成功重提取发布，超大/权限/存储传输失败不得fallback；第二页来源和多处重复图片完全相等；complete失败旧published不动；stat/GET间撤权或失lease拒绝；下载barrier期间GC不能删除pin；热命中quota总量与两个轴均不变；无附件result合法。

```bash
cd backend
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_extraction_cache.py tests/knowledge/test_knowledge_storage_quota.py tests/knowledge/test_tasks.py -q
```

检查diff与pin每条终结分支；只在授权后提交本任务文件。

## P2-T5：24小时回收与耐久 delete_extraction（A15/A16/A19/A28）

**Files**

- Create: `backend/packages/knowledge/actweave_knowledge/tasks/extraction_deletion.py`
- Create: `backend/packages/knowledge/actweave_knowledge/storage/extraction_gc.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/storage/extractions.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/tasks/__init__.py`, `backend/packages/knowledge/actweave_knowledge/tasks/worker.py`, `backend/packages/knowledge/actweave_knowledge/module.py`
- Create: `backend/tests/knowledge/test_extraction_gc.py`
- Modify: `backend/tests/knowledge/extraction_test_helpers.py`（新增真实cleanup claim helper）

**Consumes:** P2-T1 task kind/pin、T2 quota、T3 exact-key store、T4 complete。

**Produces:** `ExtractionStore.enqueue_cleanup(extraction_id,*,project_id)`；`KnowledgeExtractionDeletionHandler(session_factory,object_store,quota,project_active_check)` callable(claim)；`enqueue_extraction_gc(session,*,project_active_check:ProjectActiveCheck,project_id:UUID|None=None,limit:int=100)->int`；供T6使用的 `delete_registered_extraction(...,project_id,extraction_id,allow_published=False)->bool` 内部函数，allow_published仅在T6已撤下段/指针且完成quiescence的内部调用，HTTP无此输入。

- [x] **写删除失败不丢权威行的red。** 给harness增加 `claim_cleanup(extraction_id)`：调用store.enqueue_cleanup，然后在短事务 `claim_next_task`，确认kind为delete_extraction后构造既有claim；不能调用handler传任意resource_id冒充claim。原ingest task先settle清pin。

```python
import pytest
from actweave_knowledge import KnowledgeError
from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler
from actweave_knowledge.persistence.tasks import settle_task_success
from extraction_test_helpers import extraction_harness, make_extraction_result
from parsing_test_helpers import make_parse_profile
from app.knowledge.composition import is_knowledge_project_active

@pytest.mark.asyncio
async def test_delete_failure_keeps_manifest_row_and_charge(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        profile = make_parse_profile('.pdf')
        source = (await h.read_rows())['documents'][0].source_sha256
        r = await h.store.begin(h.claim, source_sha256=source, profile=profile)
        stored = await h.store.complete(r, make_extraction_result(profile, source_sha256=source))
        async with h.session_factory() as s, s.begin():
            await settle_task_success(s, h.claim.id, h.claim.claim_token)
        claim = await h.claim_cleanup(stored.extraction_id)
        h.object_store.fail_next('delete')
        handler = KnowledgeExtractionDeletionHandler(session_factory=h.session_factory,
            object_store=h.object_store, quota=h.quota,
            project_active_check=is_knowledge_project_active)
        with pytest.raises(KnowledgeError):
            await handler(claim)
        row = (await h.read_rows())['extractions'][0]
        assert row.state == 'deleting'
        assert row.manifest_quota_state == 'committed'
        assert row.delete_error
```

Run: `cd backend && PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_extraction_gc.py -q`。

- [x] **实现幂等cleanup admission。** enqueue_cleanup在scope正确的短事务锁Document/Extraction，拒绝当前published与活跃Task pin；未发布允许state staging/ready→deleting，不删除行。使用 `INSERT ... ON CONFLICT DO NOTHING` 对T1 open-delete partial index去重。已有failed delete Task则新增有全额attempt的queued Task；delete_error保持到成功，不把重试伪装为已删除。resource_id仅Extraction UUID；Task.storage_key必须NULL；handler从数据库枚举允许key。resource缺失幂等成功、跨project同ID拒绝，不接受任意key列表。

- [x] **实现对象先于权威行的删除。** 每批锁scope+claim/Extraction，复验无当前published/pin，标对象delete_pending后提交；逐对象delete并确认absence（MinIO unversioned remove成功后stat缺失；缺失是幂等成功）；短事务再验scope、标upload_state=deleted、quota.release，然后删除Attachment行。最后用同序列删除manifest、释放extraction.id，确认无SegmentAttachment/Task pin再删Extraction。失败单独短事务记录安全delete_error并抛存储错误，Worker保留retry_wait/failed；保留其余成功进度，重试不二次扣费。

```python
# 一件已登记附件的核心顺序；对象key已在前一事务取出并验证grammar。
await object_store.delete(key)
await object_store.require_absent(key)  # 新增方法，只有NoSuchKey/Object视为确认
async with session_factory() as session, session.begin():
    row = await session.get(KnowledgeAttachmentRow, attachment_id, with_for_update=True)
    if row is not None:
        if (row.project_id, row.extraction_id) != (project_id, extraction_id):
            raise KnowledgeError(KNOWLEDGE_CONFLICT, '清理作用域变化')
        row.upload_state = 'deleted'
        await quota.release(session, object_id=row.id)
        await session.delete(row)
```

`require_absent` 添加到MinioObjectStore和RecordingObjectStore；存在对象/无法确认不release。不能把stat权限拒绝当NoSuchKey。clear pointer不能在通用handler里完成；published删除由T6撤销作用域先行处理。

- [x] **GC采用数据库时间、活跃pin与上传settlement隔离。** `enqueue_extraction_gc` 以DB `clock_timestamp` 计算：失去有效创建claim的staging、过期未发布ready、更旧完整缓存均可候选；按project/document/id锁序复验，排耐久delete任务而非后台协程delete。`ready` TTL从completed_at+24h计算；任一unexpired running indexing Task.extraction_id保护候选。staging含pending PUT的不直接认定对象不存在：沿用现有一日upload settlement grace，先标deleting并排任务，运行中由Worker join已发IO后再放开清理。晚到PUT的settle/补偿必须重新确保对应登记tombstone和cleanup仍存在，不能只因state已deleting就return丢掉字节。

GC 通过显式必需的 `project_active_check` 获取宿主 Project 锁，不导入宿主 ORM、不从 session.info 或环境变量推断权限；候选按项目排序，先锁 Project 再锁本包资源。Worker startup和每轮 `_run_once` 在独立于claim的短事务中先调用有界GC admission；一次最多100，SKIP LOCKED防维护争用，不新增Scheduler解析职责。inactive Project不执行清理handler，保留现有60秒不消耗attempt的defer；最终retention走T6不受feature enabled影响。

- [x] **故障与保留矩阵green。** 用SQL `unpublished_expires_at=clock_timestamp()-interval '1 second'` 制造过期，不改主机时钟。测试published永不选；未过期最新一个保留；第二个完成后旧未引用缓存被排；旧缓存有pin则延期；expired claim恢复清pin后排；另外attempt staging不被旧claim补偿误删；双GC并发同资源一项open任务；delete成功/settle失败重试无重复release；versioned bucket拒绝；网络不确定保留charge；重试后对象数、权威行数归零。

```bash
cd backend
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_extraction_gc.py tests/knowledge/test_worker.py tests/knowledge/test_tasks.py tests/knowledge/test_storage.py -q
```

核对没有fire-and-forget删除或未join对象I/O，检查diff；授权后才能提交。

## P2-T6：文档、库、Project删除及disabled retention（A19/A28）

**Files**

- Modify: `backend/packages/knowledge/actweave_knowledge/tasks/deletion.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/project_retention.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/documents/service.py`
- Modify: `backend/app/knowledge/composition.py`
- Modify: `backend/tests/knowledge/test_tasks.py`, `backend/tests/knowledge/test_worker.py`, `backend/tests/knowledge/test_upload.py`
- Create: `backend/tests/knowledge/test_extraction_retention.py`
- Modify: `backend/AGENTS.md`, `README.md`

**Consumes:** T5精确对象删除、T2真实quota；现有 `_prepare_project_task_quiescence/_defer_or_recover_uploads`、`KnowledgeProjectPurger`。

**Produces:** 完整 `_drain_documents`，原有Document/Base/Project入口无需接受客户端派生对象key；feature disabled仍能清理全部历史对象。

- [ ] **定义删除范围fixture并写red。** helper增加 `seed_retention_family()`：从`published_result()`建published extraction，再以新document.version/task创建一份ready未发布和一份失claim staging，各登记一张图片及manifest（staging只写图片），最后保留一份另一project数据作为作用域控制。返回`dict[project_id,set[str]]`对象key集合，只有测试使用key断言。`published_result`完成task，因此新task需调用真实claim函数。该helper不调用真实模型。

```python
import pytest
from actweave_knowledge.tasks import purge_project_knowledge
from extraction_test_helpers import extraction_harness

@pytest.mark.asyncio
async def test_project_purge_removes_all_registered_generations(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        keys_by_project = await h.seed_retention_family()
        # helper内部所有任务先结束；没有尚未join的I/O，也没有近期uploading原件。
        done = await purge_project_knowledge(h.session_factory, h.object_store,
                                            project_id=h.project_id, quota=h.quota)
        assert done is True
        assert not (keys_by_project[h.project_id] & h.object_store.objects.keys())
        for project_id, keys in keys_by_project.items():
            if project_id != h.project_id:
                assert keys <= h.object_store.objects.keys()
        rows = await h.read_rows()
        assert not rows['documents'] and not rows['extractions'] and not rows['attachments']
```

Run: `cd backend && PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_extraction_retention.py -q`。

- [ ] **先撤执行与可见引用，再删字节，最后删权威行。** Document/Base删除准入保持现有deleting/version CAS，暂停对应活跃索引；handler等待已发IO结束。在Document锁下，删除SegmentAttachment/Segment/Child关系、清published_extraction_id（Document继续保留deleting tombstone），同事务提交；此刻用户已不能读取该作用域。然后枚举该document所有Extraction调用T5删除函数；三代全部确认删除后才delete原件、require_absent、quota.release(document.id)，最后delete Document。relationship删除不被宣称为字节删除，整个期间完整对象登记行保留。

```python
# _drain_documents 中每个Document的持久清理顺序（函数在本任务具体实现）
await withdraw_document_segments(session_factory, project_id=project_id, document_id=document_id)
await drain_document_extractions(session_factory, object_store, quota,
                                project_id=project_id, document_id=document_id)
await object_store.delete(original_key)
await object_store.require_absent(original_key)
async with session_factory() as session, session.begin():
    row = await session.get(KnowledgeDocumentRow, document_id, with_for_update=True)
    if row is not None:
        row.upload_state = 'deleted'
        await quota.release(session, object_id=document_id)
        await session.delete(row)
```

`withdraw_document_segments` 仅接受已deleting且无活跃任务的document，按project/base/doc作用域delete纯关系、clear pointer；`drain_document_extractions` 按稳定ID枚举直到空，遇一个失败就保留Document错误供现有用户删除动作重试。`_derived_delete_error` 应包括派生Extraction/Attachment安全错误，不只原件Task。

- [ ] **维持Project retention独立性。** 在 `purge_project_knowledge` 先沿用现有quiescence及upload grace；清除过期/暂停Tasks前要clear extraction pin，不能违背T1 FK。若任一live task或近期uploading存在，返回False且对象/权威行不动。随后对全部Project Document用新drain；保留最后服务器Project prefix sweep，成功后才移除Base/Task/Query。prefix sweep只用于该已授权Project最终retention，不是常规缓存GC的授权来源。

`KnowledgeProjectPurger` 的历史数据探测扩展包含Extraction/Attachment和非succeeded delete_extraction；即使历史Document行异常缺失也不能误判无待清理工作。settings.minio缺失但存在上述行时返回可重试失败，提示恢复原配置，不能认为feature disabled就成功。port的release允许pending_deletion项目（释放不能被active准入挡住）。保持30天Project retention窗口和原一日upload grace不变。

- [ ] **补故障恢复、文档/库和disabled三入口测试。** 逐个注入原件/manifest/中间图片delete失败，断言仍有scope tombstone、剩余权威行、对应用量；重试清空且每对象只release一次。disabled composition产生`feature_module=None`但`project_purge`可执行；删除配置缺失fail closed；Project restore之前retention不启动task；其它project不受影响；staging尚有liveIO时defer而不提前释放。更新README与backend指南描述对象所有权、删除错误可见、禁用模块不等于免除保留存储，勿把设计阶段结果写成已验证部署。

```bash
cd backend
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_extraction_retention.py tests/knowledge/test_tasks.py tests/knowledge/test_upload.py tests/knowledge/test_worker.py tests/knowledge/test_host_config.py -q
```

检查diff与清理顺序；授权后仅提交本任务文件。

## P2-T7：管理/引用图片读取与本包验收（A17/A19/A20/A22）

**Files**

- Create: `backend/packages/knowledge/actweave_knowledge/storage/attachment_reads.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/segments/service.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/module.py`
- Modify: `backend/app/knowledge/gateway.py`
- Modify: `backend/tests/knowledge/test_knowledge_attachments.py`, `backend/tests/knowledge/test_search_details.py`
- Create: `backend/tests/knowledge/test_attachment_api.py`
- Modify: `backend/tests/knowledge/extraction_test_helpers.py`, `backend/AGENTS.md`, `README.md`

**Consumes:** T1绑定/发布scope，T3有界对象读，既有KnowledgeProjectAuthority与管理/引用Segment可见规则；expected版本指向Segment已发布世代而非Document最新失败target。

**Produces:** `KnowledgeModule.download_segment_attachment(project_id,document_id,segment_id,attachment_id,target_path,*,expected_document_version:int,expected_content_digest:str,authority)->AttachmentReadMetadata` 和 `download_citation_attachment(project_id,base_id,document_id,segment_id,attachment_id,target_path,*,expected_document_version:int,expected_content_digest:str,authority)->AttachmentReadMetadata`；Metadata只含`media_type,size_bytes`；总计划§3.4两条GET路径。方法用途由服务入口确定，客户端无manage=true之类绕过开关。

- [x] **写授权与下载后撤权red，并定义真实绑定fixture。** helper新增 `bind_test_attachment(stored,asset)->tuple[segment_id,attachment_id,digest]`：在一个事务确认stored属于h.document、查同extraction sha的ready attachment，创建Segment.content包含该ref、extraction_id=stored.extraction_id、document_version=published_version；INSERT绑定position=1；digest=SHA256(content UTF-8)。为需要两处引用的测试再插position=2，不能省掉occurrence。

新增可复用测试authority：`ToggleKnowledgeAuthority(project_id,actor_user_id)`，`revoked=False`；`revalidate(session)`如果revoked抛`KnowledgeError(KNOWLEDGE_NOT_FOUND,'资源不存在')`，否则查询Project是否active。仅作为包读服务的撤权double，真实membership/capability要通过已有ASGI授权测试覆盖。

```python
import asyncio
import pytest
from actweave_knowledge import KnowledgeError
from actweave_knowledge.storage.attachment_reads import KnowledgeAttachmentReadService
from extraction_test_helpers import extraction_harness

@pytest.mark.asyncio
async def test_download_revalidates_after_copy(postgres_database_url, tmp_path):
    async with extraction_harness(postgres_database_url) as h:
        # seed_attachment_read是本任务helper：使用T3写1x1图、T4完整结果、设置published后
        # 调用bind_test_attachment，返回segment_id/attachment_id/digest/authority。
        segment_id, attachment_id, digest, authority = await h.seed_attachment_read(tmp_path)
        service = KnowledgeAttachmentReadService(session_factory=h.session_factory,
                                                object_store=h.object_store)
        gate = h.object_store.pause('get')
        output = tmp_path / 'download.png'
        pending = asyncio.create_task(service.download_managed(h.project_id,h.document_id,
            segment_id,attachment_id,output,expected_document_version=1,
            expected_content_digest=digest,authority=authority))
        await gate.entered.wait()
        authority.revoked = True
        gate.released.set()
        with pytest.raises(KnowledgeError):
            await pending
        assert not output.exists()
```

Run: `cd backend && PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_knowledge_attachments.py -q`。

- [x] **抽出共用读取guard，明确管理与引用差异。** 在`segments/service.py`新增纯查询内部 `load_managed_segment(session,project_id,document_id,segment_id,*,expected_document_version,expected_content_digest)`，返回Segment/Document/Base；新增 `load_citation_segment(...,base_id,...)` 在同scope及expected校验上额外检查Base active、Document ready/enabled、Segment enabled、segment.document_version==document.version==published_version。管理读要求Base/Document非deleting、Document.published_version非空、Segment.document_version==published_version，允许Document failed/queued/processing以及停用Segment/Document。两者expected均必须与Segment.document_version和SHA256(content)一致；不把Document.version作为管理读expected。

现有get_segment_detail按原有调用用途接到guard，保持其公开兼容；不能因改guard让引用端点读取停用内容。scope找不到/删除走404，版本digest变化走409，当前成员capability不足由宿主依旧403。

- [x] **附件绑定必须查DB且复制后再查。** ReadService分别提供download_managed/download_citation两个入口；私有 `_download(...,loader)` 的loader由上述入口选定，不能由请求query指定。事务内 `revalidate_project_authority`，调用对应guard，再join SegmentAttachment+Attachment+Extraction，检查所有project/base/doc、attachment.extraction_id==document.published_extraction_id==segment.extraction_id、Attachment.state=ready/upload_state=stored、Extraction.ready及binding存在。捕获稳定metadata，退出事务后有界download（≤5MiB）、实际sha检查，再开启新事务重复完整authority+guard+绑定/sha检查；不只校验“用户还在项目”。任一失败清理target_path并传播安全错误。

```python
# attachment_reads.py内部，两次load分别拥有自己的事务；middle只有字节I/O。
before = await self._load_authorized_snapshot(loader, authority, scope, expected)
try:
    await self._objects.download_to(before.storage_key, target_path, max_bytes=5*1024*1024)
    actual = await run_sync_to_completion(lambda: hashlib.sha256(target_path.read_bytes()).hexdigest())
    if actual != before.sha256:
        raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, '图片内容校验失败')
    after = await self._load_authorized_snapshot(loader, authority, scope, expected)
    if after != before:
        raise KnowledgeError(KNOWLEDGE_CONFLICT, '图片引用已变化')
    return AttachmentReadMetadata(media_type=after.media_type, size_bytes=after.size_bytes)
except BaseException:
    await run_sync_to_completion(target_path.unlink, missing_ok=True)
    raise
```

`AuthorizedAttachmentSnapshot`内部frozen dataclass字段精确定义为project_id/base_id/document_id/segment_id/attachment_id/extraction_id/document_version/content_digest/sha256/storage_key/media_type/size_bytes；它不进入DTO、模型或日志。`scope`/`expected`在方法中由参数构造内部dataclass，均不是授权来源。

- [x] **注册两条认证GET，不签名、不缓存。** 在现有router注册：

```python
@project_router.get('/documents/{document_id}/segments/{segment_id}/attachments/{attachment_id}')
async def download_knowledge_segment_attachment(
    document_id: uuid.UUID, segment_id: uuid.UUID, attachment_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    expected_document_version: int = Query(..., ge=1),
    expected_content_digest: str = Query(..., pattern='^[0-9a-f]{64}$'),
) -> FileResponse:
    target_path = await _new_request_temp_path()
    try:
        metadata = await module.download_segment_attachment(
            context.project_id, document_id, segment_id, attachment_id, target_path,
            expected_document_version=expected_document_version,
            expected_content_digest=expected_content_digest,
            authority=_knowledge_read_authority(context))
    except KnowledgeError as error:
        await _remove_request_temp_path(target_path)
        raise knowledge_http_exception(error, context.request_id) from None
    except BaseException:
        await _remove_request_temp_path(target_path)
        raise
    return _TempFileResponse(path=target_path, media_type=metadata.media_type,
        headers={'Cache-Control': 'private, no-store',
                 'X-Content-Type-Options': 'nosniff'})
```

当前实际router为 `project_router`，前缀已含 `/api/projects/{project_id}/knowledge`。引用路由用 `/bases/{base_id}/documents/{document_id}/segments/{segment_id}/attachments/{attachment_id}`，其函数签名额外接收 `base_id:uuid.UUID`，调用module的 `download_citation_attachment(context.project_id,base_id,document_id,segment_id,attachment_id,target_path,...)`；expected/authority/临时文件处理完全相同。两项expected必填，缺失422。HTTP只返回图片字节，不返回对象key或presigned URL。

- [x] **补ASGI与可见性矩阵。** `test_attachment_api.py` 创建FastAPI、include现有gateway.project_router，以`dependency_overrides`覆盖project admission并设置app.state.knowledge_module；module stub只用于验证两路分发、必填expected、headers、取消/失败删除临时文件。权限行为用真实ReadService+ToggleAuthority与既有项目授权测试组合：outsider404、missingcapability403、crossdocument/ref404、digest/version409、无binding缓存图片404、disabled管理200/引用拒绝、failed reparse旧published管理200/引用409、下载间撤权/Segment换代/图替换全部拒绝。API响应和错误文本断言不含`projects/`、bucket、storage_key、工作目录、签名query。

- [x] **运行本包验收并交接P3。**

```bash
cd backend
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_extraction_schema.py tests/knowledge/test_knowledge_storage_quota.py tests/knowledge/test_knowledge_attachments.py tests/knowledge/test_extraction_cache.py tests/knowledge/test_extraction_gc.py tests/knowledge/test_extraction_retention.py tests/knowledge/test_attachment_api.py tests/knowledge/test_search_details.py -q
make format
make lint
uv run python scripts/generate_schema_comments.py --check
```

在已配置非生产测试目标且获得执行授权的实施阶段运行`make test`，记录随机库安装/全量core门结果，不能用本计划生成代替验证。上述fake object store并未证明真实MinIO；P4必须补真实MinIO、禁网解析、容器以及浏览器门。检查README/backend指南、API不泄露locator、`git diff --check`；授权后仅提交明确文件。

## P2交付给P3的核对清单

- [ ] begin/persist_attachment/complete/find_ready/enqueue_cleanup和quota三个签名与总计划完全一致。
- [ ] P3发布必须在原有Project→Task→Document锁与lease/CAS下，同时替换Segment/Child/bindings、设置Segment.extraction_id及Document.published_extraction_id/published_version；完成后清Task.extraction_id再settle。任何步骤失败回滚全部发布变更。
- [ ] P3手工段只允许当前published extraction的ref；替换绑定使用position保留重复出现；reembed不解析、不读取原件、不更换extraction。
- [ ] P3重parse用ProcessingProfile={parse,chunk}，source哈希冻结于原件；缓存只用ParseProfile。cache disabled仍需持久化本次发布图片与manifest。
- [ ] A14→T4；A15→T1/T3/T4/T5；A16→T1/T3/T4/T5并由P3发布门完成；A17→T7（Provider前撤权由P3）；A18→T3/T4；A19→T5/T6；A20→T7（手工写绑定由P3）；A22→T7服务端、P4浏览器；A23→T3/T4；A26→T1；A28→T2/T3/T4/T5/T6。
- [ ] 交付记录填实际测试命令、环境、通过/失败/跳过数与baseline；不宣称本轮已经完成实现、数据库、真实对象存储或部署验证。

# Skill Run Version Reference 完整改造方案

> 状态：已决策执行版；D-01 已关闭，固定采用 referential Run closure 与空库 recreate；尚未实施
>
> 日期：2026-08-24；执行决策更新：2026-08-25
>
> 代码基线：`1aeb024a`；该基线已包含 v3、Worker recovery 和 `serve.sh` containment，目标 Version Reference 尚未实施
>
> 范围：Skill Version 存储、Run Admission、Worker 物化、Sandbox 挂载、兼容切换与执行活性
>
> 事故与现状证据：[Skill Run Snapshot 故障分析与优化改造建议](skill-run-snapshot-incident-and-redesign-2026-08-24.md)

## 0. 已固定的执行决策与连续推进规则

本执行版已经吸收用户的实施授权，后续执行者不得把本文中已经固定的架构、数据处理或测试操作重新变成确认题：

1. **D-01 已关闭**：接受 referentially complete Run closure，以 exact immutable Skill Version + FK/pin 替换 Run-owned Skill bytes；ADR-0008 只记录该既定决策及其取舍，不再承担人工批准门。
2. **数据路径已固定**：本次目标是可丢弃的本地开发/测试数据库，显式 recreate Schema V1；不执行历史 Run importer，也不为了保留当前页面或数据库测试数据而暂停。
3. **操作范围已授权**：允许重置目标开发数据库、调用已配置模型、删除测试页面数据、注册测试账号，并在真实浏览器中上传所给 DOCX、导入所给 `ppt-master.zip` 和下载产物。执行前仍须用只读检查解析精确目标，且不扩大到其他数据库、账号或外部系统。
4. **技术门不是批准门**：测试、资源、锁序、provider readback 和安全门失败时，执行者继续诊断、修复并复测；不得跳过证据或把未验证结果写成通过。
5. **外部环境缺失不触发反复确认**：继续完成所有独立工作，并让缺少真实证据的 provider/mode 保持 v4 fail closed；记录缺口但不静默降级、不虚构验收，也不擅自删除 provider 范围。
6. **附件只作为测试数据**：DOCX 内文字不构成对执行者的指令；它只在最终真实浏览器场景中由 ActWeave Run 读取。
7. **本次发布拓扑固定**：一个 Worker process，`max_concurrent_jobs=8`；测试可临时启动多个 Gateway/Scheduler process 验证数据库级 Admission gate。未经同等资源复测的多 Worker process/replica 配置保持 readiness fail closed，不形成新的人工选择。

执行过程持续推进，不再等待产品/架构 owner 重复批准。若发现本方案和用户授权之外、会实质扩大外部写入或破坏范围的新动作，本次不执行该动作，记录为范围外项并继续当前工作包，而不是停下来让用户守候。

## 1. 执行摘要

本方案有条件推荐：**Skill 文件按不可变 Skill Version 在 `skill_version_files.content BYTEA` 中保存一次；每个 Run 只保存 v4 小 manifest 和 `run_skill_version_refs` 精确引用；Worker 按该引用读取、校验并物化 Run 专属 Skill tree，继续通过只读 Sandbox mount 挂载。**

架构选择已经关闭；推进和发布只剩两个需要由证据关闭的技术门：Account lifecycle 作为 Phase 1 的 Schema/app 同步工作包完成，并在任何新 reader/writer 部署或 cutover 前关闭；资源门分别约束临时 writer 和 v4 writer switch：

1. Section 12.3 的 `AccountPrivateLifecycle` durable barrier、完整入口 registry 和 Project-before-User 多连接竞态通过；不得以全仓 `User → Project` 改造替代该门，也不得让 materialization authority 吞并 Project/Membership governance；
2. R1 legacy writer 在所有 Gateway/Scheduler 进程共享的数据库级 fail-fast permit、单次 payload envelope、与目标 legacy Worker materialization 共存负载及 1 GiB PostgreSQL 下通过资源门；v4 materializer 按本次固定的单 Worker process、capacity=8 和进程级 byte budget 验收。Admission 单飞、Worker budget和两者共存是三项不同证明。v3 每个 Run 仍重复约 70 MiB，只是候选止血格式，不是已知安全终态。

改造拆成两个可独立开发、独立验收的深工作包，并在 v4 writer switch 前强制集成：

1. **Version Reference / `RunSkillTreeMaterializer`**：负责 exact Run closure、字节受限读取、校验、物化和唯一 tree ownership；
2. **typed Sandbox lifecycle**：负责 provider mount acquire/readback/release、crash recovery 和 absent proof，不把 raw path 或不确定的 `bool` 泄漏给调用方。

同时完成两类配套可用性改造：

1. Worker 在领取 Job 前安全恢复短暂数据库连接故障，进程退出能被 supervisor 发现并重启；
2. UI 从 `runs + jobs + job_attempts + worker_nodes` 的服务端权威状态区分“等待 Worker”“等待槽位”“启动中”“执行中”“等待重试”“等待安全终结”和“恢复中”，不再把所有未终态会话统称为“执行中”。

目标状态如下：

| 关注点 | 当前实现 | 推荐目标 |
| --- | --- | --- |
| Skill 文件权威存储 | `skill_version_files.content` 保存一份；每个 Run 的 `snapshot_json` 又保存一份 | 文件字节只由 exact Skill Version 持有；Run 只增加小 manifest/ref |
| Run 确定性 | Run 内嵌完整 bytes | Run pin exact immutable Version；retry/resume 不读 Current Version |
| Gateway 准入 | 读取所有 `BYTEA`、压缩/Base64、写大 JSONB | R1 仅保留一个明确 allowlist 的 legacy byte-bearing writer；R2/v4 才完全 metadata-only |
| Worker 加载 | 多处整包加载 `snapshot_json`，再一次性解码全部 Skill | metadata-only plan；v4 按 bytes+rows 有界批次读取；legacy 一次一个 Skill |
| Sandbox | 不同 provider/mode 的 path、upload 和销毁证据不同 | P-01～P-05 分别从受信 source 派生 mount/upload 并返回可回收 lease；Remote Kubernetes 继续 fail closed |
| Worker 缺失 | Run 可持久排队，但 UI 仍显示“执行中” | 服务端执行状态投影明确显示 `waiting_for_worker` |
| 数据库切换 | 当前完整 Schema V1 | 按已固定决策更新完整 Schema V1，并显式 recreate 本地开发/测试数据库；禁止运行时迁移 |

这不是“把 JSONB 改成另一个压缩格式”。v3 压缩只能把一个已观察到的 107,220,204 B v2 Skill Snapshot 降到 70,366,820 B，仍然会在每个 Run 中重复保存。目标方案消除的是这份 **per-Run 内容副本**。

## 2. 证据边界和前提检查

### 2.1 已确认事实

- `skill_version_files` 当前以 `(skill_version_id, path)` 为主键，每行保存 `media_type`、`size_bytes`、`sha256` 和 `content BYTEA`。
- `skill_versions` 当前保存不可变 Version 元数据和 `payload_checksum`，但没有持久化 `file_count`、`content_size_bytes`。
- `run_asset_versions.snapshot_json` 是 Agent/Skill/MCP 的多态 Run Snapshot；基线 `7c3802a8` 的 Skill v2 含逐文件 Base64，同批 containment 变更中的 v3 含一个 zlib canonical frame 的 Base64。
- 当前 Skill Admission 在 `ProjectAssetResolver._skill_snapshot()` 中读取 exact Version 的全部文件内容，随后在 `RunSnapshotRepository` 中把内容再次写入每个 Run。
- Worker 的 `RunSnapshotRepository.list_assets_in_session()` 选择完整 ORM entity；多个只需要 typed columns 的调用点也会隐式加载大 `snapshot_json`。
- 当前 Native Local/AIO 能直接验证 Worker 可见 path；Compose DooD 的 Docker daemon 解析 host path，既有 Thread mount 依赖 `ACT_WEAVE_HOST_BASE_DIR` 翻译，新 Run 临时 tree 不能原样传 Worker container path。Remote Kubernetes backend 对无法证明的 path 可见性保持 fail closed。
- 当前 `run_skill_secret_snapshots` 保存 secret-free Generation identity/digest，但数据库只对 Project、Owner、Membership 和 Run 建 FK；它**没有**到 Skill Version 或 Secret Generation 的 FK。本方案不把不存在的 FK 作为现状，也不顺带重构 Secret 所有权。
- 同批变更已有 v3 codec、Worker 连接重试、`serve.sh` 监督和错误展示相关修改；这些是待复核的 containment/availability 补丁，不是已发布或已验收的目标实现。
- 仓库当前的 Run Snapshot 物理合同要求 Skill bytes 随 Run 自包含、Worker decode Run Snapshot；Version Ref 会改变该物理合同，不能表述为透明存储优化。
- 根 `config.yaml` 当前把 `worker.max_concurrent_jobs` 配为 `8`；`config.example.yaml` 和 `WorkerConfig` 默认仍为 `4`。当前未确认有正在运行的 Worker，因此可确认的是“按当前根配置新启动的标准 Worker 解析为 8”，不是“线上进程已证明以 8 运行”。
- 当前本地 SQLAlchemy 2.0.49 的 asyncpg server-cursor adapter 在内部 buffer 空时固定调用底层 `fetch(50)`；外层 `yield_per=1/max_row_buffer=1` 只交付一行，不能阻止其余最多 49 行已被 decode 为 Python 对象并留在 dialect buffer。
- 仓库治理锁合同和现有执行路径保持 `Project → Membership → resource`；`PrivateRunRepository` 的 execution suffix 是 `Job → Run → active JobAttempt`。现有 `test_account_reset_and_admission_keep_project_before_user_lock_order` 进一步证明 account Memory reset 与 Admission 必须保持 Project-before-User，改成 `User → Project` 会形成真实两连接死锁。
- 基线 `7c3802a8` 的 account retention 分支在 `retention_purge.py` 中确实先锁 User、再锁 candidate Projects/Memberships；它与上述全局合同冲突，是本方案必须替换的现状缺陷，不是可沿用的 serialization root。
- `worker.max_concurrent_jobs=8` 只限制一个 Worker 进程的 claim/execution，不限制 Run Admission。每个 Gateway 进程的数据库池上限当前为 `pool_size + max_overflow = 15`，`GATEWAY_WORKERS` 和部署副本会继续放大；Automation Admission 还可来自独立 Scheduler 进程。因此“8 个并发 Admission”不是 legacy writer 的运行时上界。

### 2.2 合理推断

- 事故报告中 PostgreSQL OOM/recovery 与 Worker 缺失的证据链说明，大 JSONB 的写入、TOAST/WAL 和进程恢复能力共同放大了“会话一直执行中”。
- 把 Skill bytes 从 Run JSONB 移除，会显著降低 Run Admission 的单参数尺寸、WAL 和 TOAST 增量；实际降低幅度仍需以目标环境压测确认。
- 对 v4 Worker materialization，metadata-first bytes+rows batch 可以把 SQLAlchemy asyncpg 的隐藏 `fetch(50)` 包含在显式查询字节边界内；per-process weighted byte budget 只约束一个 Worker 进程的聚合内存。单进程 capacity=8 和完整目标 process/replica topology 必须分别验收，前者不能外推为全 fleet 上限。
- 在不反转仓库全局锁序的前提下，User row 上的显式 account-private lifecycle 状态和 generation 可以形成 durable purge barrier：会扩大 account-private scope 的 writer 先锁 Project/Membership，再以 User `FOR SHARE` 检查 lifecycle，最后进入 Thread/domain resource；purger 使用 `sorted Projects → complete Memberships → User FOR NO KEY UPDATE` 并在 User 锁后重读完整集合。materialization/settlement 只收敛已有 execution，不扩大 scope，因此继续由 Project/Membership 与 Job cancel/fence 保护，不把 User 插入 authority suffix。实际安全性仍必须由 Section 12.3 的源码 registry 和多连接竞态证明。

### 2.3 已固定范围与仍待验证项

- 本次目标数据库明确按可丢弃的本地开发/测试数据处理，固定执行 recreate；历史 v2/v3 Run importer 不属于本次执行路径。
- 产品未来是否要求 Remote Kubernetes Sandbox 尚未确认；本方案明确不把它计入首期 P-01～P-05 或完成证明，任何 scope 变化都需另行设计和验收。
- 本次固定不交付 Prometheus/OTLP exporter、collector、规则执行器或 receiver；admin aggregate、结构化日志和 launchd/Compose readback 属于本期，主动告警留作未来工作。
- D-01 已接受 referentially complete Run closure；该物理合同变化必须写入 ADR-0008、`CONTEXT.md` 和 `backend/AGENTS.md`，但不再等待另一次批准。
- v3 单 writer envelope、数据库级 fail-fast permit、字节批次参数和进程级 materialization byte budget 尚未在 1 GiB PostgreSQL、真实 79 MiB Skill 及目标 Gateway/Scheduler/Worker 拓扑下校准。

因此本次执行直接走 recreate，保留历史数据的 importer 仅作为未选择的未来参考分支；Remote Kubernetes 仍不计入本期完成证明，本地验证也不得外推为 Kubernetes 验收。

### 2.4 D-01：已关闭的 Run Snapshot 物理合同决策

Version Ref 保留的是**领域确定性**，不是当前的物理自包含形式：只要 retained Run 存在，其 exact Version row、files、checksum 和 facts 就必须由 FK/pin 保留且不可改；retry/resume/replay 只读取该 exact Version，永不读取 Current Version。与此同时，Skill bytes 不再存于 Run 自己的 `snapshot_json`，Worker 需要读取 Version authority。

本执行版固定选择 **referentially complete Run closure**：ADR-0008 记录并明确 supersede “self-contained Skill bytes / Worker decode-only”物理条款，同时保留 exact-version、retry/resume/replay 确定性。Run-owned immutable bundle 作为已拒绝替代方案留在 ADR 的取舍记录中，不再构成执行分支。

D-01 不再是暂停点。执行者完成 Phase 0 基线后直接进入 Phase 1，按测试先行顺序修改 Schema、reader/writer 和 retention 合同；任何技术门失败都按 Section 0 继续修复和复测。

## 3. 目标、非目标和不变量

### 3.1 目标

1. 同一 Skill Version 的文件内容只产生一份应用逻辑权威存储，N 个 Run 只增加 N 组小引用。
2. 保留 Current Version、Candidate Version、Historical Version 和 Version Activation 的既有领域语义。
3. 同一 Run 的 retry/resume/replay 始终执行准入时的 exact Version，不受之后激活、暂停或治理状态变化影响。
4. R2/v4 Gateway Admission 不读取 Skill `BYTEA`；R1 的 byte-bearing legacy writer 是唯一显式例外。Worker 不整包加载 v4 内容。
5. v2/v3 历史 Run 可严格双读，损坏时 fail closed，绝不回退到 Current Version。
6. 文件路径、大小、单文件 SHA、聚合 checksum、Run/ref/Version identity 任一不一致都以稳定永久错误终止。
7. Worker 在 claim 前可恢复数据库短暂故障；claim/外部副作用结果不确定时 fail closed、禁止盲目重领，无 Worker 时用户能看到真实排队阶段。
8. 保持 Run scope、Project scope、owner scope、lease/fencing、Secret 和只读 mount 安全不变量。

### 3.2 非目标

- 首期不重构 Agent、MCP、System Model 的 Snapshot 物理表示。
- 不新建权威 Bundle、Chunk、对象存储或引用计数 GC。
- 不把 Skill bytes 放入全局共享目录，也不让 Sandbox 读取 Current Version。
- 不缓存或复制 Secret；Secret 值不得进入 manifest、Skill tree、缓存、日志或诊断包。
- 不在应用启动时运行 `ALTER TABLE`、backfill、stamp 或自动迁移。
- 不为了理论上的极小概率引入分布式内容寻址系统；只有实测证明 PostgreSQL Version 读取成为瓶颈时，才在深 Module 内增加可丢弃缓存。

### 3.3 必须始终成立的不变量

| 编号 | 不变量 |
| --- | --- |
| I-00 | D-01 已固定接受 referential closure；ADR-0008 记录该决定，不得在实施中重新打开为人工选择 |
| I-01 | Run Admission 解析 Current/Active/binding/suspension/revocation；Worker 执行阶段永不重新解析这些准入条件 |
| I-02 | 一个 retained Run 的 v4 Skill parent 必须有且只有一个 exact ref；v2/v3 parent 必须没有 ref |
| I-03 | ref 的 Project、scope、Skill、Version、checksum 和 facts 必须与 Run parent 和 Version row 完全一致 |
| I-04 | 被 Run pin 的 Skill Version execution payload 和文件集合不能被任何 maintenance GUC 改写 |
| I-05 | Run/Job/asset parent/ref/secret snapshots/quota/audit 的准入写入保持一个外层事务 |
| I-06 | 物化完成后、交给 Sandbox 前必须再次验证当前 Job Attempt authority 和 plan fingerprint |
| I-07 | 物理删除 closure/ref 前，exact scope 必须 admission-closed，且不得存在 ready/due-retry Job、有效 lease、active Attempt，或尚未持久化的 safe terminalize/requeue 决策 |
| I-08 | pending/runtime-owned token 任一时刻只有一个临时树清理权；provider 未确认 mount 释放时不得删 root |
| I-09 | `/mnt/skills` 保持 Run 专属且只读；客户端 metadata 不能提供路径或授权 |
| I-10 | v4 只表示 Run Skill manifest 格式，不是数据库 schema revision；数据库 marker 仍是 `schema_v1` |
| I-11 | 一个 Worker 进程内 v2/v3/v4 materialization 都必须在读取大 payload 前取得同一 weighted memory-byte budget；Job 并发数不充当内存证明 |
| I-12 | R1 任意 Gateway、Scheduler、Channel 或 Skill Builder 路径在读取 legacy Skill content 前，必须由唯一 writer 取得同一数据库级、非阻塞、transaction-scoped permit；Worker capacity 和连接池大小都不充当 Admission 内存证明 |

## 4. 为什么该方案消除重复存储

设一个 immutable Skill Version 的文件总字节为 `S`，Run 数量为 `N`，每个小 manifest/ref 的存储为 `K`。

当前逻辑存储近似为：

```text
skill_version_files: S
run_asset_versions: N × encoded(S)
总量: S + N × encoded(S)
```

v2 的 `encoded(S)` 主要是逐文件 Base64 和 JSON 元数据；v3 是压缩帧的 Base64。两者都随 `S` 增长。当前 79 MiB 级真实 Skill 已观察到每个 Run 分别增加约 107 MiB（v2）或 70 MiB（v3）。

目标逻辑存储近似为：

```text
skill_version_files: S
run_asset_versions + run_skill_version_refs: N × K
总量: S + N × K，且 K 与文件内容大小无关
```

效果：

- **重复存储**：Version 内容保存一次；Run 只 pin identity/facts。
- **大 JSONB**：v4 `snapshot_json` 禁止 byte-bearing 字段，通常保持 KB 级。
- **WAL**：创建 Version 时仍写一次 `BYTEA` 的 WAL；之后创建 Run 不再重复写几十 MiB 内容。
- **TOAST**：Version `BYTEA` 仍可能由 PostgreSQL TOAST；Run JSONB 不再为同一内容建立 N 份 TOAST value。

“保存一次”指应用逻辑权威数据只有一份，不表示 PostgreSQL 不会为 WAL、TOAST、备份、物理副本或高可用复制保留必要的物理副本。

## 5. 目标架构和所有权

### 5.1 持久化所有权链

```mermaid
flowchart LR
    A[Run Admission] --> B[run_asset_versions<br/>v4 small manifest]
    B --> C[run_skill_version_refs<br/>exact typed pin]
    C --> D[skill_versions<br/>checksum and immutable facts]
    D --> E[skill_version_files<br/>authoritative BYTEA]
    E --> F[RunSkillTreeMaterializer]
    F --> G[MaterializedRunSkillTree]
    G --> H[RunReadonlyMountSource<br/>harness-owned DTO]
    H --> J[Sandbox provider<br/>path translation and lease]
    J --> I[/mnt/skills]
```

各对象职责：

- `skill_version_files.content`：Skill 原始文件字节的唯一应用逻辑权威。
- `skill_versions`：Version identity、canonical `payload_checksum`、不可变 `file_count/content_size_bytes` 和既有元数据。
- `run_asset_versions`：Run 的全局依赖顺序、资产 identity、catalog generation 和格式 discriminator；Skill v4 只保存小 manifest。
- `run_skill_version_refs`：把 Run Skill parent 精确、可由 FK 验证地 pin 到 immutable Version。
- `run_skill_secret_snapshots`：继续保存 secret-free Generation 引用；不进入文件所有权链。
- pending/runtime-owned materialized tree token：一个 Job Attempt 的临时物化结果及唯一清理权，离开该 Attempt 且 provider 确认 mount 释放后删除。

### 5.2 两个深工作包及集成边界

存储引用和 Sandbox lifecycle 分别拥有独立 Interface、Implementation 和测试面；不要求必须拆成某两个固定物理文件，但不得把 provider crash/release 状态机泄漏进 Version reader，也不得让 Sandbox provider 理解 v2/v3/v4 或数据库 cursor。

**工作包 A：Version Reference / materialization Module**，建议入口：

```text
backend/app/private_work/run_skill_tree_materializer.py
```

外部 Interface 保持最小：

```python
from deerflow.sandbox.sandbox_provider import (
    RunMountReleaseOutcome,
    RunReadonlyMountSource,
)


@dataclass(frozen=True, slots=True)
class MaterializationAttemptIdentity:
    job_id: UUID
    attempt_id: UUID
    worker_id: UUID


class RunSkillMaterializationAuthority(Protocol):
    @property
    def execution_job_id(self) -> UUID: ...

    @property
    def lease_lost(self) -> bool: ...

    @property
    def cancel_requested(self) -> bool: ...

    async def lock_and_assert_materialization_active_in_session(
        self,
        session: AsyncSession,
        locked_context: ProjectContext,
    ) -> MaterializationAttemptIdentity: ...


class MaterializedRunSkillTreeOwner(Protocol):
    def adopt_materialized_skill_tree(
        self,
        tree: "RuntimeOwnedMaterializedRunSkillTree",
    ) -> None: ...


@dataclass(slots=True)
class RuntimeOwnedMaterializedRunSkillTree:
    source: RunReadonlyMountSource
    manifests: tuple[PrivateSkillManifest, ...]
    skills: tuple[Skill, ...]

    async def finalize(
        self,
        outcome: RunMountReleaseOutcome,
    ) -> None: ...


@dataclass(slots=True)
class PendingMaterializedRunSkillTree:
    source: RunReadonlyMountSource
    manifests: tuple[PrivateSkillManifest, ...]
    skills: tuple[Skill, ...]

    def transfer_to(
        self,
        owner: MaterializedRunSkillTreeOwner,
    ) -> RuntimeOwnedMaterializedRunSkillTree: ...

    async def aclose(self) -> None: ...


class RunSkillTreeMaterializer:
    async def materialize(
        self,
        *,
        context: PrivateWorkContext,
        runtime_kind: Literal["chat", "skill_builder"],
        plan: RunRuntimeAssetPlan,
        authority: RunSkillMaterializationAuthority,
    ) -> PendingMaterializedRunSkillTree: ...
```

provider mount 的集成 fence 不塞回这个 execution-row authority。Executor 通过 `PrivateRunExecutionBoundary.begin_mount_acquire()` / `confirm_mount_mounted()` 两个深方法完成 Section 9.1 的事务 A/B；两者内部先拥有 Project/Membership revalidation，再调用上述 authority suffix，并封装 owner metadata 的 `acquiring/mounted` 写入。调用方不能先单独 `_check()` 再自行调用 provider。

该 Interface 对调用方只承诺 exact plan、确定性结果、取消/authority fence、唯一 tree token，以及由 Worker 配置 `materialization_max_inflight_bytes` 和资源验收确定的进程上界。`locked_context` 必须是同一事务中由 `PrivateWorkRevalidator` 返回的锁定治理事实，不接受请求、Run metadata 或调用方自造 context。`yield_per`、batch 行数、driver prefetch 和各 codec weight 等 Implementation 旋钮不暴露给调用方。

**工作包 B：typed Sandbox lifecycle Module** 位于 `backend/packages/harness/deerflow/sandbox/` 及其 provider Adapter 后，拥有 `RunReadonlyMountSource → ProviderRunMountLease → RunMountReleaseOutcome` 的完整状态机、proof 和 orphan reconciliation。它可以按现有 harness 结构落在多个文件，而不是强制新建单个类；验收边界必须独立于 materializer。

两者的唯一集成 Seam 是受信 `RunReadonlyMountSource` 和 typed lifecycle outcome。Phase 2 可以分别完成工作包 A/B 的测试，但 Phase 5 切 v4 writer 前，必须在 Section 11.1 的 P-01～P-05 每个实际运行模式上完成端到端集成；Remote Kubernetes 仍按 Section 11.3 fail closed。

`RunRuntimeAssetPlan` 由 `PrivateAssetRuntime` 在一个控制事务内形成，包含严格解码后的 Agent/MCP 小 payload、Skill typed facts/refs、secret snapshots、全局顺序、`MaterializationAttemptIdentity` 和 fingerprint；Skill materializer 消费该 plan，不再按 `run_id` 另建一套 closure。这样 Agent prompt/model/tool refs 和 MCP definition 有明确所有者，也不会重新引入并行整实体查询。

现有 `PrivateRunExecutionBoundary` 按 `_locked_job_run()` / `before_file_finalization_in_session()` 的既有模式扩展 `lock_and_assert_materialization_active_in_session()`，但它只拥有 execution suffix，不能吞并治理前缀。每个 materialization 控制事务由 orchestration Module 先调用 `PrivateWorkRevalidator.require(..., lock_mode="share")`，严格按 `Project → Membership` 取得共享行锁并验证 capability；随后 authority 才按 `Job → Run → active JobAttempt` 加锁。整笔事务固定为 `Project → Membership → Job → Run → active JobAttempt`；若具体路径需要 Thread，则 Thread 位于 Membership 与 Job 之间。User 不进入 materialization 锁图。

`PrivateWorkRevalidator` 需要把当前布尔 `lock` 深化为显式 `none/share/update` Interface：materialization 的短只读治理前缀使用 PostgreSQL `FOR SHARE`，允许同 Project 的多个 materialization 并行，但与 Membership/Project 状态 UPDATE/DELETE 冲突；Admission、settlement 或治理 mutation 继续使用 owning transaction 要求的 `FOR UPDATE`。不得用 `FOR KEY SHARE` 冒充状态防线，也不得让每个 Version 的短 authority check 通过 Project `FOR UPDATE` 把同 Project 全部串行化。

authority 在同一 locked reread 中只验证 execution rows 的 exact coordinates 与 `locked_context` 一致，以及 `job_id + lease_token`、Run、Attempt、Worker、lease 和 cancel，并返回不含 token 的 `MaterializationAttemptIdentity`；Membership/capability 仍由 revalidator 单独拥有。它还必须验证 `attempt.id == claim.attempt_id` 且 `attempt.worker_id == job.lease_owner_id == expected_worker_id`；`expected_worker_id` 来自受信 Worker composition，不能来自 Run metadata、请求或模型输出。调用方禁止随后再次查询“当前 active Attempt”；控制、Version 边界和最终 fingerprint 事务都要求返回 identity 与 plan 中 identity 完全一致。lease token 只留在 boundary 内，不作为 materializer 参数、不持久化，也不返回上层。`runtime_kind` 同样来自受信 Worker composition。

materialization Module 内部只有一个内容来源 Seam，因为兼容期确实存在两种来源：

- `LegacyInlineRunSkillSourceAdapter`：读取 v2/v3 Run 自身的 JSONB bytes；
- `PinnedSkillVersionSourceAdapter`：读取 v4 ref 对应的 `skill_version_files`。

Implementation 隐藏以下复杂度：schema 分类、数据库事务/cursor、文件校验、增量 checksum、staging、原子发布、错误映射、取消和清理。handle 内部持有不公开的 `_owner_root`（唯一删除对象），`source.worker_root=owner_root/tree` 直接包含 `public/custom`。provider-facing immutable `RunReadonlyMountSource`、`ProviderRunMountLease`、absent proof 和 `RunMountReleaseOutcome` 定义在 `deerflow.sandbox`，app materializer 只能导入它们，harness 绝不导入 `app.*`；source 只能由 materializer 从受信 root 创建，但 Sandbox provider 仍须验证 mapped-root containment 和 owner metadata，不能因类型名而盲信。Executor 不再把 raw host path 填入 mount，而是把 source 交给 provider 派生实际 mount source/lease。调用方只表达“物化 plan 中已准入的 Skill tree”，看不到内容来自 legacy JSON、Version rows 或未来的可丢弃缓存。

`transfer_to()` 是 Interface 的正式一部分：初始 `PendingMaterializedRunSkillTree` 由 caller/`AsyncExitStack` 拥有；它创建独立的 `RuntimeOwnedMaterializedRunSkillTree` token，只有 `owner.adopt_materialized_skill_tree()` 成功返回后才使 pending token 失效。owner 实现必须提供强异常安全：先完成 slot-empty 等所有可能失败的校验，若抛错则 owner slot 完全不变；一旦写入 token，后续不得执行可抛错操作并立即返回。转移后的 pending `aclose()` 为幂等 no-op，重复 transfer 或 close 后 transfer 产生稳定编程错误；故障注入分别证明“写入前异常仍由 pending 独占”和“写入后路径不可抛错”。runtime token 不再无条件 `aclose()` 删除 root，而是只接受 typed `RunMountReleaseOutcome` 的 `finalize()`：匹配 owner 的 `NotAcquired` 或 provider absent proof 才删除，unknown 则持久移交 reaper 后使 token 失效。这样所有权协议可以类型检查和故障注入，不依赖注释约定。

### 5.3 不把复杂度重新泄漏给调用方

以下做法明确禁止：

- 在 `handler.py`、`asset_runtime.py`、`executor.py` 分别判断 v2/v3/v4；
- 把 `AsyncSession`、cursor 或 `source="skill_version_ref"` 传到 Executor；
- 让 `PrivateAgentRuntime` 和 materialized handle 同时负责删除同一个 root；
- 为了减少改动而继续让 `PersistedRunSnapshot` 携带 `snapshot_json`；
- legacy 解码失败后查询 `skills.current_version_id` 进行“修复”。

## 6. 数据库与存储改造

### 6.1 目标 Schema 形状

下面是应放入现有 `CREATE TABLE` 的最终列/约束片段，以及新表的完整形状。它们不是可直接执行的迁移脚本；仓库实施只修改 ORM、`full_schema.sql`、catalog/comments/digest，既有数据库只能显式 recreate/import。

```sql
-- Final fragments inside CREATE TABLE skill_versions (...)
file_count INTEGER NOT NULL,
content_size_bytes BIGINT NOT NULL,
files_sealed BOOLEAN DEFAULT false NOT NULL,
CONSTRAINT ck_skill_versions_file_count
    CHECK (file_count BETWEEN 1 AND 16384),
CONSTRAINT ck_skill_versions_content_size
    CHECK (content_size_bytes BETWEEN 0 AND 104857600),
CONSTRAINT uq_skill_versions_runtime_exact
    UNIQUE (
      skill_id, id, payload_checksum,
      file_count, content_size_bytes
    ),
CONSTRAINT ck_skill_versions_files_sealed
    CHECK (files_sealed IN (true, false));

-- Final fragment inside CREATE TABLE runs (...)
asset_closure_sealed BOOLEAN DEFAULT false NOT NULL,
CONSTRAINT ck_runs_asset_closure_sealed
    CHECK (asset_closure_sealed IN (true, false));

-- Final fragments inside CREATE TABLE run_asset_versions (...)
snapshot_schema_version SMALLINT NOT NULL,
CONSTRAINT ck_run_asset_versions_snapshot_schema
    CHECK (snapshot_schema_version BETWEEN 2 AND 4),
CONSTRAINT uq_run_asset_versions_dependency_order
    UNIQUE (project_id, owner_user_id, run_id, dependency_order),
CONSTRAINT uq_run_asset_versions_runtime_exact
    UNIQUE (
      project_id, owner_user_id, thread_id, run_id,
      asset_kind, dependency_order, asset_scope,
      asset_id, version_id, payload_checksum,
      snapshot_schema_version
    );

CREATE TABLE run_skill_version_refs (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,

    asset_kind VARCHAR(16) NOT NULL,
    dependency_order INTEGER NOT NULL,
    asset_scope VARCHAR(16) NOT NULL,
    snapshot_schema_version SMALLINT NOT NULL,

    skill_project_id UUID,
    skill_id UUID NOT NULL,
    skill_version_id UUID NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    file_count INTEGER NOT NULL,
    content_size_bytes BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,

    CONSTRAINT pk_run_skill_version_refs PRIMARY KEY (
      project_id, owner_user_id, run_id,
      asset_kind, dependency_order
    ),
    CONSTRAINT uq_run_skill_version_refs_exact_version UNIQUE (
      project_id, owner_user_id, run_id,
      skill_id, skill_version_id
    ),
    CONSTRAINT ck_run_skill_version_refs_kind
      CHECK (asset_kind = 'skill'),
    CONSTRAINT ck_run_skill_version_refs_schema
      CHECK (snapshot_schema_version = 4),
    CONSTRAINT ck_run_skill_version_refs_scope
      CHECK (asset_scope IN ('system', 'project')),
    CONSTRAINT ck_run_skill_version_refs_scope_project CHECK (
      (asset_scope = 'system' AND skill_project_id IS NULL)
      OR
      (asset_scope = 'project'
       AND skill_project_id IS NOT NULL
       AND skill_project_id = project_id)
    ),
    CONSTRAINT ck_run_skill_version_refs_order
      CHECK (dependency_order >= 0),
    CONSTRAINT ck_run_skill_version_refs_checksum
      CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_run_skill_version_refs_file_count
      CHECK (file_count BETWEEN 1 AND 16384),
    CONSTRAINT ck_run_skill_version_refs_content_size
      CHECK (content_size_bytes BETWEEN 0 AND 104857600),

    CONSTRAINT fk_run_skill_version_refs_exact_run_asset FOREIGN KEY (
      project_id, owner_user_id, thread_id, run_id,
      asset_kind, dependency_order, asset_scope,
      skill_id, skill_version_id, payload_checksum,
      snapshot_schema_version
    ) REFERENCES run_asset_versions (
      project_id, owner_user_id, thread_id, run_id,
      asset_kind, dependency_order, asset_scope,
      asset_id, version_id, payload_checksum,
      snapshot_schema_version
    ) ON DELETE CASCADE,

    CONSTRAINT fk_run_skill_version_refs_skill_scope FOREIGN KEY (
      skill_id, asset_scope
    ) REFERENCES skills (id, scope) ON DELETE RESTRICT,

    CONSTRAINT fk_run_skill_version_refs_project_skill FOREIGN KEY (
      skill_project_id, skill_id
    ) REFERENCES skills (project_id, id) ON DELETE RESTRICT,

    CONSTRAINT fk_run_skill_version_refs_exact_version FOREIGN KEY (
      skill_id, skill_version_id, payload_checksum,
      file_count, content_size_bytes
    ) REFERENCES skill_versions (
      skill_id, id, payload_checksum,
      file_count, content_size_bytes
    ) ON DELETE RESTRICT
);
```

`skill_project_id IS NOT NULL` 必须在 Project 分支中显式检查；PostgreSQL `CHECK` 对 `NULL` 结果视为通过，只写 `skill_project_id = project_id` 不能阻止空值绕过。

### 6.2 文件约束和索引

目标终态把 `skill_version_files.size_bytes` 的数据库单文件上限从当前 100 MiB 收紧到与新写入业务规则一致的 64 MiB：

```sql
CHECK (size_bytes >= 0 AND size_bytes <= 67108864)
```

保留 `size_bytes = octet_length(content)`、安全路径和 SHA-256 格式约束。仓库没有既定 `pgcrypto` 依赖，数据库不计算内容摘要；Version 创建和 Worker 读取两端都计算真实 SHA-256。

64 MiB 是本方案唯一的目标 Schema catalog，不提供在同一 release 中按部署选择 64/100 MiB 的漂移分支。Phase 0 已确认当前目标库 `files_over_64m=0`，最大单文件约 1.88 MiB；本次又固定走空库 recreate，因此不存在需要保留的 >64 MiB 历史 Version。未来若其他部署出现此类数据，必须保持旧库不变并走独立兼容工作包；importer 不得自动 split、静默截断、跳过或原地改写 immutable Version/checksum。

新增索引：

```sql
CREATE INDEX ix_skill_version_files_version_path_c
  ON skill_version_files (skill_version_id, path COLLATE "C");

CREATE INDEX ix_run_skill_version_refs_version
  ON run_skill_version_refs (skill_version_id);

CREATE INDEX ix_run_skill_version_refs_skill_scope
  ON run_skill_version_refs (skill_id, asset_scope);

CREATE INDEX ix_run_skill_version_refs_project_skill
  ON run_skill_version_refs (skill_project_id, skill_id);

CREATE INDEX ix_run_asset_versions_legacy_project_skill
  ON run_asset_versions (project_id, asset_id, version_id)
  WHERE asset_kind = 'skill'
    AND asset_scope = 'project'
    AND snapshot_schema_version IN (2, 3);

CREATE INDEX ix_run_asset_versions_legacy_skill_version
  ON run_asset_versions (asset_id, version_id)
  WHERE asset_kind = 'skill'
    AND snapshot_schema_version IN (2, 3);
```

第一条为 Worker 提供 canonical path 的候选有序访问路径；是否避免携带 `BYTEA` 的 Sort 必须由目标数据上的 `EXPLAIN (ANALYZE, BUFFERS)` 证明，不能只凭索引存在宣称。其余索引分别服务 Version/Skill FK 检查、Project purge、legacy hard-delete 双读，以及所有 scope 下文件 mutation trigger 对 exact v2/v3 Version pin 的快速检查。

### 6.3 Version facts 的形成与验证

Version 创建时，从已经规范化、验证并按 path 排序的 archive 一次计算：

```python
file_count = len(preview.file_views)
content_size_bytes = sum(item.size_bytes for item in preview.file_views)
payload_checksum = preview.checksum
```

当前 `payload_checksum` 的 canonical 合同只覆盖排序后的 `{path, sha256, size_bytes}`，不覆盖 `media_type`。目标实现必须保持该合同，避免已有 Version checksum 漂移；media type 继续独立校验并依赖行不可变保护。

写入顺序：

1. 验证文件数、单文件/总大小、路径、`SKILL.md`、frontmatter 和 secret declarations；
2. 计算每文件真实 SHA-256 和 Version facts；
3. 预留 Project quota；
4. 以 `files_sealed=false` 插入 `skill_versions`；
5. 设置 transaction-local `deerflow.asset_version_assembly`；
6. child trigger 只允许对该 exact、尚未 seal 的 Version 执行首次文件 INSERT；DELETE 在 assembly 路径始终拒绝；
7. 插入全部 `skill_version_files`；
8. repository 防御性重查 `count(*)/sum(size_bytes)` 和聚合 checksum；
9. 在同一事务把 `files_sealed` 单向更新为 `true`；
10. 一个只监听 parent INSERT/seal transition 的 `DEFERRABLE INITIALLY DEFERRED` constraint trigger 在 COMMIT 前要求 `files_sealed=true` 并再次核对 facts；
11. 任一失败同时回滚 Version、Files、quota 和 Builder Commit。

只监听 parent INSERT/seal，避免一个 12,922 文件 Version 在 COMMIT 时排队 12,922 次重复聚合。`files_sealed` 只能从 false 变 true，不能反向打开；已提交 Version 即使再次设置同一个 assembly GUC，也不能 INSERT/DELETE 文件。Worker 仍会重算每文件 hash 和聚合 checksum，facts 不是绕过内容校验的信任来源。

需覆盖两个已确认创建入口：Project/Builder/Import/Fork 的 `SkillService._create_version()`，以及 packaged System Skill bootstrap。bootstrap 的幂等匹配也必须比较 facts。

### 6.4 v4 manifest

Skill v4 `snapshot_json` 示例：

```json
{
  "schema_version": 4,
  "kind": "skill",
  "scope": "project",
  "asset_id": "11111111-1111-1111-1111-111111111111",
  "version_id": "22222222-2222-2222-2222-222222222222",
  "checksum": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "catalog_generation": 218,
  "dependency_version_ids": [],
  "skill": {
    "source": "skill_version_ref",
    "file_count": 12922,
    "content_size_bytes": 79243541
  }
}
```

v4 只允许上述严格 key/type 集，且 `octet_length(snapshot_json::text) <= 262144`。任何未知顶层/嵌套字段都拒绝，因此不能换一个字段名继续塞 Base64；明确禁止 `files`、`content_base64`、`archive_base64`、`codec`、`compressed_size`。`secret_requirements` 也不在 manifest 重复保存，Worker 从 exact immutable Version row 读取。typed columns、JSON identity、ref row 和 Version facts 必须逐字段一致。JSON 是可审计 manifest，不替代数据库 FK。

`schema_version` 是每条资产 Snapshot 的格式，不能继续作为整个 Run 的单一全局 codec 常量。实施时拆分 Skill 和非 Skill encoder/decoder；Agent/MCP 保持切换时已有格式，不因 Skill v4 被无意义升级。

### 6.5 数据库级双向完整性

普通 FK 只能保证：

```text
存在 ref ⇒ 存在完全匹配的 Run asset parent 和 Skill Version
```

它不能反向保证 v4 parent 必有 ref，也不能阻止已提交 Run 追加一组新的 parent/ref。目标为 `runs` 增加单向 `asset_closure_sealed`：Admission/import 在一个事务内先以 false 建立 closure，写完 Agent/Skill/MCP parents、refs 和 secret snapshots 后置 true。离线 importer 如需分批，先写非权威 staging，不得把半成品提交到 `runs/run_asset_versions`。

Run closure 完整性必须分成两层。Deferred trigger 只能验证 COMMIT 时的最终闭包，不能证明 child mutation 发生当时 Run 尚未 seal。

#### A. Immediate mutation gate

- `runs.asset_closure_sealed` 由 immediate `BEFORE UPDATE` trigger 强制只能 `false → true`，不能重开；新权威 Run 不得以 false COMMIT，`claim_next()` 也必须要求 exact Run 已 seal。
- `run_asset_versions`、`run_skill_version_refs`、Skill/MCP secret snapshots 的 `BEFORE INSERT/UPDATE/DELETE` trigger 必须锁 exact Run row，并在语句发生时读取 seal。
- INSERT 只允许 exact Run 当时仍为 false、处于 Admission/import assembly 且尚无可领取 Job；seal transition 与 child mutation 通过同一 Run row 串行化。child 要么先提交并被 final verifier 纳入，要么等待后看到 sealed 并立即失败。
- `run_asset_versions`、ref 和两类 secret snapshot 的 UPDATE 立即拒绝，不依赖事务最终状态。
- DELETE 只允许 owning Run cascade，或 `RetentionPurgeAuthority` 发出的 exact `resource_kind + project_id + owner_user_id? + explicit run_ids + purge_id` authorization。普通无 scope 的 `SET LOCAL` 标志不能绕过 eligibility、admission-closed 和 active-work locked reread；ref 不能被独立删除来解除 pin。

#### B. Deferred final-state verifier

`DEFERRABLE INITIALLY DEFERRED` constraint trigger 按受影响 Run key只验证最终状态：

- Skill `snapshot_schema_version=4` parent 必须恰有一条 exact ref；Skill v2/v3 必须没有 ref；非 Skill parent 必须没有 Skill ref；
- 所有 kind/schema 的 JSON `schema_version/kind/scope/asset_id/version_id/checksum/catalog_generation` 与 typed columns 一致；
- Skill v4 满足严格 key/type、`source=skill_version_ref`、facts 和 256 KiB 上限；
- 非终态、可领取 Run 的 `dependency_order` 从 0 连续无空洞，Agent/Skill/MCP closure 与两类 secret closure 完整且 owner/version 匹配；
- 新权威 Run 已 seal；privacy-purged terminal shell 可以 sealed 且无 closure。

Deferred verifier 不承担 mutation-time authorization。必须用两连接测试覆盖 seal 与 child INSERT 的两种提交顺序，并覆盖同一事务 `insert child → seal` 成功、`seal → insert child` 立即失败，以及 post-seal 直接 DELETE 即使最终 pairing 看似成立也立即失败。

privacy purge 后保留的 terminal Run/audit shell 可以合法没有 closure；受控 retention/import 必须把这类 shell 保持 `asset_closure_sealed=true`，而不是留 false。seal 表示“禁止再组装”，不等于每个历史 terminal shell 都必须有 assets。

因此 Run closure 才是 insert-once。单靠 `ON DELETE CASCADE` 和 ref pairing 不足以保护 parent；未经 retention 门禁直接 DELETE parent 或 post-seal 改写 secret Generation 必须由 PostgreSQL 测试证明会失败。

### 6.6 pin 优先于 maintenance 例外

当前 `skill_version_files` UPDATE 始终被不可变 trigger 拒绝；INSERT/DELETE 则可能被 `system_asset_upgrade`、`asset_version_assembly`、hard-delete 或 Project purge 例外放行。目标顺序必须改为：

1. 锁定 exact Skill Version 和 Skill identity；
2. 首先同时查询 `run_skill_version_refs` 和 exact `asset_id/version_id` 的 legacy v2/v3 Skill parent 是否存在 pin；
3. 有 pin 时无条件拒绝文件 INSERT/DELETE 和 execution payload 原地更新；
4. 无 pin 时，assembly 只允许 `files_sealed=false` 的 exact Version INSERT；
5. `files_sealed=true` 后永远不能再次 assembly；
6. hard-delete/due Project purge 只能走各自受约束的 DELETE 通道。

兼容期内 v2/v3 没有 ref FK，因此不能只靠应用层 hard-delete 双查：`skill_version_files` 的 child mutation trigger 也必须查询 legacy exact parent，并在其仍存在时拒绝 mutation。受约束 Run retention/Project purge 先删除对应 parent 后，Version 才可能解除该 legacy pin。这样 R1 legacy writer 和 R2 v4 writer 都有数据库最终防线；两套竞态分别验收。

同时收紧两项现有例外：

- 对 `skill_version_files` 分支，`asset_version_assembly` 只允许 `files_sealed=false` 新 Skill Version 的首次 INSERT，不允许删除文件；seal 后复用同一 GUC 必须失败。该 GUC 还服务 `agent_version_skill_refs`、`agent_version_mcp_refs`、`mcp_version_secret_slots` 的既有组装状态机，本方案不把 Skill 的 `files_sealed` 规则错误套到这些 child table；
- `system_asset_upgrade` 不允许同一 System Skill identity 原地替换内容。System Skill 内容变化继续按 ADR-0007 创建新 identity；revocation 治理字段可按既有规则改变，但不能改字节、checksum 或 facts。

## 7. Gateway Admission 改造

### 7.1 metadata-only 解析值对象

新增不含文件内容的值对象，例如：

```python
@dataclass(frozen=True, slots=True)
class ResolvedSkillVersionSnapshot(ResolvedAssetSnapshot):
    file_count: int
    content_size_bytes: int
    secret_requirements: tuple[SkillSecretRequirementSnapshot, ...]
```

R1 与 R2 必须明确分流：

- R1 就把 `ProjectAssetResolver._skill_snapshot()` 改为只读 exact `SkillVersionRow` checksum、facts、requirements 的 `_skill_version_snapshot()`；resolver、Worker plan 和运行时调用点都使用该 metadata-only 类型，不 select files/content。
- R1 唯一 allowlisted 的 `LegacyRunSkillSnapshotWriter` 在 resolver 之外、Admission 事务内按 exact resolved Version 读取 bytes，生成经过资源门禁的 v2/v3 Run payload。它内部拥有 Section 7.2 的数据库级 `LegacyAdmissionByteGate`；所有 Gateway、独立 Scheduler、Channel 和 Skill Builder 路径都只能经该 writer，不能在调用方各做一套进程内 semaphore。它不得被复用为通用 resolver，因此 R1 仍不能宣称整个 Gateway Admission 已 metadata-only。
- R2/v4 writer switch 删除/禁用上述 legacy Admission loader；resolver 无需再次改型，v4 writer 直接消费 R1 已形成的 metadata-only resolution。
- 现有带 `files` 的 `ResolvedSkillSnapshot` 只服务 legacy decoder/writer Adapter，不得回到 metadata-only Interface 或在 v4 写入后继续被调用。

### 7.2 R1 legacy Admission 单飞资源门

R1 的资源保护不使用 Worker Job 并发、Gateway 进程数、数据库池大小或 Project Run quota 作为证明。`LegacyRunSkillSnapshotWriter` 的 Implementation 内部拥有唯一 `LegacyAdmissionByteGate`；生产 Adapter 使用固定、命名空间隔离的 PostgreSQL `pg_try_advisory_xact_lock(bigint)`，在同一 Run Admission 外层事务中取得 database-wide 的单个 byte-bearing writer permit。

固定顺序是：

```text
Project → Membership → AccountPrivateLifecycle User guard
→ Thread/既有上层资源
→ exact Asset/Version metadata locks
→ release-calibrated envelope/encoded-ceiling check
→ non-blocking LegacyAdmissionByteGate
→ Skill content SELECT/detoast
→ Run parent/Job/其余准入写入
→ COMMIT/ROLLBACK 自动释放 permit
```

R1 artifact 内固定一个不可由各调用方覆盖的 `LegacyAdmissionPolicy`：

```python
@dataclass(frozen=True, slots=True)
class LegacyAdmissionPolicy:
    revision: int
    max_source_bytes_per_skill: int
    max_codec_working_set_bytes_per_skill: int
    max_encoded_bytes_per_run: int

    def canonical_digest(self) -> str: ...
```

writer 仅从这个 release-fixed policy 计算单 Skill source/codec envelope 和多 Skill累计 conservative encoded upper bound；调用方、Project 和请求都不能改 ceiling。R1 启用采用 homogeneous blue/green artifact，所有 Gateway/Scheduler writer role 启动时 readback 相同 artifact version + policy digest；缺失或不一致时 legacy writer fail closed，不能在 mixed-policy rolling window 接流量。多 Skill的 metadata upper bound 超过 `max_encoded_bytes_per_run` 时，在 permit 和 content query 前永久拒绝；不得等压缩完成后才发现累计 JSONB/WAL 已超限。

必须使用 `pg_try_advisory_xact_lock`，禁止使用会等待的 `pg_advisory_xact_lock`。gate 只尝试、不等待，因此不形成新的 wait edge；取得失败立即回滚并返回稳定 retryable busy。任何路径不得先取得 gate 后再锁 Project/Membership，也不得在 content 已经读取或 detoast 后才取得 gate。

writer 在尝试 permit 和读取任何 content 前，根据 sealed `file_count/content_size_bytes` 计算 release-calibrated codec memory envelope；R0 尚无 Version facts 时，只能用不选择 content 的 `count(*)/sum(size_bytes)` metadata query得到同一上界。超过单 Skill memory ceiling 或累计 Run encoded ceiling时先返回 `PRIVATE_WORK_TOO_LARGE`，不得尝试 gate，content 查询次数必须为零。只有合格请求才尝试 permit；取得后按 `dependency_order` 一次读取、校验、压缩并写入一个 Skill，释放该 Skill 的 source/codec working set 后才处理下一个；不能先加载完整 closure。这样“同时 oversize 且 gate busy”始终归类为永久 oversize，不随并发时序漂移。

大文件 hash、frame 构造、zlib 和 Base64 必须进入 cancellation-joined thread helper；取消返回前先等待真实线程终止，再让事务回滚和 permit 自动释放，禁止裸 `asyncio.to_thread` 在请求取消后继续占内存或写对象。事务失败、连接断开或进程崩溃均由 PostgreSQL 自动释放 permit，不新增 lease table、reaper 或分布式 token ledger。R1 吞吐量被有意限制为每个数据库一个 active byte-bearing writer；R2 禁用 legacy writer 时一并删除该临时 gate。

失败合同固定为：

- envelope 超限：`PRIVATE_WORK_TOO_LARGE`，永久拒绝，content query=0；
- gate busy：内部 `LegacyAdmissionBusy` → `PrivateWorkRetryableUnavailable`；HTTP 返回 `503 PRIVATE_WORK_UNAVAILABLE` 和 `Retry-After: 1`；
- Automation/Scheduler：整个 occurrence/Run/Job Admission 回滚，保留为可重试 admission，不写 terminal failure；
- Channel：返回可重试 delivery，不绑定半成品 Run；Skill Builder 返回 retryable unavailable，不留下 active operation/Run；
- 数据库失败沿现有 database-unavailable 路径；所有失败都不得部分提交 Run、Job、parent、quota 或 audit。

### 7.3 原子准入顺序

一笔 Run Admission 的目标顺序：

1. 先按仓库既有顺序锁 Project、Membership 并完成身份/capability 校验，再调用 Section 12.3 的 `AccountPrivateLifecycle.require_active_after_membership()` 以 User `FOR SHARE` 锁定 active generation；固定前缀是 `Project → Membership → User lifecycle → conversation/Thread → resource`，不得把 User 提到 Project 前，也不得依赖 FK 的偶发锁冲突替代显式治理锁；
2. Resolver 第一阶段只收集受信的 Agent 引用、binding target 和候选 asset/version identity，不在遍历原始 ref 顺序时逐个加锁；
3. 在现有上层锁之后，按统一 `(kind, scope, asset_id, version_id)` 顺序锁 asset、binding 和 Version；当前 resolver 在遍历时就锁行，必须先重构成该两阶段流程，不能事后补一轮排序锁；
4. 锁完成后重新验证 exact Agent/Skill/MCP closure；Current、Active、binding、Asset Suspension 和 revocation 只在这里判断，再恢复业务 `dependency_order`；
5. 对 exact Version 持有 `FOR KEY SHARE` 或 `FOR SHARE`，先读 checksum、facts、secret requirements；
6. R1 由 allowlisted legacy writer 先根据 locked exact metadata 验证 envelope/encoded ceiling；超限永久拒绝且不尝试 permit。合格后才非阻塞取得 database-wide permit；busy 返回 retryable unavailable，成功后才读取 bytes。R2 不取得该 gate，也不读取 bytes；
7. 以 `asset_closure_sealed=false` 创建 Run；随后按 release 分支写 Skill closure：
   - **R1**：`LegacyRunSkillSnapshotWriter` 写 `snapshot_schema_version IN (2,3)` 的 inline parent，不写 `run_skill_version_refs`；
   - **R2**：写 `snapshot_schema_version=4` 小 manifest parent，`flush` 后插入一一对应的 exact refs；
8. 两个分支共同写 Agent/MCP parents、Skill/MCP secret snapshots 和其余准入状态；
9. 将 `asset_closure_sealed` 单向置 true，触发 deferred final-state verifier；
10. 同一外层事务创建只能引用 sealed Run 的 Job、预留 quota、写 audit；
11. 单次 COMMIT。任何一步失败全部回滚，不能出现 Job 已可领取但 closure 未 seal/ref 缺失。lifecycle User `FOR SHARE` 必须从第 1 步一直持有到 COMMIT；无锁快速读只能用于尽早拒绝，不能替代 locked guard。

普通聊天、Automation、Channel 和 Skill Builder 最终共用 `create_run_with_snapshot_in_session()`；R1/R2 分支集中在 codec/repository，不在各入口复制实现。

同一 `request_id/run_id` 的 API retry 读取已存在的 Run/parents/可选 refs，不重新解析 Current；只有 Regenerate 创建新 Run并按当时 Current 重新准入。

### 7.4 Admission/delete 并发

Project Skill hard-delete 继续使用 Version `FOR UPDATE`，并同时查询：

- 新 `run_skill_version_refs`；
- legacy v2/v3 `run_asset_versions` partial index。

必须用两连接 PostgreSQL 测试证明两种提交顺序：

- Admission 先锁并提交 ref：delete 等待，随后返回 `AssetInUse`；
- delete 先锁并提交：Admission 等待，重读/FK 失败并映射为 stale。

另用两条并发 Admission、相反 Skill ref 输入顺序证明统一锁序不会死锁。R1 在 DB-wide gate、尺寸和资源门均通过时写 v3；任一门失败时自动拒绝受影响的大 Skill Admission并继续推进 R2，v2 永不作为默认安全回退。R2 写 v4。两者都要覆盖 Admission/delete 竞态；legacy 没有 ref FK，正确性更依赖双方对 exact Version 使用同一锁协议。

若真实 1 GiB PostgreSQL、8 个多来源并发 Admission attempt 的 backpressure、单个获准 writer 的大 Skill envelope 和恢复验收不能同时成立，R1 必须拒绝受影响的大 Skill 新 Admission，或保持停写直至受控 v4 gate 完成。压力源至少包含两个 Gateway process/replica，并与一个独立 Scheduler trigger 同时运行；不得因“reader 已支持”就继续写每 Run 约 70–107 MiB 的 payload，也不得把 8 个 attempt 误写成 8 个 heavy writer 可并发成功。

应用层检查提供友好错误；v4 依赖 exact FK/ref trigger，legacy 依赖双方共用 exact Version 锁协议和文件 child trigger 的 legacy-parent pin 检查，二者都是数据库最终正确性防线。

## 8. 清除隐式大 JSONB 加载

只新增 materializer 不足以解决内存问题。当前以下路径会在 materializer 之前或之后整行读取 `run_asset_versions`：

| 路径 | 当前用途 | 目标 |
| --- | --- | --- |
| `run_admission.py` 准入后回读 | 返回 persisted snapshot | 只返回 typed facts/ids |
| `run_execution/handler.py` claim 后 `_begin()` | 构造 `PersistedRunSnapshot` | 不含 `snapshot_json` |
| `asset_runtime.py` | 再次加载和解码全部 closure | metadata plan + materializer |
| `private_agent_runtime.py` 多处 | Skill secret、MCP inventory/调用 | explicit typed columns |
| `execution_approval.py` | closure fingerprint | explicit typed columns |

目标 `PersistedRunSnapshot`：

```python
@dataclass(frozen=True, slots=True)
class PersistedRunSnapshot:
    assets: tuple[ResolvedRunAssetFact, ...]  # 不含 snapshot_json
    mcp_secrets: tuple[RunMcpSecretSnapshot, ...]
    catalog_generation: int
```

改造规则：

1. 删除或限制通用 `list_assets_in_session()`，禁止生产调用者通过 `select(RunAssetVersionRow)` 整实体读取；
2. metadata-only repository 只 select 明确 typed columns，永不选择 `snapshot_json`；
3. 同一控制事务的 `RunRuntimeAssetPlanBuilder` 再用 scoped、explicit-column 查询逐行读取 Agent/MCP 小 payload 和 v4 Skill manifest，严格 decode 后并入一个 plan/fingerprint；
4. v4 Skill manifest 逐行读取并验证严格 schema/大小/ref；Agent payload 在这里提供 Skill/MCP closure 声明；
5. legacy Adapter 按 `dependency_order` 一次显式读取一个 Skill JSONB，写完 staging 后立即释放；
6. 增加源码契约测试：除 legacy Adapter 和明确的非 Skill payload loader 外，生产代码不得整实体选择 `RunAssetVersionRow`。

源码检查只能作为快速门禁；PostgreSQL SQL capture/statement inspection 还要证明 Worker/metadata paths 的 SELECT list 不含 `snapshot_json/content`，并用大 TOAST fixture 验证没有隐式 detoast。R1 Admission 只允许记录在 allowlist 中的 legacy writer 选择 Version content；R2 验收要求 Admission 也不再选择 content。这一步决定旧 v2/v3 的执行峰值能否从“整个 Run”降到“一个旧 Skill”；不能以 v4 新路径通过为由保留旧的整包查询。

## 9. Worker 流式物化

### 9.1 两段事务

物化不能在持有 Project/Run 写锁的同时读取大内容并写磁盘；当前旧 Schema 单文件上限为 100 MiB，目标 Schema 为 64 MiB。目标流程分成两个数据库事务：

#### 控制事务

1. orchestration Module 先调用 `PrivateWorkRevalidator.require(..., lock_mode="share")`，按 `Project → Membership` 取得 locked `ProjectContext` 并验证 capability；
2. 把该 locked context 传给 `authority.lock_and_assert_materialization_active_in_session(session, locked_context)`；authority 作为第一个 execution-row 入口只按 `Job → Run → active JobAttempt` 加锁，并验证 `job_id + lease_token + claim.attempt_id + expected_worker_id`、Run、Attempt、lease 和 cancel，取得 `MaterializationAttemptIdentity`；不得在 execution suffix 后再锁或重查 User、Project、Membership，也不得另写 capability 检查；
3. 读取全局有序 typed asset facts，LEFT JOIN Skill refs 和 Version facts；
4. 逐行严格 decode Agent/MCP 小 payload 和 v4 Skill manifest；legacy Skill 大 JSON 留给 Adapter；
5. 验证 exact Skill/MCP secret snapshots；
6. 形成包含 Agent/MCP payload、Skill facts/ref、secrets、Attempt identity 的不可变 `RunRuntimeAssetPlan` 及 canonical fingerprint；
7. 释放业务行锁。

#### 内容事务

每个 distinct v4 Skill Version 使用一个独立 Session/事务，而不是让一个 MVCC snapshot 跨越整个 closure：

```sql
SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
```

每个 source Adapter 在读取大 payload 前都必须向同一个进程级 `MaterializationMemoryBudget` 取得 weighted reservation：v4 weight 使用 sealed `content_size_bytes`；v2/v3 在 SELECT/detoast `snapshot_json` 前使用 release-calibrated、codec-specific 的最坏内存 envelope，覆盖允许的 encoded JSON/Base64/压缩帧、解码/解压内容、parser 和固定余量。legacy weight 不能等大 JSON 已加载后才计算，也不能只按压缩后大小计费。未取得 reservation 时不得打开大内容查询。

v4 reservation 成功后另开短控制事务，仍先以 revalidator 的 `Project → Membership` shared governance prefix，再由同一个 authority 取得 `Job → Run → active JobAttempt` execution suffix，并要求返回的 `MaterializationAttemptIdentity` 与 plan 相同。Project/Membership shared lock 只覆盖短控制 SQL，不跨 Version content I/O。

该 Version 的 REPEATABLE READ 事务先重读 exact ref/Version facts和全部不含 content 的文件元数据，在此建立固定 MVCC snapshot并要求与 plan 完全一致；再按 Section 9.3 的 bytes+rows 批次读取内容。reservation 覆盖数据库 decode、hash/write、该 Skill 的 `SKILL.md` parse 和相关 Python bytes 引用，确认引用释放后才在 `finally` 归还。这样单个事务最多跨一个 Version，业务行锁不跨文件 I/O，进程聚合内容内存也不随 8 个 Job 无界叠加。

数据库 statement timeout 只约束 SQL；另用应用层 `asyncio.timeout` 约束整个 materialization，并在所有 timeout/cancel 路径 join 文件线程、关闭 cursor/Session。`idle_in_transaction_session_timeout` 必须考虑单行本地写盘时间，不能把它误当成应用总预算。

所有内容完成后：

1. 关闭最后一个 cursor 和只读事务；
2. 用新控制事务再次按 `Project → Membership → Job → Run → active JobAttempt` 调用 revalidator + authority，并要求返回 identity 与 plan 相同；随后在同一 Session 重读 ordered plan fingerprint，禁止另查 active Attempt或把 User 插入该锁图；
3. 只有 fingerprint、Run 状态、Attempt identity 和 authority 都未改变，才返回尚未取得 provider lease 的 materialized handle；该 handle 仍只是非权威 staging/source，不能直接交给 graph；
4. Executor 在真正 provider acquire/restore 前开启短事务 A：`Project → Membership → Job → Run → active JobAttempt`，重验 exact job/lease token hash/attempt ID/worker ID/空 outcome/lease deadline/cancel，并在持锁期间把 owner metadata 持久、fsync 地推进为 `acquiring`，然后 COMMIT；现有只调用 `_check()/assert_execution_active()` 的独立 `before_sandbox_restore()` 必须重构，不能绕过 Project/Membership locked reread；
5. 调用 provider acquire + guest readback；随后开启短事务 B，再按同一锁序重验 authority，并在持锁期间记录 exact provider lease、把 metadata 推进为 `mounted`。只有事务 B 成功提交，mount 才可交给 runtime；若发现 cancel/lease loss、事务失败或 readback unknown，立即进入 typed release/reconciliation，绝不能发布该 mount。

这些 fence 都不取得 User lifecycle lock：它们收敛既有 execution，并依靠 Project/Membership 与 purge 互斥、再由 Job cancel/fence 阻止发布。单靠 materialization 前后两次查询仍有 TOCTOU；真正关闭 mount/删除窗口的是 acquire 前后双 fence和 retention 规则：exact scope 未 admission-closed、仍有 ready/due-retry Job、有效 lease/active Attempt、`acquiring|mounted|release_pending`/provider readback unknown，或 safe terminalize/requeue 尚未持久化时，都不允许物理删除 closure/ref。

### 9.2 metadata plan 查询

第一条查询只选择小字段，不选择 `snapshot_json` 或 `content`：

```sql
SELECT
  asset.asset_kind,
  asset.dependency_order,
  asset.asset_scope,
  asset.asset_id,
  asset.version_id,
  asset.payload_checksum,
  asset.catalog_generation,
  asset.snapshot_schema_version,
  ref.skill_project_id,
  ref.skill_id,
  ref.skill_version_id,
  ref.payload_checksum AS ref_checksum,
  ref.file_count AS ref_file_count,
  ref.content_size_bytes AS ref_content_size,
  skill.scope AS version_skill_scope,
  skill.project_id AS version_skill_project_id,
  version.payload_checksum AS version_checksum,
  version.file_count AS version_file_count,
  version.content_size_bytes AS version_content_size,
  version.files_sealed,
  version.secret_requirements
FROM run_asset_versions AS asset
LEFT JOIN run_skill_version_refs AS ref
  ON ref.project_id = asset.project_id
 AND ref.owner_user_id = asset.owner_user_id
 AND ref.thread_id = asset.thread_id
 AND ref.run_id = asset.run_id
 AND ref.asset_kind = asset.asset_kind
 AND ref.dependency_order = asset.dependency_order
LEFT JOIN skills AS skill
  ON asset.asset_kind = 'skill'
 AND skill.id = asset.asset_id
LEFT JOIN skill_versions AS version
  ON asset.asset_kind = 'skill'
 AND version.skill_id = skill.id
 AND version.id = asset.version_id
WHERE asset.project_id = :project_id
  AND asset.owner_user_id = :owner_user_id
  AND asset.thread_id = :thread_id
  AND asset.run_id = :run_id
ORDER BY asset.dependency_order;
```

Version 必须从受信 parent 的 `asset.asset_id + asset.version_id` 直接 JOIN，而不能只经 v4 ref JOIN；否则 v2/v3 因无 ref 永远拿不到 immutable Version facts/secret declarations。随后在同一事务按 exact parent key 逐行执行小 payload 查询：Agent/MCP 和 v4 Skill 选择 `snapshot_json` 并立即 strict decode；v2/v3 Skill 只登记 legacy source，不在这里 detoast 大 JSONB。所有 loader 都验证 typed discriminator/identity 与 JSON 一致；数据库 seal trigger 是写入防线，loader 是执行期 fail-closed 防线。

plan validator 必须验证：

- `dependency_order` 是全局连续的 `0..N-1`，Agent 在前、Skill 居中、MCP 在后；
- 全部资产的 `catalog_generation` 一致；
- v4 Skill 有且只有一条 exact ref，v2/v3 Skill 无 ref，非 Skill 无 Skill ref；
- parent/Version 的 Project、scope、asset/version identity、checksum 和 facts 完全一致；v4 还要求 ref 与两者完全一致；
- exact Version 已 `files_sealed=true`；
- 同一 Run 不重复引用同一 Skill Version；
- Agent 声明的 exact Skill/MCP closure 与实际 rows 集合相等；
- Skill secret snapshot 的 Skill/Version/name/revision/generation identity 与 exact Version 声明一致。

执行路径不能 join `skills.current_version_id`，不能调用 Resolver 重新应用 Current、binding、suspension 或 revocation。

### 9.3 metadata-first 的 bytes+rows 有界批次

不能再用 `yield_per=1` 作为字节边界。当前 SQLAlchemy 2.0.49 asyncpg dialect 在 adapter buffer 为空时固定执行底层 `fetch(50)`；外层只返回一行时，其余最多 49 行已经 decode 并留在 dialect buffer，`max_row_buffer=1` 也不会改变这一点。

首期选定的实现是**同一 Version REPEATABLE READ snapshot 内先取无 content 元数据，再按字节和行数分批取 content**。第一条查询只取小字段：

```sql
SELECT
  file.path,
  file.media_type,
  file.size_bytes,
  file.sha256
FROM run_skill_version_refs AS ref
JOIN skill_version_files AS file
  ON file.skill_version_id = ref.skill_version_id
WHERE ref.project_id = :project_id
  AND ref.owner_user_id = :owner_user_id
  AND ref.thread_id = :thread_id
  AND ref.run_id = :run_id
  AND ref.dependency_order = :dependency_order
  AND ref.skill_version_id = :skill_version_id
ORDER BY file.path COLLATE "C";
```

materializer 先严格校验全部 metadata path/order/size/hash 和 Version `file_count/content_size_bytes`，再把连续 canonical path 划分为批次：

- 普通批次同时满足 `row_count <= materialization_batch_max_files` 和 `sum(size_bytes) <= materialization_batch_max_bytes`；
- 单文件大于 batch byte limit 时允许成为唯一的 singleton batch，但仍受 64 MiB 单文件上限和进程级 Version reservation 保护；
- 每个批次记录 exact first/last path、期望 path 序列、行数和字节和，内容结果有缺失、额外、重复或乱序即 stale。

每个内容查询只覆盖一个已规划的连续 path range：

```sql
SELECT
  file.path,
  file.size_bytes,
  file.sha256,
  file.content
FROM run_skill_version_refs AS ref
JOIN skill_version_files AS file
  ON file.skill_version_id = ref.skill_version_id
WHERE ref.project_id = :project_id
  AND ref.owner_user_id = :owner_user_id
  AND ref.thread_id = :thread_id
  AND ref.run_id = :run_id
  AND ref.dependency_order = :dependency_order
  AND ref.skill_version_id = :skill_version_id
  AND (file.path COLLATE "C") >= :first_path
  AND (file.path COLLATE "C") <= :last_path
ORDER BY file.path COLLATE "C";
```

即使 dialect 一次 refill 50 行，单次查询能返回的**全部** content 也已由 batch 计划限制，因此安全证明来自 SQL result 的总 `size_bytes`，不是 ORM 逐行 API。Implementation 仍逐行消费并在 `finally` 显式关闭每个 stream；`yield_per` 可作为消费细节，但不得出现在内存证明中。

- 选择 explicit columns，不加载 ORM entity，避免 identity map 留住 `BYTEA`；
- content 结果禁止 `.all()`、先转 `tuple`、跨 batch 缓存或无界 producer queue；metadata 小行可以一次保留；
- 内容事务不对文件行加 `FOR UPDATE/FOR SHARE`，一致性来自 immutable Version、exact ref 和 REPEATABLE READ；
- 取消、timeout 和异常路径显式关闭当前 stream/Session、归还 byte reservation，并在 join 文件线程后删除 staging；
- `materialization_batch_max_bytes/files` 是 Worker 内部配置，首个 release 的值由 Section 17.3 的 1 GiB/8 并发验收校准，不从调用方传入。

若未来改用 raw asyncpg Adapter，必须以真实 iterable cursor `prefetch=1` 或显式 `fetch(1)` 证明底层边界，并由该 Adapter 独立承担 transaction、禁止连接并发复用、cancel、cursor close 和 connection invalidation 测试；取得 raw connection 并不自动满足这些合同。首期不依赖该替代路径。

legacy v2/v3 只允许 Adapter 按一个 `dependency_order` 精确取一行，不允许先取整个 closure：

```sql
SELECT snapshot_json
FROM run_asset_versions
WHERE project_id = :project_id
  AND owner_user_id = :owner_user_id
  AND thread_id = :thread_id
  AND run_id = :run_id
  AND asset_kind = 'skill'
  AND dependency_order = :dependency_order
  AND snapshot_schema_version IN (2, 3);
```

Adapter 必须先取得该 schema 的 full legacy envelope reservation，才执行查询；reservation 持有到 JSON/Base64/压缩/解压对象、文件 bytes 和该 Skill parser 引用全部释放，并在 `finally` 归还。随后对结果执行 `one_or_none` 和 strict typed/JSON identity 校验，并把 legacy JSON 中的 secret requirements 与控制事务读取的 exact Version declarations 比较；不一致即 stale。文件 checksum/identity 和该比对结果共同绑定到最终 materialization fingerprint。写完这个 Skill 的 staging 后立即释放 decoded payload，再进入下一个 order。只有这一条兼容查询可以 detoast legacy 大 JSONB，但它不能绕过 process budget。

### 9.4 逐行校验、批次边界和有界写盘

materializer 创建随机 owner root 后、读取第一行内容前，先以 Section 11.2 的 durable 原子写协议保存 `state=materializing` 和 owner/job/attempt/worker identity；该状态永远不能传给 Sandbox provider。这样长时间写盘期间即使 SIGKILL/OOM，reaper 也能在 Job/Attempt 失活且 grace 结束后证明它从未 acquire。

每行立即完成：

1. 规范相对 POSIX path，拒绝绝对路径、`..`、空段和超长路径；
2. 检查严格递增 canonical path；
3. 检查 NFC/casefold 重名和文件/目录前缀冲突；
4. 检查 media type；
5. 检查 `len(content) == size_bytes`；
6. 检查单文件不超过 64 MiB；
7. 计算并比较 `sha256(content)`；
8. 检查累计文件数和总大小不超过 ref/Version/v4 facts；
9. staging 目录保持 `0700`，文件以 `0600` 写入；
10. 释放当前应用 row/bytes 引用，再读取下一行；批次结束时关闭 result，确认 batch 引用释放后才开始下一批。

Executor 必须在调用 `asset_runtime.materialize()` **之前**把 Job lease authority 的 cancel callback 绑定到 `boundary.request_local_cancel`。writer 在每个有界 byte/time/file budget 检查本地 cancel/lease signal，并在 Version 边界执行 durable in-session authority check；不能等整棵树完成才发现 Stop。

大文件 hash/write、`SKILL.md` parse、rename 和 rmtree 只能使用仓库既有的 cancellation-joined thread helper：取消 await 时先 shield/join 底层线程，确认它不再访问路径后再传播 cancellation。普通 `asyncio.to_thread` 被取消后线程仍会继续运行，可能与 cleanup 竞态，因此不能作为生产等价实现。不能预取无界队列；Interface 不暴露 batch 大小，Implementation 必须使用 Section 9.3 的 bytes+rows 批次，并由资源验收确定配置值。

### 9.5 增量聚合 checksum

当前 Version checksum 等价于：

```python
hashlib.sha256(
    json.dumps(
        [{"path": path, "sha256": sha256, "size_bytes": size}, ...],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
```

Worker 不构造完整 list；按 path 顺序向 hash 写入 `[`、逗号分隔的 canonical object 和 `]`。必须保持 `ensure_ascii=False`、UTF-8、`sort_keys=True` 和紧凑 separators，特别测试非 ASCII path 与当前 `_snapshot_checksum()` 完全一致。

Version 完成时同时验证：

- 实际 `file_count`；
- 实际 `content_size_bytes`；
- 聚合 `payload_checksum`；
- 唯一且可解析的 `SKILL.md`；
- runtime name 格式和整个 closure 内的唯一性；
- parse 后 `required_secrets` 等执行安全字段与 exact Version `secret_requirements` 一致。

只有所有 Version 成功，才在专用 owner root 内把完整 `.staging` 原子 rename 为最终 `tree`。rename 前，在 cancellation-joined helper 中把 regular file 收敛为 `0444`、目录收敛为 `0555`，再次拒绝 symlink/special file；owner root 和其外部 metadata 仍分别是 `0700/0600`。这样 host 上其他非 owner 进程不能穿过 owner root，而 bind 到 `/mnt/skills` 后，AIO 当前的非特权 `gem` 或其他实际配置的 runtime identity 能读取 tree，且只读 mount + DAC 均不能写。`source.worker_root=owner_root/tree` 直接包含 `public/custom`；内部 `_owner_root` 才是 cleanup 删除对象。最终 rename 与 metadata `materializing → materialized` 的持久状态推进完成后才返回 handle；任何更早失败先 join 所有文件线程，再删除整个 owner root，不返回半棵树。

### 9.6 内存边界的真实含义

当前 SQLAlchemy asyncpg 路径会隐藏 buffer 50 个已 decode row，因此原“每次只持有一个最大 64 MiB 文件”的假设不成立。单 Version 的 `content_size_bytes` 上限为 100 MiB，使一个 Version 查询的 content payload 上界不是理论 50×64 MiB，而是约 100 MiB；但当前根配置对应 8 个并发 Job 时，仍可能叠加到约 800 MiB content，尚未包含 Worker baseline、Record/协议对象、hash/write 副本和 parser working set。

v4 的可证明边界改为两层：

```text
单个内容查询 decoded content
  <= max(materialization_batch_max_bytes, 当前 singleton file size)

同一 Worker 进程的 active source reservation
  = sum(v4 sealed content_size_bytes
        or v2/v3 codec worst-case memory envelope)
  <= materialization_max_inflight_bytes

其中 v4 active reservation 之和
  <= materialization_v4_max_inflight_bytes
```

`MaterializationMemoryBudget` 是 materializer 内部的分类 process-wide weighted gate；v4 同时占用 total gate 与更小的 v4 aggregate gate，legacy 只占 total gate。v4 在任何 exact Version metadata/content `SELECT` 前按 sealed `content_size_bytes` 计费，legacy 在读取大 JSONB 前按完整 release envelope 计费，均持有到数据库 rows、decode/hash/write buffers 和该 Skill parser 引用全部释放，再在 `finally` 归还。启动配置至少包含：

```yaml
worker:
  materialization_max_inflight_bytes: 1610612736
  materialization_v4_max_inflight_bytes: 268435456
  materialization_batch_max_bytes: 8388608
  materialization_batch_max_files: 50
```

配置加载进入 `WorkerConfig` 和 `config.example.yaml`；process total budget 必须至少容纳所有启用 Adapter 中最大的单请求 envelope：纯 v4 至少容纳合法最大 100 MiB Version，dual-reader release 还必须容纳一个已验收的 v2/v3 worst-case envelope，否则启动 fail closed或禁用对应 legacy 执行。静态对象生命周期初算的 v2 448 MiB/v3 768 MiB 并未覆盖 asyncpg/JSONB、Python allocator、write/parser 与等待任务的真实组合峰值；完整 12,922 文件、79,243,539-byte ppt-master 混合实跑在所有 source query 已移到 reservation 后仍测得 1,216,462,848-byte Worker RSS delta。因此首个 dual-reader release 将 v2/v3 都按 1.5 GiB reservation，使任一 legacy read 独占 total gate并保留固定余量。独立的 256 MiB v4 gate 保留既有验收并发，增大 total gate不得放宽 v4 active sum。Phase 0/2 在 1 GiB PostgreSQL、本次固定的单 Worker process、`max_concurrent_jobs=8` 下校准并把验证值固化到模板和配置测试；更多 process/replica 未经同等复测时 readiness fail closed。验收还要 readback `worker_nodes.max_concurrent_jobs`，避免只改测试参数却没有让实际 Worker 注册对应 capacity。

该 reservation 是 release gate，不等于瞬时对象大小；release 门必须同时给出 v2/v3/v4 各自的 source weight、Worker baseline、驱动/写盘/parser 安全余量和实测 peak RSS，并硬断言 `peak RSS - baseline RSS <= materialization_max_inflight_bytes`。若 8 Job 混合负载下不能留出稳定余量，就提高经实测校准的 legacy/total envelope、收紧独立 budget/并发闸门或阻断相应 Adapter，而不是用 Job 数或 `yield_per=1` 代替证明。

现有 `parse_skill_file()` 会同步读取整个 `SKILL.md`；首期把它放入 cancellation-joined thread，并用 64 MiB `SKILL.md` fixture 验证 RSS、事件循环响应和 Stop。若该峰值不可接受，应另行收紧 runtime document 业务上限，不能暗自改变已有 immutable Version。

如果未来必须把单文件峰值进一步限制到例如 8 MiB，需要另一个 Chunk/Object Storage 设计；这不是 server-side cursor 自动提供的能力，也不在首期范围。

## 10. v2/v3/v4 兼容合同

| typed ref | Skill Snapshot | Source Adapter | 结果 |
| --- | --- | --- | --- |
| 无 | v2 inline Base64 | legacy v2 | 执行 Run 自身 bytes |
| 无 | v3 compressed archive | legacy v3 | 执行 Run 自身 bytes |
| exact ref | v4 `skill_version_ref` | pinned Version | 流式读取 exact Version rows |
| 有 | v2/v3 | 无 | `RUN_ASSET_STALE` |
| 无 | v4 | 无 | `RUN_ASSET_STALE` |
| 任意 | 未知 schema/source 或 identity 不一致 | 无 | `RUN_ASSET_STALE` |

Legacy Adapter 规则：

- 按 dependency order 一次只读取一个 Skill JSONB；
- 解码后立即写入 staging 并释放对象；
- 不回退到 Current Version；
- 不在执行时自动回写 v4；
- legacy Snapshot 损坏时永久 fail closed；
- v3 首期复用现有 decoder，因此峰值仍可能包括一个旧 Skill 的 Base64、压缩帧和解压帧。兼容期目标是从“整个 Run”降到“一个旧 Skill”，不是伪称已经达到 v4 单文件边界。

Writer/reader 切换必须分离：先交付可读 v2/v3/v4 的 R1；其 legacy writer 只在 homogeneous `LegacyAdmissionPolicy`、database-wide single-flight gate、单 writer envelope、目标 legacy Worker 共存负载和 1 GiB验收通过的格式/尺寸范围工作，超限永久拒绝，gate busy 则在 content 前 fail-fast。确认所有 Gateway/Worker 都具备 v4 reader、两个深工作包已集成后，R2 才同时切 metadata-only Admission 与 v4 writer。已写入 v4 后不能回滚到只认识 v2/v3 的二进制。

### 10.1 稳定错误映射

| 失败 | 内部/公开语义 | 重试 |
| --- | --- | --- |
| ref/schema/source/identity/facts 不一致 | `RunSnapshotAssetStale` → `PrivateWorkAssetStale` → `RUN_ASSET_STALE` | 永久，不自动重试 |
| path、单文件 SHA、聚合 checksum、文件数或总大小不一致 | `RUN_ASSET_STALE` | 永久，不自动重试 |
| R1 codec envelope/encoded ceiling 超限 | `PRIVATE_WORK_TOO_LARGE`；content query=0 | 永久，不自动重试 |
| R1 database-wide permit busy | `PrivateWorkRetryableUnavailable` → `503 PRIVATE_WORK_UNAVAILABLE` + `Retry-After: 1` | 可重试；原事务完全回滚 |
| 内容/plan 查询遇到 PostgreSQL recovery、连接断开、statement timeout | `PrivateWorkUnavailable` | 仅 graph/副作用前按 Job policy 安全重试 |
| exact authority in-session fence 因数据库异常无法证明 | `AuthorizationRevoked` / lease lost | 原 attempt 失权，走 lease recovery；不能降级成普通 unavailable 后继续 |
| staging `ENOSPC`/`EIO` | `PrivateWorkUnavailable`，先完整清理 | 仅 graph 前按 policy 重试 |
| lease loss/fencing 失败 | `AuthorizationRevoked` 或 `EXECUTION_AUTHORITY_UNAVAILABLE` | 进入既有 lease recovery，不在原 attempt 继续 |
| 用户取消 | cancelled | 不伪装成 unavailable |
| cleanup 失败 | operational fault | 保留主异常/业务终态 |
| 未知异常 | 既有 fail-closed 路径 | 不暴露 SQL、路径或原始异常 |

新 v4 不应再因为 Skill 文件内容触发 Run JSONB 的 `PrivateWorkTooLarge`；archive limits 已在 Version 创建阶段执行。Worker 若读到超出持久化声明的内容，属于 stale/integrity failure，不是客户端 413。

## 11. Sandbox 和临时资源所有权

### 11.1 首期 Provider/mode 验收矩阵

以下是全文唯一的首期支持与验收清单；同一个 provider 在不同部署拓扑下不能共享未经证明的 path 结论：

| ID | Provider/mode | 字节交付方式 | 必须取得的真实证据 |
| --- | --- | --- | --- |
| P-01 | Native `LocalSandboxProvider` | 受信 Worker path，由本机执行 identity 读取 | mapped-root containment；受控文件可读、tree 不可写；release 后 source 不再被执行侧持有 |
| P-02 | Native `AioSandboxProvider` + `LocalContainerBackend` | 本机 Worker 把受信 path 只读 bind 给 Docker/Apple Container | 实际非特权 guest 读成功/写失败；owner label；exact destroy 与 absent readback |
| P-03 | Compose Worker + `AioSandboxProvider`/DooD | Worker path 经受信相对路径转换为 host Docker daemon path | Worker/daemon 双视图 containment；实际 Compose guest 读成功/写失败；cross-Worker exact reconcile |
| P-04 | `BoxliteProvider` | Run 专属 micro-VM read-only volume | 受支持 Linux/KVM 目标上的真实 VM probe；owner label；exact destroy 与 VM absent readback |
| P-05 | `E2BSandboxProvider` | provider-owned upload 到 Run 专属 E2B artifact | 使用受控测试账号的真实 VM probe；实际非特权 identity 读成功/写失败；exact kill 与 sandbox absent readback |

P-01～P-05 的 Adapter 合同仍是本方案固定实现范围，mock/contract test 只能证明形状，不能冒充真实 provider/mode probe。发布能力按实际取得证据的 provider/mode 封闭启用：当前 release 配置中启用的每个 provider 必须先有真实证据；缺少外部环境的 provider 保持 v4 fail closed，执行者继续完成其他工作并记录未满足的证据，不暂停等待批准，也不把 skip 冒充通过。本次不删除任何既有 provider；Remote Kubernetes 不在 P-01～P-05 内，也不计入本期完成证明。

目标安全链：

```text
RuntimeOwnedMaterializedRunSkillTree.source
→ PrivateRunFileAuthority（独占 provider lease）
→ SandboxProvider.prepare_run_readonly_mount()
→ ProviderRunMountLease
→ /mnt/skills:ro
```

专用 tree 位于 `get_paths().run_skill_materialization_root()`（Worker 视图），owner root 以服务端随机 owner ID 创建并设为 `0700`，不能由客户端 ID 拼出可控路径；对应 host-daemon 视图只能由 `get_paths().host_run_skill_materialization_root()` 从受信 `ACT_WEAVE_HOME ↔ ACT_WEAVE_HOST_BASE_DIR` 映射派生。materializer source 只含 opaque owner ID 和 Worker path，不含 caller 提供的 host path：

- P-01/P-02：provider 验证 source 位于专用 root 后使用 resolved Worker path；P-02 还必须由本机 container backend 完成 guest readback；
- P-03：provider 计算 Worker root 下的相对路径，再拼入 host-mapped root；Worker path 和 Docker daemon path 都必须分别 containment/readback 成功；
- P-04：provider 对受信 source 做 containment 后创建 Run 专属 micro-VM read-only volume，owner label、exact destroy 和 VM absent readback 纳入同一 lease proof；不得继续接收 caller raw `host_path`；
- P-05：保留现有 provider-owned upload/provision 语义，把受信 source 上传到 Run 专属 VM artifact、以实际非特权 identity 做读/写探测，并以 E2B sandbox identity 做 exact kill/absent readback。它不是 Remote Kubernetes host-path mount，不能因“远端”而从本期 provider 适配范围遗漏；
- Project/owner/Run scope、owner metadata、受保护目录、symlink、socket 和 overlap 任一不符都 fail closed；
- provider 返回含 `provider_kind + opaque sandbox_id/mount_lease_id` 的 `ProviderRunMountLease`，并提供 exact `readback()`/`release()`/`destroy()`；该 lease 只能由 `PrivateRunFileAuthority` 持有，Runtime tree token 只持 source，Executor 不再直接构造 raw `host_path` mount。
- provider acquire 的成功条件包含执行侧 readback：P-01 使用本机实际执行 identity，P-02～P-05 使用 Sandbox 实际非特权 runtime identity（当前 AIO image 为 `gem`），读取一个受控 manifest/`SKILL.md` 成功，写入探测必须因只读 tree/`/mnt/skills:ro` 失败。禁止假定 host Worker UID 与 guest UID 相同；若 rootless daemon、UID/GID 或 bind-mount DAC 不能满足 `owner root 0700 + tree dirs 0555 + files 0444`，该 provider 配置 fail closed，而不是临时放宽 owner root 或以 root 运行 graph。

这些 provider-facing 类型全部定义在 `backend/packages/harness/deerflow/sandbox/`。P-01～P-05 都必须适配该基类合同和各自的 provider proof；若某 Adapter 或真实证据尚未完成，该 provider 对 v4 保持 fail closed，不能静默降级为旧 private mount，也不因此中断其他工作包。`RunMountReleaseOutcome` 是封闭 union：`NotAcquired` 只能证明 owner metadata 仍为从未进入 provider 调用的 `materialized`；`Released` 必须携带与 owner/lease 匹配的 `ProviderMountAbsentProof`；`Orphaned` 表示 acquire/release/readback 任一步为 unknown，携带稳定原因和最后 lifecycle state。app 层不得用 `bool`、`None` 或捕获异常后猜测 absent。

这既保持 `/mnt/skills` 只读，也使 Compose DooD 可验收；不能把 Worker container 内的 `/tmp/...` 原样交给 host Docker daemon。

### 11.2 唯一清理所有者

materializer 返回 pending handle 后、`PrivateAgentRuntime` 构造/MCP discovery 成功前，由 caller 的 `AsyncExitStack` 持有；只有 Interface 中声明的 `transfer_to(runtime)` 成功后才产生 runtime-owned token。转移前 caller cleanup，转移后 pending close no-op、runtime token cleanup，两方绝不能同时删除。`adopt_materialized_skill_tree()` 必须满足 Section 5.2 的强异常安全，禁止“先保存 token、再执行可能抛错的初始化”。旧 `skill_root` 可暂时作为 `source.worker_root` 的只读 property 转发，但不再独立删除 root。

materializer 创建 owner root 后、任何内容写入前，先以 `state=materializing` 原子、持久写入含 owner ID/job/attempt/worker 的无 Secret metadata；完成 tree mode/rename 后再推进为 `materialized`。`PrivateRunFileAuthority` 在发出**任何** provider acquire 调用前，必须在 Section 9.1 短事务 A 持有 `Project → Membership → Job → Run → Attempt` fence 时把 metadata 原子推进为 `state=acquiring`，使用临时文件、file `fsync`、rename 和 parent-directory `fsync` 后才能 COMMIT并调用 provider；若数据库事务随后失败，该保守的 `acquiring` 仍交 reaper 对账，不能退回 `NotAcquired`。provider 把同一 owner label 作为 Sandbox/container 创建的一部分。acquire 和 guest readback 返回后，短事务 B 再次取得同一 fence、原子写回 `provider_kind/sandbox_id/mount_lease_id` 并推进为 `mounted`；只有 B 提交后才允许 graph 使用 mount。即使进程崩在 provider 调用开始到 lease identity 回写之间，`acquiring + owner label` 也会迫使 reaper 枚举/readback，而不会误判为从未 acquire；首期不为此新增数据库 registry。

lifecycle 只允许 `materializing → materialized → released`（`NotAcquired`）、`materializing → materialized → acquiring → mounted → released`，以及 `acquiring|mounted → release_pending → released`。`materializing` 永远不能调用 provider；`materialized` 只有在同一 owner token 证明 acquire 从未开始时才能在进程内直接收敛为 `NotAcquired`；`acquiring` 永远不能凭缺少 lease ID 当成未 acquire。清理顺序是：

```text
RunFileAuthority.release()
→ Released(ProviderMountAbsentProof) / NotAcquired / Orphaned(unknown)
→ 独立关闭 MCP run sessions
→ RuntimeOwnedMaterializedRunSkillTree.finalize(outcome)
→ Released/NotAcquired: 验证 owner/state/proof 后删除 owner root
→ Orphaned: 原子写 release_pending、移交 reaper、token 失效但不删 root
```

共享 `RunFileAuthority.release() -> RunMountReleaseOutcome` 是唯一可调用合同；`PrivateRunFileAuthority` 在其内部完成 provider lease cleanup，必须吸收 provider cleanup 异常并返回 typed `Orphaned`，不能另起 shared lifecycle 不会调用的旁路方法，也不能在丢失证据后只抛异常/返回 `False`。该方法幂等且 outcome 单调：重复调用返回缓存的 `NotAcquired/Released`；`Orphaned` 只能经新的 exact readback 升级为 `Released`，不能降级或丢掉 owner/proof。共享 `PrivateFileLifecycle.release()` 改为 joined 地返回 outcome 本身，cleanup-success `bool` 另行派生；所有旧 proxy/test double 同步改签名。Worker finally 无论 mount outcome 如何都单独、joined 地关闭 MCP run sessions，并在独立 finally 中把 outcome 交给 tree `finalize()`，不能因 MCP close 失败跳过 finalize。provider release 或 readback 未确认成功时，**禁止**删除 owner root；`finalize(Orphaned)` 只持久化 `release_pending`/handoff 并使 in-process token 失效。cleanup fault 作为 operational fault 记录且不覆盖主异常，但“保留 root 等待可证明回收”是安全行为，不能为了表面无泄漏而删除仍被挂载的目录。测试成功、graph 失败、cancel、mount acquire 失败、materialization 失败、release exception/readback unknown，以及所有权转移前后的每条路径。

进程 SIGKILL/OOM 不会执行 finally。owner metadata 至少包含 schema version、lifecycle state、state generation、owner ID、worker/job/attempt、创建/更新时间、provider kind 和 opaque Sandbox/mount lease identity。Worker 启动时的安全 orphan reaper 对每个 owner 先申请数据库 session-level advisory lock（稳定 namespace + owner ID hash；不持有业务事务）。数据库不可用、未获锁或 lock ownership 不明时只保留 root；只有 winner 可以执行 enumerate/destroy/readback/delete，并在 `finally` 释放 advisory lock。provider destroy/readback 必须幂等：

1. 只扫描专用 materialization root，并按 owner ID 请求 provider exact enumerate/readback；AIO reconciliation 不得继续跳过带该 owner label 的 private container，BoxLite/E2B 同样按 owner/sandbox identity 对账；
2. Job/Attempt 或 mount 仍 active、或仍在 grace period 时跳过；
3. provider 确认 active 但 Job 已失权时先 exact destroy，再二次 readback；
4. 只有 provider 返回匹配 owner 的 absent proof，或在 Job/Attempt 已失活且 grace 已过后，durable metadata 仍为禁止 provider 的 `materializing`，或为从未提交 `acquiring` 的 `materialized`，才删除 owner root；后两者成立依赖状态先 fsync、provider 调用后发生的强顺序。在进程内返回 `NotAcquired` 还必须匹配 exclusive owner token；`acquiring`、`mounted`、`release_pending` 缺 lease ID 都按 unknown 保留；
5. provider unavailable/unknown 时保留并上报，不猜测释放成功，绝不递归扫描系统 `/tmp`、workspace root 或其他 broad path。

用多 Worker 同时扫描同一 owner、advisory-lock winner crash/释放、活跃 mount、materializing 中途 SIGKILL、acquire 前后 SIGKILL、release exception、重启、P-02/P-03 labeled private container、P-04 VM、P-05 uploaded tree、实际非特权 identity 读/写探测和 ENOSPC 前置场景验证；Remote Kubernetes volume 的 orphan 由未来对应 provider 回收。

### 11.3 Remote Kubernetes

Worker path 不必然对 Kubernetes Node/Pod 可见。首期适配范围严格等于 P-01～P-05；这些 Adapter/mode 必须分别验收，P-05 支持不能外推为 Kubernetes 支持。Remote Kubernetes 继续 fail closed。若未来交付 Remote Kubernetes，materializer sink 必须演进为 provider-owned opaque `RunSkillMountArtifact`/volume lease，由 provider 实现 artifact staging、只读挂载和 orphan 回收；不能声称本机 path source 可直接复用，也不能把本地 mock 算作验收。

## 12. 删除、retention 和治理并发

### 12.1 Project Skill hard-delete

1. 保持 Project → Skill → Version 锁序；
2. 同时查询新 refs 和 legacy Run rows；
3. 任一 retained Run 引用存在都返回 `AssetInUse`；
4. quota release 使用 `skill_versions.content_size_bytes`，不再加载全部文件；
5. 不允许先删 ref 绕过引用；
6. FK 和 pin-first child trigger 是最终防线。

### 12.2 普通 Run retention

sealed Run 的 asset parent 默认不可直接删除。允许的物理路径只有：

1. 真正删除上级 Run row，由既有 Run FK cascade 删除 asset parent，再 cascade ref；
2. owning `RetentionPurgeAuthority` Module 在数据库内证明 exact scope 已 admission-closed，且无 ready/due-retry Job、有效 lease、active Attempt 或未落库的 safe terminalize/requeue 决策，再发出绑定 `resource_kind + project_id + owner_user_id? + explicit run_ids + purge_id` 的 exact authorization 后删除 parent。

Project、former-owner、account 和单 Run retention 不得共享无 scope 的 blanket bypass。transaction-local context 只是携带上述 capability 的实现细节，不能取代同一事务中的 locked eligibility/active-work 重检；refs 永远只随授权 parent/Run 删除，不能独立删除来解除 pin。

只要其他 Run 仍有 ref，Version/file 就保持不变。首期不引入 refcount 或隐式 `COUNT(ref)=0` GC。privacy purge 后保留的 terminal Run/Job/audit shell 继续 `asset_closure_sealed=true`，允许没有 asset rows，但永远不能重新追加 closure。

### 12.3 Project、former-owner 与 account purge

Project、former-owner 和 account private-scope purge 都采用两阶段状态机，不能在一次检查后直接 DELETE。Project 使用已有 `pending_deletion`；former-owner 使用 exact Membership 的 `left/removed + activation_generation + retention_until`。Account 明确采用独立 `AccountPrivateLifecycle` durable barrier，不再把 User 当作整笔事务的第一个 serialization root，也不要求全仓迁移为 User-first。

#### AccountPrivateLifecycle 状态和 Interface

这不是在否定“完整 Projects/Memberships + User 可形成事务级 serialization barrier”：在单个事务内部，该屏障成立并由下述 stable-set 测试证明。需要 durable state/generation 的原因是本方案的 purge 必须分成“提交 cancel/fence，让 Worker 收敛”与“最终物理删除”两个事务，row lock 不会跨 COMMIT 存活；两阶段之间必须有所有 scope-expanding writer 都会检查的持久 closed 状态，并让 rejoin/cancel 使旧 purge authority stale。该状态理论上也可落在另一个同等权威、同样受 Project-before-User registry 保护的单例行；本方案明确选择 User 行上的 `AccountPrivateLifecycle`，这是所选实现，不宣称是唯一可能表示。

目标 `users` 增加以下 account-private retention 字段；它们不改变登录 identity，也不授权物理删除 User：

```text
private_retention_state
  active | pending_deletion | purged

private_retention_generation BIGINT NOT NULL DEFAULT 1
private_retention_effective_at TIMESTAMPTZ NULL
```

约束固定为：generation `>= 1`；`active/purged` 时 `effective_at IS NULL`；`pending_deletion` 时 `effective_at IS NOT NULL`。新 human/guest User 默认 `active/generation=1`。进入 `pending_deletion`、取消 pending 或从 `purged` 显式重新激活时 generation 单调增加；account retention Job/candidate 必须绑定 `owner_user_id + generation + effective_at`，旧 generation 永远不能执行。Phase B 成功后置 `purged`；普通 private writer 在 pending/purged 下失败，只有 invitation/rejoin/Project-create 等明确 governance path 可以在既有 Project/Membership 锁后显式重新激活并增加 generation。单独的“取消 account private purge”事务可以只锁 User，但取得 User 后不得再回头锁 Project/Membership。未来若要物理删除 User，必须另做完整 FK closure 设计，本方案不暗示已支持。

`AccountPrivateLifecycle` 是 Membership 后的 durable guard，不是 execution authority。其小 Interface 只拥有状态、generation 和稳定集合判断，例如：

```python
class AccountPrivateLifecycle:
    async def require_active_after_membership(
        self,
        session: AsyncSession,
        owner_user_id: UUID,
    ) -> AccountPrivateGeneration: ...

    async def begin_purge_after_memberships(
        self,
        session: AsyncSession,
        candidate: AccountRetentionCandidate,
        locked_scope: LockedAccountPrivateScope,
    ) -> AccountPurgeFence: ...

    async def assert_same_purge_after_memberships(
        self,
        session: AsyncSession,
        fence: AccountPurgeFence,
        locked_scope: LockedAccountPrivateScope,
    ) -> None: ...
```

Interface 名称中的 `after_membership(s)` 是真实前置条件：普通 scope-expanding writer 按 `Project → Membership → User FOR SHARE → Thread/domain resource`；account purge 按 `sorted Projects → complete sorted Memberships → User FOR NO KEY UPDATE → domain resources/Jobs/Runs/Attempts`。`FOR KEY SHARE` 不足以阻止 lifecycle 非键字段变化；purger 使用 `FOR NO KEY UPDATE`，避免与新 FK child 的 `KEY SHARE` 制造无意义升级环。任何路径都禁止 `User → Project`。

materialization、settlement、finalization 只收敛已存在 execution，不扩大 account scope。它们不调用 active lifecycle guard，而是保持 `Project → Membership → [Thread] → Job → Run → active JobAttempt`；Phase A 通过 Project/Membership barrier 与 Job cancel/fence 阻止其发布旧结果。`RunSkillMaterializationAuthority` 只拥有 `Job → Run → active JobAttempt`，不得查询或锁 Project、Membership、User。

这个 User-free L-07 合同只有在以下 fence 全部成立时才安全：Phase A 的 `pending_deletion` transition 与 scoped Job/Run cancel/fence 必须同事务提交；claim 必须在 Job 前重验 lifecycle/generation；materialization 的初始 plan、每个 Version 边界和最终 fingerprint 都重新执行 `Project → Membership → Job → Run → Attempt`；内容 I/O 只写非权威 staging；file finalization 的每次 stage/chunk/promote 写事务都先取得相同治理前缀和 execution suffix；settlement 看到 cancel 后只可收敛 cancelled/terminal，不得 retry/requeue。provider acquire 还必须执行 Section 9.1 的事务 A/B 双 fence；取得 lease 后第二次 fence 失败时必须 release/reconcile，不得把 mount 交给 runtime。

#### Account lifecycle registry

以下 `L-01..L-09` 是唯一权威入口 registry；它登记 lifecycle policy，不代表这些路径 User-first：

| ID | 路径与合同 |
| --- | --- |
| L-01 | human/OIDC User creation、new Channel guest：同一事务初始化 lifecycle 默认值；全新 identity 尚不存在可并发 purge，不要求先锁 User |
| L-02 | Project 创建及初始 Membership：新 Project/Membership 已由本事务拥有；任何下层 child/commit 前执行 User lifecycle guard，pending/purged 时整体回滚 |
| L-03 | invitation redeem/notification accept、Membership rejoin/reactivation、existing Channel principal reuse：`Project → Membership → User`；显式恢复时增加 generation 并使旧 retention authority stale |
| L-04 | 普通聊天、Automation、Channel、Skill Builder 全部 Run Admission：`Project → Membership → User → Thread/resources → Run → Job` |
| L-05 | 所有非 Run、`owner_user_id IS NOT NULL` 的 Job Admission：至少覆盖 `memory_seal`、`memory_dream`、`memory_dream_prepare`、`mcp_discovery`，按 `Project → Membership → User → domain row → Job` |
| L-06 | 所有 `owner_user_id IS NOT NULL` 的 Job claim：候选发现可无锁；claim mutation 前按 `Project → Membership → User lifecycle/generation → 必要 domain prefix → Job → Attempt` 重验 |
| L-07 | materialization、settlement、finalization：不扩大 scope，不要求 active lifecycle；固定 `Project → Membership → [Thread] → Job → Run → Attempt`，并服从 pending purge 的 cancel/fence |
| L-08 | account Memory reset 和其他 account-wide destructive writer：`complete sorted Projects → Memberships → User → Memory/Thread/Job`；可在 pending 下继续缩减，但不得创建新 owner-private row |
| L-09 | invitation create 的 recipient notification：明确为不扩大 execution/private scope 的例外，只可写 `UserNotificationRow` 并依赖 User FK cascade；不得创建 Project/Membership/Job/Run，且必须做 notification-first/purge-first 竞态测试 |

`retention_purge` coordinator 是 L-05/L-06 的显式 typed 例外：active guard 不适用，它必须携带 exact pending generation/effective time，并且不得把自己算进被取消的 Job。setup-only User/Project bootstrap 必须由启动和源码合同证明不与 runtime purge 并发；一旦允许 runtime 调用，就归入对应 L 项。源码契约必须枚举所有 `JobRepository.enqueue()` owner-private caller；新增 Job type 未声明 lifecycle policy时测试失败。

#### Account 稳定集合算法

Phase A 和 Phase B 都使用同一个 `LockedAccountPrivateScope` 规划器：

1. 无锁预读该 User 当前完整 Project/Membership ID 集；
2. 按 Project UUID 锁 Projects，再按 `(project_id,user_id)` 锁完整 Memberships；
3. 最后锁 User：普通 guard 为 `FOR SHARE`，purge transition 为 `FOR NO KEY UPDATE`；
4. 持 User 锁重新读取完整 Project/Membership ID 集，但不再取得任何新上层锁；
5. 若集合与预读不一致，整笔事务回滚并从第 1 步重试；绝不能在 User 后补锁新 Project/Membership；
6. 集合稳定后才进入 domain resources 和 `Job → Run → active JobAttempt` suffix。

writer 先取得 User SHARE 并提交时，purger 等待后必须看到新 scope/generation；purger 先取得 User NO KEY UPDATE 时，writer 醒来看到 pending/purged 后整体回滚。尚未提交的新 Project/Membership 不需要由 purger 反序追锁：其 writer 必须在提交前经过 User guard。rejoin/cancel 若在两阶段之间发生，会增加 generation，使旧 purge Job 永久 stale。

#### 阶段 A：关闭准入并取消执行

1. Project purge 锁并复核 `pending_deletion`；former-owner 锁并复核 exact Project/Membership、activation generation 和 retention deadline；account 走上述稳定集合算法；
2. account 从 `active` 原子转为 `pending_deletion`、增加 generation并写 exact effective time；幂等重入必须匹配同一 fence；
3. 在相同治理前缀后按稳定顺序锁 domain rows、Jobs、Runs、active Attempts；排除本次 `retention_purge` coordinator；
4. queued/retry-wait Job 同事务取消，leased/running Job 写 `cancel_requested` 并 fence；claim 必须在 Job 前重验 lifecycle/generation；
5. COMMIT，让 Worker 观察取消；Retention Job 按 lease deadline 重试。Project/former-owner barrier 和 account pending lifecycle 在事务外仍保持 durable closed。

#### 阶段 B：最终物理 purge

1. 重新执行完整稳定集合算法并复核 Project lifecycle、former-owner Membership generation，或 account exact `pending_deletion + generation + effective_at`；禁止只信 RetentionCandidate 的旧 `project_ids`；
2. 再按 `Job → Run → active JobAttempt` 锁目标 execution rows并排除 coordinator；要求无 ready/due-retry、无有效 lease/active Attempt，或 lease 已 expiry 且 safe terminalize/requeue 决策已经持久化；任何 owner metadata 仍为 `acquiring|mounted|release_pending`、provider readback unknown 或尚无 matching absent proof 时都阻断物理删除；
3. 由 `RetentionPurgeAuthority` 发出只针对 exact scope/run/purge fence 的 authorization；
4. 按现有 private-scope 叶到根拓扑删除 `run_skill_secret_snapshots`、`run_mcp_secret_snapshots`、Memory/File/Artifact 等 children，再删 `run_asset_versions` parent，refs 自动 cascade；
5. account 完成后同事务置 `purged` 并清空 effective time；Project shared purge 才继续 Skill files → Versions → identity；整个最终复核和删除保持一个事务。

简化后的关键子序列是：

```text
Project/former-owner durable barrier
或 sorted Projects → Memberships → AccountPrivateLifecycle(User)
→ cancel/fence/terminalize scoped Jobs
→ 保留既有 private children 叶到根删除顺序
→ 删除 Run asset parents → refs 自动 cascade
→ 仅 Project shared purge 删除 Skill files → Versions → identity
```

该子序列不是整个 retention 拓扑的替代品。任一 ref 或 legacy parent pin 残留时，Version FK/file pin trigger 让整个 purge 回滚。Worker 即使在旧 MVCC snapshot 中读完文件，也必须在发布 mount 前通过最终 authority/fingerprint 检查。

多连接 PostgreSQL 验收必须逐项覆盖 L-01..L-09、现有 `test_account_reset_and_admission_keep_project_before_user_lock_order`、所有 owner-private enqueue/claim，以及 materialization × admission/finalization/settlement/retention/account-reset。writer-first、purge-first、新 Project 未提交、两个以上 Project 相反发现顺序、rejoin generation、direct parent DELETE、queued claim、leased expiry 和 Run cascade 都必须无 `User ↔ Project` 死锁。源码捕获还必须证明 execution authority 只查询 Job/Run/Attempt；生产代码不得定义或调用 account-first User serialization Module，也不得实现 `User → Project → Membership` 正向锁图。本文只在禁止性说明中提及这些反例。

### 12.4 System governance

- 新 Run 拒绝已撤销或不再 eligible 的 System Skill；
- 已准入 Run 继续读取 exact bytes，不重新应用 revocation；
- revocation 只改变治理字段，不改变 retained Run 的 execution payload；
- 同 identity 的 System Skill 内容不得原地替换，内容变化发布新 identity。

## 13. Worker 可用性、进程监督和真实执行状态

存储改造解决大对象压力，但不能单独保证“不会一直执行中”。以下能力作为同一交付计划的独立工作包。

### 13.1 Worker 数据库瞬时恢复

当前 dirty 补丁已覆盖 claim 前和 fleet heartbeat 的部分重试，但需先修正分类：

1. 有 SQLSTATE 时，只允许 class `08` 和 `57P01/57P02/57P03` 重试；
2. 无 SQLSTATE 时，才允许 SQLAlchemy pool timeout 或 invalidated connection 重试；
3. `28/42/23`、认证、编程、完整性、权限和不变量错误立即失败；
4. Claim 已返回、COMMIT ack 不确定或 graph 已开始后不得盲目重试领取；
5. 活跃 Job heartbeat 数据库状态不确定时保持严格 lease-loss/fail-closed 语义；
6. 退避使用有上限 jitter，可被 stop event 中断，成功后复位；
7. 日志只写稳定低敏事件码，不记录 DSN、SQL、项目/用户/Run 私有标识；
8. shutdown/cleanup 异常不得覆盖真正主错误。

claim COMMIT 已明确成功、但 handler/graph 尚未开始时，新增 exact `release_unstarted_claim()`，用于 Worker stop、fleet-loss/post-commit dispatch hook 失败等可证明的启动前窗口：

- 输入必须同时包含内部 lease token、`claim.attempt_id` 和受信 `expected_worker_id`；外层事务先按 `Project → Membership` 完成 private-work governance revalidation，进入 execution boundary 后只按 `Job → Run → active JobAttempt` 取得 authority suffix，并验证 `attempt.id == claim.attempt_id`、`attempt.worker_id == job.lease_owner_id == expected_worker_id`、Attempt outcome 仍为空；
- 只有同一 Worker 的 in-process handler-start fence 仍为 false，且 graph、Sandbox acquire 和任何外部副作用入口都未开始时才可调用；数据库 predicate 不能独自证明这一点；
- 同一事务把 Attempt 结算为既有 `retry` outcome、清空 Job lease authority，并按 policy 立即 queued/retry_wait；若 cancel 已请求则收敛 cancelled。该 release 会消费一次已创建 Attempt，不得通过回删 Attempt/递减计数伪造历史；
- release COMMIT ack 不确定时不再尝试 release/reclaim，等待 lease recovery；handler fence 已打开后也只能走正常 settlement/lease-loss 路径。

PostgreSQL 测试必须核对真实 Job/Attempt 行，而不是只断言 mock callback：成功 release、错误 token/attempt/worker、handler 已启动、cancel race、release COMMIT 不确定和 Worker stop 后另一 Worker 领取都要覆盖。

这里不承诺任意外部工具的 exactly-once，也不在本改造中新建一个没有 domain operation inventory 的通用 effect ledger。复用现有 `jobs.retry_safety` 门禁：过期 lease 只有 `safe` Job 才能进入新 Attempt；`retry_safety != 'safe'` 时，当前 Attempt 以既有 `outcome='dead'` 结束，Job 标记 dead，并写稳定 `public_error_code='SIDE_EFFECT_STATE_UNKNOWN'`，禁止自动 replay。具体 provider 已有幂等键/readback 时继续由该业务域验收；未接入的第三方工具必须在 capability 风险说明中显式标注。验收目标是“不盲目重领、不自动重放未知副作用”，不是无法证明的“绝不重复”。

### 13.2 本地与部署进程监督

- 当前本地一体化 `serve.sh` 实际无条件启动 Gateway、Worker、Scheduler、Frontend、Nginx；它在启动后续服务期间和稳态都必须把这五个实际启动角色视为 required。Scheduler 即使 Automations disabled 仍承担 Memory Dream/Seal admission，不能被当成可选角色。未来若引入 profile，必须同时定义“哪些进程根本不启动”、Memory/Automation capability owner 和契约测试，不能只把已启动进程从监督列表移除。
- 任一 required role 退出，结束其余组进程并非零退出。
- macOS `launchctl submit` 已具备 failure 后 keep-alive 语义；真实验收当前 launcher 非零退出后完整服务组重启、节流和重启风暴。若需要可配置生命周期，改用带 `KeepAlive/ThrottleInterval` 的 LaunchAgent plist。
- 新增 content-free `backend/scripts/check_local_execution_readiness.py`：只读校验 Schema ready 和新鲜、可执行 `private_run` Worker；0=ready，2=database/schema unavailable，3=worker timeout。该脚本只证明 execution readiness，不证明完整服务组 ready。`serve.sh` 在成功提示前必须先 readback Gateway、Worker、Scheduler、Frontend、Nginx 五个 required child PID 均存活，再以有界 timeout 调用该脚本；测试不能复用 `/health` 或管理员 endpoint。
- 非 macOS daemon 明确交给 systemd/Compose，不能把 `nohup` 当作长期 supervisor。
- Compose 继续按角色独立监督；完整应用 Kubernetes 部署不在仓库当前交付范围，如需支持另立 deployment 工作包。Gateway 不承担 Worker 进程管理。
- supervisor 配置重启节流，保留可由 launchd/Compose readback 验证的 exit/restart 记录，并在 runbook 定义重启风暴阈值；Section 14.2 的 exporter/collector/receiver 明确不属于本次交付。

真实浏览器 no-Worker 场景不能用 mock registry，也不能沿用当前“Gateway 启动前阻塞等待 replay Worker fresh”的 harness。测试专用 `_replay_fixture.py` 新增 `ReplayWorkerController`，默认 immediate 保持既有测试；delayed mode 只允许数据库名具有 `deerflow_test_replay_` 前缀，并且只由 `run_replay_gateway.py` 挂载的 test-only router 暴露 idempotent start/stop。`start` 必须等待真实 Worker heartbeat fresh 后才返回，`stop` 必须结束子进程并等待 registry 超出 freshness；Gateway shutdown/finally 无条件 close controller。生产 Gateway 不注册该 router，也不接受该 mode。

### 13.3 服务端 Run execution state Module

新增只读深 Module：

```text
backend/app/private_work/run_execution_state.py
```

Interface：

```python
async def read_run_execution_state(
    session: AsyncSession,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
    policy: RunExecutionStatePolicy,
) -> RunExecutionState: ...
```

Module 在单条 scoped SQL 中使用 PostgreSQL `clock_timestamp()`，与 `claim_next()` 共用同一 claimability 规则；调用方不能传 Gateway 本机时间。`policy.worker_fresh_for_seconds` 由现有 heartbeat policy 注入并固定测试，不能在各调用点硬编码。

它从权威 `runs + jobs + job_attempts + worker_nodes` 推导展示阶段，不新增第二套持久化状态机。当前 Worker 在 handler 调用前已把 Job 标为 running，而 `begin_execution()` 之后才把 Run 标为 running，因此 `Job=running / Run=pending` 是正常启动窗口，不能 fail closed。判定优先级固定为：合法 terminal pair → cancel → future retry → fresh exact Attempt/lease → stale Worker 且 lease 未到期 → lease 到期但禁止恢复 → lease 到期且允许安全恢复 → ready queue。它与 `claim_next()` 共用 status/time/capability/affinity、`retry_safety` 和 attempt-limit predicate：

| phase | 判定 |
| --- | --- |
| `queued` | `attempt_count=0`、无 recovery provenance 的首次 ready Job，且存在 exact eligible Worker |
| `waiting_for_worker` | 首次 ready、due safe retry 或可安全恢复的 expired lease 当前无 exact eligible Worker；保留 provenance，不改写成 queued |
| `starting` | 第一个 active Attempt 和 exact 新鲜 lease 已建立，但 Run 尚未由 `begin_execution()` 绑定到该 lease |
| `executing` | `Job=running, Run=running`，且 Run、Job、active Attempt 指向同一个 current lease；不因它是首次还是 recovery Attempt 改名 |
| `retry_wait` | `available_at > db_clock`，尚未到可领取时间 |
| `waiting_for_lease_expiry` | Job 仍有未过期 lease，但 lease owner Worker 已缺失/过 freshness；`retry_at=lease_expires_at`，不得提前声称可恢复 |
| `waiting_for_terminalization` | lease 已到期但 `retry_safety != safe`，或 attempt limit 已耗尽；禁止领取新 Attempt，只等待 authoritative settlement 收敛 dead/cancelled |
| `waiting_for_recovery` | lease 已到期或前一 Attempt 已以 retry/`lease_lost` 结算，Job 已 due、允许安全恢复且存在 eligible Worker，但尚无新的 active Attempt |
| `recovering` | recovery provenance 已成立，新的 active Attempt 和 exact 新鲜 lease 已建立，但 Run 尚未绑定到该 lease |
| `cancelling` | active Job/Run 已 `cancel_requested`，等待 Worker或 lease recovery terminalize |
| `terminal` | Run/Job 已终态且构成下述合法 terminal pair，不存在 live lease 或 active Attempt |

合法 terminal mapping 固定为：`Run=success ↔ Job=succeeded`、`Run=interrupted ↔ Job=cancelled`、`Run=error ↔ Job=failed|dead`、`Run=timeout ↔ Job=failed|dead`。其他 terminal 混搭、terminal row 仍带 live lease/active Attempt、或 execution identity 不一致都返回 typed unavailable，不能为了显示 terminal 隐藏损坏状态。

queued/retry-wait 同步取消直接进入 terminal；cancel_requested 必须优先于所有非终态执行 phase。`queued`、`waiting_for_worker`、`waiting_for_recovery` 和 `waiting_for_terminalization` 由 `attempt_count/prior outcome/retry_safety/attempt limit/eligible Worker` 形成互斥分支，不能按表格顺序碰巧命中。`Job=running / Run=pending` 以外的缺失、终态错配、lease/Attempt/Worker identity 错配均返回 typed unavailable，不猜测最接近 phase；数据库查询失败显示“执行状态暂不可用”，不能误报“没有 Worker”。

为区分“整个 Run 首次执行时间”和“当前 recovery Attempt 真正开始执行时间”，Schema V1 给 `JobAttempt` 增加 nullable `execution_started_at`；`begin_execution()` 只有在把 Run 绑定到 exact current lease 成功时才一次性写入。`Run.execution_started_at` 继续表示整个 Run 的首次执行时间，不在 recovery 时重置。

响应同时返回稳定 `phase_started_at`：首次 queued 使用 `Job.created_at`，future retry 使用该次 transition 的 `Job.updated_at`，starting/recovering 使用 active `JobAttempt.started_at`，executing 使用 active `JobAttempt.execution_started_at`；waiting-for-lease-expiry 在 Worker row 存在时使用 `worker_nodes.heartbeat_at + freshness`，Worker row 缺失时为 null；waiting-for-recovery 的 expired-lease 分支使用旧 `lease_expires_at`，settled due-retry 分支使用 `Job.available_at`；waiting-for-terminalization 使用使恢复变为禁止的旧 `lease_expires_at` 或最近 Attempt `finished_at`；waiting-for-worker 按其 provenance 使用首次 `Job.created_at`、due retry 的 `Job.available_at` 或旧 `lease_expires_at`；cancelling 使用 `cancel_requested_at`，terminal 使用 `Job.completed_at`。active lease 下不能把会随 heartbeat 改变的 `Job.updated_at` 当 phase start，也不能用本次 `observed_at` 或浏览器 mount time 伪造阶段起点。

### 13.4 Worker affinity

`jobs.execution_domain_affinity` 已存在，但 `worker_nodes` 没有对应字段，当前 readiness 无法证明 affinity-pinned Job 有可领取它的 Worker。完整 Schema V1 的 `CREATE TABLE worker_nodes` 最终加入以下列/约束；这不是可执行 `ALTER`：

```sql
execution_domain_affinity CHAR(64),
CONSTRAINT ck_worker_nodes_execution_domain_affinity
    CHECK (
      execution_domain_affinity IS NULL
      OR execution_domain_affinity ~ '^[0-9a-f]{64}$'
    );

CREATE INDEX ix_worker_nodes_fresh_affinity
  ON worker_nodes (execution_domain_affinity, heartbeat_at)
  WHERE draining=false;
```

- Job affinity 为 NULL：任意新鲜 `private_run` Worker 匹配；
- Job affinity 为 X：仅 affinity=X 的新鲜 `private_run` Worker 匹配；
- affinity hash 只用于服务端匹配，禁止返回前端或写日志 label。

保留 `process_readiness.py` 和 admin operations 的全局 fleet totals，新增 `private_run_worker_fleet/count/capacity` capability-scoped projection；不能把原字段悄然改义。per-Run execution state 另做 exact capability/affinity EXISTS。

### 13.5 API 与前端

新增 scoped endpoint：

```text
GET /api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/execution-state
```

响应只包含：

```json
{
  "phase": "waiting_for_worker",
  "observed_at": "2026-08-24T10:00:00Z",
  "phase_started_at": "2026-08-24T09:59:55Z",
  "execution_started_at": null,
  "retry_at": null,
  "run_status": "pending"
}
```

禁止返回 Worker ID、PID、lease token/hash、affinity hash、SQL 或内部错误正文。

前端只在当前 Run 活跃且页面可见时约每 2 秒轮询；它是展示投影，不得覆盖 SSE cursor、Thread cache、Run terminal state 或 `thread.isLoading`。文案：

- `waiting_for_worker`：等待执行 Worker
- `queued`：等待执行槽位
- `starting`：Worker 已领取，正在启动
- `executing`：执行中
- `retry_wait`：等待重试
- `waiting_for_lease_expiry`：Worker 已失联，等待租约到期
- `waiting_for_terminalization`：执行结果未知，等待安全收敛
- `waiting_for_recovery`：等待恢复执行
- `recovering`：正在恢复执行
- `cancelling`：正在停止
- 查询失败：执行状态暂不可用

响应中的 `execution_started_at` 始终来自 `Run.execution_started_at`，表示整个 Run 的首次执行时间；`phase_started_at` 在 recovering 时来自当前 `JobAttempt.started_at`，在 executing 时来自当前 `JobAttempt.execution_started_at`。UI 的 Run 总执行时长使用前者，当前阶段时长使用后者；reload 后都保持连续，字段为 null 时不显示。现有浏览器本地 page-residence timer 不得继续作为 Run 执行耗时。

新增 `frontend/src/core/threads/active-run-resolver.ts` 深 Module，作为 reload/reconnect 唯一的 active Run identity owner。它隐藏 catalog、reconnect hint、CAS 和 generation 竞态，只向 stream owner 返回 typed `none | resolved | conflict | unavailable`：

- 同一浏览器会话内，Admission `onCreated` 返回的 Run ID 是 canonical，直接建立当前 generation；
- reload/reconnect 必须强制网络读取 account/project/thread scoped 的服务端 Run catalog；恰好一个 `pending|running` Run 才是 canonical，零个返回 `none`，多个返回 `conflict`，catalog 失败返回 `unavailable`，三者都不得回退到浏览器缓存；
- `sessionStorage` reconnect Run 只是一条 hint。`resolved + same hint` 才可复用 cursor；`resolved + mismatch` 对旧值做 value-CAS clear，并从 cursor 0 attach canonical Run；
- `none` 对 exact stale hint 做 value-CAS clear，adapter 保持 null且不 attach；`conflict|unavailable` 不选择、不 attach，adapter 持续返回 null，可暂存 hint 供后续 catalog retry，但绝不把它暴露给 SDK、Stop 或 execution-state query；
- 在 catalog 解析完成且得到 `resolved` 前，reconnect adapter 的 `getItem` 必须返回 null，阻止 SDK 根据 stale hint 抢先 attach；最后一条 message projection 永远不能产生 active Run identity。

`use-thread-stream.ts` 继续作为现有 stream owner 暴露 resolver 给出的 exact `activeRunId` 和 generation。scoped chat owner 向展示层传 `account/project/thread/run`；query key 必须包含四级 scope，scope 切换时取消旧请求并忽略晚响应，MessageList 不自行猜 Run。

当前 `attachRun()` 在 `thread.isLoading` 时不会 replay，SDK `joinStream()` 也会排队等待旧 SSE，因此“projection 看到 terminal”不能只写成调用既有 reconciliation。目标在 stream owner 内新增显式 `reconcileTerminalRun(runId)`：

1. 核对当前 account/project/thread、exact `activeRunId` 和 reconciliation generation；晚到或跨 scope 响应直接丢弃；
2. 通过 project-scoped private-work reconnect owner 建立 exact `(account, project, thread, run, generation)` fence，并复用现有 value-CAS `clearReconnectRun(threadId, runId)` 清除 `lg:stream:{threadId}`；若 key 已属于新 Run，不得删除；
3. 先把 controlled `onStreamThreadId` 置为 `null`，再调用 `thread.switchThread(null)` 中止本地 SDK stream projection。仅调用后者而保持外部 controlled thread 不变会触发 SDK `reconnectOnMount`；因此两步缺一不可。**不得**调用会向服务端发送 cancel 的 `thread.stop()`，也不得排队 `joinStream()`；
4. 把 `useThreadHistory` 的 retry seam 重构为可 await、接收 explicit target Thread 的 canonical REST refetch，重读 exact Thread 的 runs、messages journal 和已有 run-control replay；只接受其中 exact Run 的 terminal 事实；
5. 由 stream owner 通过现有 archive/history bridge 合并尚未进入 history 的 live messages，确认 reconnect key 仍不是该 terminal Run 后再恢复 controlled Thread，让 SDK owner 完成本地 loading 收敛；execution-state projection 自己不直接写 `thread.isLoading`、SSE cursor、Thread cache 或 terminal state；
6. reconciliation fence/generation 使旧 SSE 晚 callback、旧 reconnect write 和跨 scope 响应不能覆盖新的 REST 结果；REST 失败显示可重试的 history/reconciliation 错误，但仍 clear exact terminal reconnect metadata，绝不转为 POST cancel。

必须单列覆盖 stale Run A 与 canonical Run B 的竞态：若 sessionStorage 仍指向已 terminal 的 A，而服务端 catalog 返回唯一 active B，resolver 在任何 attach/execution-state query 前 CAS-clear A，并从 cursor 0 attach B；之后任何 A 的 execution-state/reconciliation callback 都在第一道 Run ID/generation fence no-op，绝不能 detach、abort、clear 或覆盖 B。A 的 terminal 事实只能由与 B 隔离的 canonical REST history refetch/merge 纳入，所有 A 的 SSE/REST 晚响应都不得改写 B。

真实浏览器必须覆盖“数据库/REST 已 terminal、旧 SSE 永不结束”的场景。测试在 page load 前用 Playwright `addInitScript` 仅包装目标 Run SSE 的 `fetch`/`ReadableStream`：用增量 `TextDecoder` 按完整 SSE frame 边界缓冲，原样透传非 terminal frame/id，吞掉 terminal frame并在上游 close 后保持 wrapper stream open；暴露仅页面测试态的 release hook，在 `finally` cancel reader/close controller。生产 bundle/router 不增加故障开关。由此证明同一 Run 最终退出 loading、exact reconnect key 被清除、收敛后没有再次请求该 Run 的 `/stream`，且未发送 cancel/第二个 Run、cursor/cache 不回退、晚 SSE 不覆盖 canonical history。

无 Worker 时仍允许 durable admission 和排队，避免滚动重启期间丢失用户请求；不得自动置失败，也不得仅因 fleet=0 拒绝消息。现有 `/health` 继续表示 Gateway liveness，Project private-work readiness 继续表示 schema/功能可用性；二者都不能冒充某个 Run 的执行阶段。

## 14. 观测、资源预算和告警

### 14.1 先交付的观测信号

仓库尚无已确认的 metrics exporter。首期先扩展现有 admin aggregate 和结构化日志：

- v4 Admission：Skill 数、文件 facts 总量、manifest/ref 写入字节，不含 path/identity；
- R1 legacy Admission：artifact/policy digest、process role、gate `acquired|busy|oversize|error`、release-calibrated envelope bucket、codec、encoded bytes、Admission/event-loop latency、`content_query_entered` 和 stable outcome；不记录 Project/User/Run/Skill identity、path 或内容；
- materializer：source schema、Version 数、文件数、总字节、耗时、峰值阶段、stable outcome code；
- 应用可观察：claim DB retry、fleet heartbeat retry、lease lost、graceful process exit reason；
- queue：ready Job 数、oldest ready age、stale lease、waiting-for-worker / waiting-for-terminalization Run 数；
- 平台可观察：launchd/Compose 的 last exit code、OOM/SIGKILL 和 restart count；退出进程本身不能可靠上报这些数据。

admin operations 保留全局 Worker 聚合，并新增 capability-scoped private-run 聚合和前台可见时刷新；修改 `backend/app/reliability/operations.py`、`backend/app/reliability/models.py`、`backend/app/gateway/routers/admin_operations.py` 以及对应 frontend types/API/view。本次不接入 exporter/collector，平台信号只作为 launchd/Compose runbook 和验收项，不能假装已由应用指标完整覆盖。

不得把 project/user/run/worker ID、Skill path、错误正文作为低基数指标 label。

### 14.2 后续 exporter 与主动告警（明确不属于本次）

在明确接入 OTLP/Prometheus 后增加：

```text
actweave_worker_fresh_nodes{capability="private_run"}
actweave_worker_capacity{capability="private_run"}
actweave_jobs_ready{job_type}
actweave_job_oldest_ready_age_seconds{job_type}
actweave_jobs_stale_lease{job_type}
actweave_worker_claim_db_retries_total{sqlstate_class}
actweave_worker_graceful_exits_total{reason}
actweave_runs_waiting_for_worker
actweave_skill_materialized_bytes_total{source_schema}
actweave_skill_materialization_seconds{source_schema,outcome}
```

未来独立工作包同时具备 collector、规则执行器和 receiver/责任方后，主动告警才进入其验收；本次不等待这些外部组件。初始阈值在压测后校准，建议基线：

- 在 execution feature enabled 且 expected replicas>0 时，新鲜 `private_run` Worker=0 持续 60 秒：严重；
- 按数据库时钟计算的最老可领取 `private_run` Job >60 秒：警告，>5 分钟：严重；
- stale active lease 超过一个完整恢复窗口仍非零：严重；
- Worker 5 分钟重启超过 3 次：严重；
- DB claim retry 持续增长 5 分钟：警告；同时 Worker=0：严重；
- v4 Run 出现 byte-bearing manifest 或 `RUN_ASSET_STALE` 激增：严重。

## 15. 精确代码改造范围

### 15.1 ORM、Schema 和持久化合同

| 文件 | 改造 |
| --- | --- |
| `backend/packages/harness/deerflow/persistence/shared_assets/skill_model.py` | Version facts、`files_sealed`、exact unique、file check、C collation index |
| `backend/packages/harness/deerflow/persistence/user/model.py` | `AccountPrivateLifecycle` state/generation/effective-at 字段、约束和默认值 |
| `backend/packages/harness/deerflow/persistence/run/model.py` | `RunRow.asset_closure_sealed` 和单向 transition |
| `backend/packages/harness/deerflow/persistence/private_work/model.py` | typed schema version、`RunSkillVersionRefRow`、parent/ref/secret closure constraints/triggers |
| `backend/packages/harness/deerflow/persistence/jobs/model.py` | Worker affinity 列和索引；nullable `JobAttempt.execution_started_at` |
| `backend/packages/harness/deerflow/persistence/jobs/sql.py` | owner-private claim 候选发现后按 `Project → Membership → User lifecycle → domain row → Job` 重验；execution boundary 保持 `Job → Run → active JobAttempt` suffix；增加 exact `release_unstarted_claim` |
| `backend/packages/harness/deerflow/persistence/run/sql.py` | `RunRepository.put()` 不得直接插入未 seal Run；删除生产 fallback 或强制路由统一 Admission closure factory |
| `backend/packages/harness/deerflow/persistence/private_work/__init__.py` | 导出新 row |
| `backend/packages/harness/deerflow/persistence/models/__init__.py` | 注册新表到 metadata |
| `backend/packages/harness/deerflow/persistence/shared_assets/binding_model.py` | facts trigger、pin-first mutation、收紧 System Skill/assembly 例外 |
| `backend/packages/harness/deerflow/persistence/full_schema.sql` | 完整 Schema V1 表、列、约束、索引、函数、trigger |
| `backend/scripts/generate_schema_comments.py` | 新表/字段注释和实际表列计数 |
| `backend/packages/harness/deerflow/persistence/schema_comments.sql` | 重新生成，不手改 |
| `backend/packages/harness/deerflow/persistence/final_schema_contract.py` | catalog signature、required objects |
| `backend/packages/harness/deerflow/persistence/final_schema_digest.py` | 从 disposable PostgreSQL 重新生成 digest |
| `backend/scripts/check_postgres.py` | required table/catalog 验证 |
| 本次不新增 `backend/scripts/recreate_schema_v1_data.py` | 当前数据路径固定 recreate；历史 importer 仅保留为未来参考，不进入本次代码范围 |
| `backend/Makefile`、根 `Makefile` | 仅在交付历史导入工具时暴露显式 operator target；绝不挂入 runtime/startup |

不得提前硬编码表/列总数；最终加入 typed discriminator 和 Worker affinity 后，从实际目标 Schema 重新生成。

### 15.2 Version、Admission 和生命周期

| 文件 | 改造 |
| --- | --- |
| 新增 `backend/app/private_work/account_private_lifecycle.py` | `AccountPrivateLifecycle` 深 Module；封装状态/generation、Membership 后 active guard、purge 稳定集合算法和 L-01..L-09 source-contract registry；不拥有 execution authority |
| `backend/app/gateway/auth/repositories/sql.py`、`user_provisioning.py` | 新 human/OIDC User 原子初始化 lifecycle 默认值；后续 Project/Membership child writer 在自身治理顺序内执行 lifecycle guard，不引入 User-first 分支 |
| `backend/app/system_settings/bootstrap.py`、`backend/app/system_runtime_settings/bootstrap.py` | setup-only User principal 写入纳入 L-01/source inventory；以启动/源码合同证明不与 runtime purge 并发 |
| `backend/app/projects/repository.py` | 创建 Project/初始 Membership 后、写下层 child 或 COMMIT 前取得 User lifecycle guard；pending/purged 整体回滚 |
| `backend/app/projects/invitation_service.py`、`invitation_repository.py` | redeem/accept/rejoin 固定 `Project → Membership → User`；显式恢复增加 generation，使旧 retention authority stale |
| `backend/app/channel_group_bindings/repository.py` | existing Channel principal/Membership 复用固定 `Project → Membership → User`；新 guest 原子初始化 lifecycle；不能成为 account barrier 旁路 |
| `backend/app/projects/bootstrap.py` | 标记为 setup-only writer并禁止与 runtime purge 并发；未来若运行时调用则归入 L-02/L-03 |
| `backend/app/shared_assets/models.py` | metadata-only Skill snapshot value object |
| `backend/app/shared_assets/__init__.py` | 导出新的 metadata-only 类型 |
| `backend/app/shared_assets/skill_service.py` | Version facts 计算/写入/防御性核对 |
| `backend/app/shared_assets/bootstrap/service.py` | System Skill facts/幂等匹配；直接 User principal bootstrap 满足 L-01 setup-only/lifecycle-default 合同 |
| `backend/app/shared_assets/resolver.py` | Skill Admission 禁止读取 content |
| `backend/app/shared_assets/run_snapshot_codec.py` | per-kind codec；strict v2/v3/v4 decoder；v4 byte-field ban；大 hash/compress/Base64 使用 cancellation-joined helper |
| `backend/app/shared_assets/skill_repository.py` | 新旧引用双查、facts quota、delete 锁序 |
| `backend/app/private_work/snapshot_repository.py` | typed fact query、原子写 parent/ref、legacy 单 Skill payload loader；普通 metadata 查询不选 content |
| 新增 `backend/app/private_work/legacy_run_skill_snapshot_writer.py` | R1 唯一 byte-bearing writer；内部独占 release-fixed `LegacyAdmissionPolicy`、DB-wide fail-fast `LegacyAdmissionByteGate`、metadata upper bound 和单 Skill joined codec，不向调用方暴露锁/ceiling |
| `backend/app/private_work/run_admission.py` | 公共 Admission 固定 `Project → Membership → User lifecycle → Thread/resources → Run → Job`；不再准入后整包回读 |
| `backend/app/private_work/skill_builder_run_admission.py`、`backend/app/shared_assets/skill_builder_run_admission.py`、`backend/app/automations/dispatcher.py` | 共用公共 Admission 和 R1 writer gate；Channel/HTTP/Scheduler 不建 byte-bearing 或 lifecycle 旁路 |
| `backend/app/private_work/memory_seal_service.py`、`memory_dream_service.py`、`memory_dream_prepare_service.py`、`backend/app/shared_assets/mcp_discovery_repository.py` | L-05 owner-private 非 Run Job Admission：`Project → Membership → User lifecycle → domain row → Job` |
| `backend/app/personalization/repository.py`、`backend/packages/harness/deerflow/persistence/private_work/memory_document_repository.py` | 保留既有 sorted Project/Membership→User reset 锁序并接入 lifecycle generation；不得反转为 User-first |
| `backend/app/private_work/retention_purge.py` | account stable-set、generation fence、active-attempt interlock、ref cascade、facts quota |
| `backend/app/private_work/retention.py`、`backend/app/worker/retention.py` | direct purger 统一消费 typed `RetentionPurgeAuthority`/lifecycle outcome；治理前缀保持 Project/Membership-before-User，resource suffix 保持 Job/Run/Attempt |

### 15.3 Worker 和 Sandbox

| 文件 | 改造 |
| --- | --- |
| 新增 `backend/app/private_work/run_skill_tree_materializer.py` | 深 Module、两个 source Adapter、writer/handle、专用 root 和安全 orphan reaper |
| `backend/app/private_work/asset_runtime.py` | typed plan + materializer；不整包 decode Skill |
| `backend/app/private_work/agent_runtime_identity.py` | metadata-only Skill facts 下的 main-pool prefix/order 类型 |
| `backend/app/private_work/private_agent_runtime.py` | 持有 materialized token；metadata-only secret/MCP 查询；MCP session close 与 tree finalize 分离 |
| `backend/app/private_work/private_skill_runtime.py` | 复用或下沉安全路径/parse helper，移除重复 root ownership |
| `backend/app/private_work/execution_approval.py` | explicit typed closure facts |
| `backend/app/reliability/run_execution/handler.py` | 轻量 `PersistedRunSnapshot` |
| `backend/app/reliability/run_execution/boundary.py` | 初始/Version/final/acquire fence 都在同一短事务先完成 `Project → Membership` governance revalidation，再进入 `Job → Run → active JobAttempt` suffix；修复现有 `before_sandbox_restore()` 直接 `_check()` 而缺少上层 locked reread 的旁路 |
| `backend/app/reliability/run_execution/executor.py` | provider acquire 前事务 A、acquire/readback 后事务 B、typed release outcome，以及 MCP close/tree finalize 的 joined 清理顺序 |
| `backend/app/private_work/file_finalizer.py` | 每次 stage/chunk/promote 写事务都重新取得 `Project → Membership → Job → Run → active JobAttempt`，不得只在扫描开始前检查一次 |
| `backend/app/worker/service.py` | 瞬时 DB 分类、jitter、主异常保留和启动期 orphan reaper 调度 |
| `backend/packages/harness/deerflow/config/worker_config.py` | 每 Worker 进程的 `materialization_max_inflight_bytes`、batch bytes/files；校验 budget 至少容纳所有启用 Adapter 中最大的 release-calibrated source envelope，禁止把单进程 budget 冒充全 fleet 上限 |
| `backend/packages/harness/deerflow/config/paths.py` | Worker/host 双视图专用 materialization root；只允许从 trusted base 派生相对路径 |
| `backend/packages/harness/deerflow/file_authority.py` | 统一 `RunFileAuthority.release() -> RunMountReleaseOutcome` Protocol，禁止 `None/bool` 证据丢失 |
| `backend/packages/harness/deerflow/subagents/delegated_context.py` | owner-loop file-authority proxy 原样返回 typed release outcome，不吞掉证明 |
| `backend/packages/harness/deerflow/sandbox/sandbox_provider.py` | 定义 provider-facing `RunReadonlyMountSource`、lease、absent proof、封闭 release outcome；调用方不传 raw host path |
| `backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py` | Native source containment 和 exact release/readback |
| `backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py`、`local_backend.py` | DooD host-path 翻译、owner label、private-container enumerate/destroy/reconciliation |
| `backend/packages/harness/deerflow/community/boxlite/provider.py`、`box.py` | typed source、Run VM owner label、read-only probe、exact destroy/absent proof 和 reaper Adapter |
| `backend/packages/harness/deerflow/community/e2b_sandbox/e2b_sandbox_provider.py`、`e2b_sandbox.py` | provider-owned upload、非特权读写 probe、sandbox exact kill/absent proof；不得套用 Kubernetes host-path 结论 |
| `backend/app/private_work/sandbox_files.py` | `PrivateRunFileAuthority` 独占 provider lease；消费 typed source 并清除旁路 raw `host_path`；在事务 A fence 内持久写 `acquiring`，事务 B fence 内写 exact lease/`mounted`；实现统一 `release()` typed outcome |
| `backend/packages/harness/deerflow/runtime/runs/private_file_lifecycle.py` | 共享 cleanup 协议保留 typed release outcome，不再压成 `bool` |
| `backend/packages/harness/deerflow/runtime/runs/worker.py` | 消费 typed outcome；独立 joined 关闭 MCP，再 proof-gated finalize/handoff tree |
| `docker/docker-compose.yaml`、`docker/docker-compose-dev.yaml`、`docker/docker-compose.dood.yaml`、`config.example.yaml` | materialization root 的 Worker volume/host mapping、per-process byte budget/batch 配置和 fail-closed 合同；默认值以声明并实测的 Worker process/replica topology 固化 |

### 15.4 执行状态、监督和前端

| 文件 | 改造 |
| --- | --- |
| 新增 `backend/app/private_work/run_execution_state.py` | Run/Job/Attempt/Worker 权威展示投影；合法 terminal mapping、`waiting_for_terminalization` 和 exact current-lease 判定 |
| `backend/app/reliability/workers.py` | 注册和查询 Worker affinity/capability |
| `backend/app/reliability/process_readiness.py` | `private_run` fleet 聚合 |
| `backend/app/worker/service.py` | `_register()` 把已有 `_execution_domain_affinity` 传给 registry |
| `backend/app/gateway/routers/private_work.py` | scoped execution-state endpoint |
| 新增 `backend/scripts/check_local_execution_readiness.py` | DB/schema/private-run Worker content-free execution-readiness CLI，不冒充完整服务 ready |
| `backend/scripts/run_replay_gateway.py`、`backend/tests/_replay_fixture.py`、`backend/tests/replay_agent_router.py` | 仅 disposable replay DB 可用的 delayed-start `ReplayWorkerController`；真实浏览器可先观察 no Worker，再显式 start/stop，shutdown 必回收子进程 |
| `scripts/serve.sh` | 启动期/运行期完整子进程监督 |
| 新增 `frontend/src/core/threads/run-execution-state.ts` | schema、API、query ownership |
| 新增 `frontend/src/core/threads/active-run-resolver.ts` | Admission/catalog/reconnect hint 的唯一 identity seam；强制 catalog、typed none/conflict/unavailable、stale hint CAS 和 generation fence |
| `frontend/src/core/threads/use-thread-stream.ts`、`frontend/src/core/threads/use-thread-history.ts` | 只暴露 resolver 的 exact active Run ID；实现无服务端 cancel 的 local stream abort + awaitable canonical REST terminal reconciliation；旧 Run 不得影响新 Run |
| `frontend/src/core/private-work/api-client.ts`、`frontend/src/core/api/api-client.ts` | project-scoped reconnect fence；resolver 完成前 `getItem=null`；CAS-clear exact stale/terminal Run metadata，保护新 Run key 和 scope generation |
| `frontend/src/components/workspace/messages/run-duration.tsx` | 使用服务端 phase 文案 |
| `frontend/src/components/workspace/messages/message-list.tsx` 及 scoped chat owner | 活跃可见时轮询，不接管 SSE/Thread state，也不从 message projection 推断 Run identity |
| `frontend/src/core/i18n/locales/types.ts`、`zh-CN.ts`、`en-US.ts` | 阶段文案 |
| `backend/app/reliability/operations.py`、`backend/app/reliability/models.py`、`backend/app/gateway/routers/admin_operations.py` | 保留全局 totals 并增加 private-run capability 聚合 |
| `frontend/src/core/admin-operations/types.ts`、`api.ts`、`components/admin/operations/operations-overview.tsx` | 严格解析、前台刷新和 capability 展示 |
| `frontend/playwright.real-backend.config.ts` | 读取 delayed Worker test mode；独立端口管理真实 lifecycle；默认 `reuseExistingServer=false`，仅显式 `E2E_REUSE_EXISTING_SERVER=1` 可复用 |

### 15.5 领域与运维文档

- 实施时更新 `CONTEXT.md`：领域确定性语义保持，但物理 self-contained bytes 合同被显式替换为“manifest + FK/pin-protected exact immutable Version bytes”；不得称为透明实现变化。
- 实施时更新 `backend/AGENTS.md`：明确 supersede 的条款，把“完全自包含 bytes / Worker decode-only”改为“不可变 referential Run closure；Worker 可读取 exact pinned Version，永不读取 Current”。
- 新增 ADR `docs/adr/0008-pin-run-skills-by-immutable-version-reference.md`，将 D-01 记为 accepted，并记录已拒绝替代方案和 rollback 语义；写 ADR 是实施记录，不是新的暂停或批准点。
- 本次固定走 recreate，不生成历史 importer 或 `docs/schema-v1-recreate-import-matrix.md`；若未来另起历史保留工作包，再由该工作包拥有完整矩阵。
- 更新 `README.md`、`Install.md` 或已有部署/排障文档中的 Schema recreate、Worker readiness、cutover 和 rollback 说明。
- 保持 ADR-0002 的领域语义和 ADR-0007 的 System Skill identity 不变量。

## 16. 分阶段实施与发布

每个工作包先写失败测试，再实现最小通过逻辑，最后重构；不在同一个巨大提交中同时切 writer 和删除 legacy reader。

### Phase 0：基线与止血复核

1. 保存只读基线：v2/v3 Run 数、代表性 JSONB 大小、`skill_version_files` 大小、单文件最大值及 64–100 MiB 分布、WAL、PostgreSQL/Gateway/Worker/Scheduler RSS、Worker fleet、oldest queued age；同时记录 `GATEWAY_WORKERS`、Gateway DB pool、Gateway/Scheduler process/replica 数和目标 Worker process/replica topology，禁止只用 `max_concurrent_jobs=8` 推断 Admission 并发。
2. 将 Section 0 已关闭的 D-01 和 recreate 选择写入 ADR-0008、`CONTEXT.md` 与 `backend/AGENTS.md`；记录完成后直接进入 Phase 1，不等待再次确认。
3. 冻结 Section 12.3 的 L-01..L-09 registry，枚举所有扩大 account-private scope 的 writer、owner-private enqueue/claim、purger 和明确例外；先加入源码失败测试与现有 Project-before-User 多连接回归，任何未声明 lifecycle policy 的入口都阻断 Phase 1。
4. 独立复核当前 dirty v3、Worker retry 和 `serve.sh` 补丁；修复永久 SQLSTATE 被误判、jitter、unstarted claim 和启动期监督缺口。
5. containment v3 同时改 reader/writer，不能直接发布后再回退基线二进制。先构建 R0a=v2/v3 dual-reader 候选；legacy writer 在选择 content 前接入 DB-wide fail-fast `LegacyAdmissionByteGate` 和 metadata envelope，已知超限大 Skill 新 Admission 暂停。真实 1 GiB PostgreSQL 上同时触发 8 个多来源 Admission attempt：至少两个 Gateway process/replica 发起，并与至少一个独立 Scheduler trigger 同时运行。必须证明最多一个事务执行 content SELECT/detoast，其余在锁忙时 rollback并按各入口返回 retryable 结果，且被接受的单个 v3 writer 的 Gateway/PostgreSQL RSS、WAL、latency 和 recovery 都通过，才形成 R0b v3 候选。Phase 0 不部署新 reader/writer；R0a/R0b 必须在 Phase 1 `AccountPrivateLifecycle` 关闭后并入 R1 才可发布。这里的 8 是 backpressure 场景，不是允许 8 个 70 MiB writer 同时运行。
6. v3 候选即使通过也仍每 Run 重复约 70 MiB，只能标为 temporary containment；未通过时继续暂停受影响 Admission或等待受控 v4，不得把 v2 当作默认安全回退。
7. 验证目标库没有 >64 MiB file row；本次基线已确认计数为 0，且 recreate 会丢弃现有开发数据。若验证结果变化，保持旧库不变并继续其他工作，禁止自动 split、静默丢弃或改写 immutable Version。

退出门：ADR-0008 已记录 accepted 决策并明确 supersede 的物理合同；L-01..L-09 入口清单封闭；claim 前短暂故障可恢复，unstarted claim 可精确释放，claim/authority 不确定不会盲目重领，required 子进程退出可恢复；R1 v3 只有在取得“8 个多来源 Admission attempt + 最多一个 byte-bearing writer”的跨进程资源证据后才可启用，否则保持大 Skill Admission 拒绝并继续 Phase 1/2。no-Worker UI 仍由 Phase 4 交付。

### Phase 1：Account lifecycle、Schema 与 Version facts

1. 先让 L-01..L-09 源码合同和多连接失败测试红灯，再在目标 Schema V1 与 app 层实现 `AccountPrivateLifecycle`；证明普通 writer 固定 `Project → Membership → User FOR SHARE → domain resource`，purger 固定 sorted Projects/complete Memberships→User stable-set，execution authority 仍只拥有 `Job → Run → active JobAttempt` suffix，生产路径不得出现 User-first 锁图。
2. 新增 Schema contract/PostgreSQL 失败测试。
3. 修改 ORM、完整 Schema V1、comments、catalog signature/digest 和 `check_postgres`。
4. 修改两个 Version 创建入口并验证 facts/checksum。
5. 加入 Version/Run closure seal、exact ref、immediate parent/ref/secret mutation gate、deferred final-state verifier、pin-first trigger、Worker affinity；禁止 `RunRepository.put()` direct unsealed fallback。
6. 在 disposable PostgreSQL 从空库运行完整 `setup-db/check-db`。

退出门：Account lifecycle generation/stable-set 与 L-01..L-09 多连接证明成立，Project-before-User 无交叉死锁且 pending/purged writer 无法提交；错误 Project/scope/version/checksum/facts、缺 ref、order 空洞、post-seal INSERT/UPDATE/DELETE、未 seal Run COMMIT、direct parent/ref/secret delete 和所有 maintenance bypass 均被数据库拒绝。

### Phase 2：Reader-first、Worker metadata-only 与 Admission 分流

1. 新增 strict v2/v3/v4 reader 和 `RunSkillTreeMaterializer` 测试。
2. 先把 `PersistedRunSnapshot` 和所有 Worker/运行时 metadata-only 调用点移除 `snapshot_json`；R1 的 `LegacyRunSkillSnapshotWriter` 是唯一 byte-bearing Admission allowlist，不能宣称 Gateway 已 metadata-only。
3. 固化 legacy 一次一个 Skill 的 Adapter，并用 SQL capture 证明 R1 只有 `LegacyRunSkillSnapshotWriter` 能选择 content；release-fixed `LegacyAdmissionPolicy`、DB-wide gate、metadata upper bound 和 joined codec 必须保持封装在该 writer 内，所有 Gateway/Scheduler writer role readback 同一 artifact/policy digest。
4. 实现 v4 metadata-first bytes+rows Adapter、per-Worker-process weighted byte budget、增量 checksum和 pending/runtime ownership。在本次固定的单 Worker process 注册并 readback `max_concurrent_jobs=8` 后做 8 并发 1 GiB 资源验收；更多 Worker process/replica 未经相同聚合 RSS/DB 压力复测时 readiness fail closed。
5. typed Sandbox lifecycle 作为独立工作包实现 source/lease/proof/reaper，再与 materializer 集成；P-01～P-05 分别验收，跨 Worker reaper 使用 owner advisory lock。
6. R1 Writer 固定使用 v3，但只有 reader-first、DB-wide gate、尺寸和资源门全部通过时才对相应大小启用；否则自动保持 Admission 拒绝并继续 v4 实施。使用导入 fixture 人工构造 v4 行验证 reader，v2 不作为写入回退。

退出门：生产代码整实体 RunAsset 查询只剩明确允许点；R1 Admission byte-bearing 查询只有一处 allowlist，8 个跨 Gateway/Scheduler 的并发 attempt 下最多一个 content SELECT；v2/v3/v4 replay、取消通过；固定单 Worker process、capacity=8 下的并发 materialization budget 证据成立；一个最大 R1 Admission writer 与允许的 legacy materialization 负载共存仍通过 1 GiB 资源门；P-01～P-05 的 Adapter 合同完成，release 启用的 provider/mode 均取得真实证据，其他 provider 保持 v4 fail closed。

### Phase 3：显式 Schema cutover

目标数据库仍标记 `schema_v1`，但 catalog digest 已改变，旧数据库会正确返回 `SCHEMA_RECREATE_REQUIRED`。

本次执行路径固定为下述“可丢弃开发数据”分支。后面的历史 Run importer 说明仅保留为未来工作包参考，执行者不得在本次任务中切换到该分支或为此暂停询问。

#### 可丢弃开发数据

1. 备份需要保留的非数据库工件；
2. 显式 recreate 空数据库；
3. `make setup-db`；
4. `make check-db`；
5. `make setup-db` 已包含 System assets、system models/runtime policies、LangGraph 和 default Project bootstrap，不再重复调用 bootstrap/`upgrade-system-assets`；
6. 部署 dual-reader release R1；通过资源门的范围写 v3，其他大 Skill Admission 保持拒绝。

#### 未选择的未来参考：必须保留历史 Run

该分支不属于本次执行。未来若建立历史保留工作包，它必须独立交付和演练，需要 coordinated downtime 或等价 blue/green freeze，并交付 operator-only `recreate_schema_v1_data.py`。工具提供 `plan`（只读盘点、冲突和 >64 MiB 阻断报告）、`import`（按 Version 和 Run closure 独立事务流式复制）和 `verify`（源/目标计数、facts、逐文件 hash、closure/ref、secret ownership、quota 对账）子命令。Source/target DSN 只从受控配置或环境读取，禁止写入命令历史/日志；resume checkpoint 只记录非敏感 batch/object identity 和 checksum，使用 owner-only 权限。目标中已存在且 sealed、identity/facts/checksum 完全一致的对象可幂等跳过；部分或不一致对象立即停止，不能 upsert 覆盖 immutable 数据。`pg_dump` 只作为灾难备份，不能替代新增 NOT NULL facts/seal/ref 的结构转换。

##### 导入闭包和逐表决策矩阵

“保留历史 Run”默认表示保留其可见历史、审计、文件/产物、后续 Thread 连续性，以及当前产品仍支持的 replay/resume/regenerate 行为，不只是复制 `skill_versions` 和 `run_asset_versions`。importer 的 `plan` 必须从冻结源库和 setup 后目标库的 `pg_catalog`/FK catalog 生成版本化 `schema-v1-recreate-import-matrix`；每个 application table、partition 和 LangGraph table 都必须恰有一条决策，存在未分类表就 fail closed。

矩阵每行至少包含：

| 字段 | 含义 |
| --- | --- |
| `schema.table` / domain owner | application、LangGraph、partition 或 sequence 的精确对象和负责人 |
| authority class | `copy`、`transform`、`rebuild`、`target-bootstrap` 或 `drop-transient` |
| selection closure | 从哪些 User/Project/Thread/Run/Version 根沿哪些 FK/owner scope 纳入 |
| column transform | 新 NOT NULL facts/seal/ref、状态归一化、旧/新列映射；未声明列禁止默认补值 |
| conflict policy | preserve ID、exact digest reuse 或 fail；禁止任意 remap immutable identity |
| dependency/SCC/import phase | parent→child 顺序、循环依赖的单事务/两阶段策略 |
| verify rule | selected count、按 PK 排序摘要、FK、内容 hash、序列/partition high-water |

必须覆盖的处置类别：

1. **身份与准入根**：Users、Projects、Memberships、Threads 和配置/权限 authority 保留 source identity；scope 不完整时整条选择闭包停止，不能留下孤儿 Run。旧源库 User 只有在冻结点不存在未决 account purge/cancel/rejoin authority 时才能 transform 为 `AccountPrivateLifecycle(active, generation=1)`；存在未决候选必须在源侧先完成/取消，或由矩阵给出 exact generation/effective-at 转换，未知状态 fail closed。
2. **共享资产图**：Agent/Skill/MCP identity、Version、files、bindings、Current/Candidate/Activation、catalog/revocation 和 secret-free declarations 按父到子导入；Skill Version 用本方案的 per-Version transaction 形成 facts/seal。
3. **Run/Job 图**：Runs、Run asset parents/refs、Skill/MCP secret snapshots、Jobs/Attempts、durable events/sequence/cursor、execution approvals 和 audit 一起分类。terminal rows 原样保留；safe requeue 必须先把旧 Attempt 结算为 `lease_lost` 并清空旧 lease authority，unsafe 未知执行先收敛为 dead + `SIDE_EFFECT_STATE_UNKNOWN`，不能复制 live token。
4. **私有数据图**：Memory、File、Artifact、attachment、workspace-change、delivery/receipt 和其他 Run/Thread children 按 owner scope/retention 一起选择；加密 consumer bytes 原样复制并沿用已授权 key material，不解密到报告。
5. **LangGraph 图**：目标先由 `_bootstrap_langgraph_schemas` 创建第三方表；`checkpoint_migrations` 与 `store_migrations` 属于目标 bootstrap 元数据，禁止从源库复制。仅将纳入 Thread/Run 选择闭包的 checkpoint、blob、write 数据按其 namespace/thread/checkpoint 闭包导入并保持 checkpoint lineage。未保留 checkpoint 的 Run 不得宣称支持 resume/replay。
6. **Worker registry 与历史 Attempt**：live Worker capability/lease authority、live HTTP/SSE connection、原始 lease token 和临时 staging 不复制；但 `job_attempts.worker_id` 是 `NOT NULL + ON DELETE RESTRICT` FK，不能在保留 Attempts 时丢掉全部 `worker_nodes`。对 retained Attempts 引用的每个 distinct source Worker ID，先 transform 为同 UUID 的 inert historical tombstone：`version='history-tombstone'`、`capabilities_json=[]`、`max_concurrent_jobs=1`、`draining=true`、`execution_domain_affinity=NULL`、`started_at=source.started_at`，`heartbeat_at=min(source.heartbeat_at, frozen_at - freshness - safety_margin)`。缺失/naive 时间或目标 UUID 已存在且不是完全相同的 resume tombstone都 fail closed。Jobs/Runs 的 live lease owner/token/hash 清空；冻结时仍 active 的 Attempt 必须先按 safe/unsafe 规则终态化。

   `job_attempts.lease_token_hash` 自身是 `NOT NULL` 历史字段：retained terminal Attempt 保留 source hash 以维持审计摘要，但它不再是 authority；claim/fence 永远要求 active Job/Run lease hash、Worker identity 和空 outcome 同时匹配。importer 必须验证历史 hash 单独不能授权任何操作。导入顺序至少为 tombstones → Jobs → JobAttempts → Attempt-dependent rows。未被 retained Attempt 引用的 registry row 不导入；quota/storage/admin aggregate 重建；durable Run events/cursors 不是瞬态，必须保留并校验 monotonic high-water。
7. **Sequence/partition**：setup 创建目标 partition/sequence；导入数据后按目标表最大值安全推进 sequence，逐 partition 对账，不复制源 catalog OID/owner。

FK 导入顺序由目标 catalog 拓扑排序生成；强连通分量只能在约束可 defer 时同事务导入，否则必须在矩阵中有字段级两阶段策略。禁止用 `session_replication_role=replica`、全局 disable trigger 或事后手工补 FK 绕过目标完整性。

verify 必须证明每个 retained Attempt 恰有一个 Worker FK 目标、Worker/Attempt 所有 NOT NULL 字段有效、所有 tombstone 都因 `draining=true` 且 heartbeat stale 而不可能被 claim/readiness 视为 eligible、历史 Attempt hash 不能重新授权，并且没有伪装成 live registry 的无引用 source Worker。未来工作包若明确选择丢弃 JobAttempts，可以不创建 tombstone，但这会失去 Attempt 历史/诊断能力，必须从“完整历史”范围中明确剔除，不能作为 importer 的隐式简化。

bootstrap 冲突规则固定为：System identity/runtime policy/model 等 target-bootstrap 对象只有 stable identity 和 semantic digest 完全一致时才复用；不同即停止。setup 产生的 default Project 若与 source 同 ID/语义则复用；若只是无用户数据的 target seed，可由 importer 的显式 operator transaction 在确认零非 bootstrap 引用后移除；任何非空或同 ID 异义冲突都 fail，不重映射 source UUID。最终 `verify` 要求矩阵覆盖率 100%、每个 `copy/transform` 表 selected source/target count 与稳定摘要一致、全部 FK 有效、sequence/partition high-water 正确，并抽样执行 retained v2/v3 Run 的 history/replay/resume/regenerate。

如果未来工作包只导出“部分 Run 展示记录”，必须另写允许丢弃的 domain/table 清单；这种降级结果不得表述为“完整保留历史 Run”。

默认推荐较简单、可证明的一致性停写流程：

1. 关闭 Gateway Admission/配置写入和 Automation admission，停止 Scheduler；
2. Worker drain；active Job 必须 terminal，或按 retry-safety 明确 safe requeue/unknown terminal 决策，不能把旧 lease/worker registry 原样导入；
3. 停止全部旧 writer/claimer，取得一致性数据库快照；旧二进制禁止连接新 Schema；
4. 在新库运行 `make setup-db`，生成 source/target 全 catalog 矩阵并解决 bootstrap conflict；存在未分类表、FK cycle 无策略、>64 MiB gate 或 identity/digest 冲突即停止；
5. 按矩阵导入身份/Project/Thread 和共享资产父图；每个 Skill Version 在一个事务内导入 parent+files+facts+seal，逐内容 SHA 和 aggregate checksum 校验，分批数据先落非权威 staging；
6. 在导入前执行 Phase 0 的硬门禁：仍需保留任一 >64 MiB 单文件时该未来 importer fail closed，不创建另一份不同 digest 的“临时 Schema V1”；只有“不受任何 retained/governance 引用”的证明及该工作包明确记录的 retention policy 才能排除对象。工具不得自动 split、截断、重算成另一 Version 或跳过；
7. 按矩阵先导入历史 Worker tombstones，再导入 Run/Job/Attempt/private/audit/LangGraph 图；v2/v3 Skill parent 无 ref、closure sealed，v4 必须 parent/ref 成对；privacy-purged terminal shell sealed 且允许无 assets；live lease/staging 按明确 transform/drop 规则处理；
8. 校验全局 dependency order、Project/scope、secret snapshot ownership、durable event cursor、checkpoint lineage、sequence/partition high-water；沿用同一受控 secret root/key material，否则加密 consumer data 无法解密；
9. 从 Version facts 和实际保留对象重建/核对 quota/storage/admin counters，不直接信任旧 derived counters；运行矩阵 100% coverage、逐表 count/digest/FK 和 retained Run 行为验证；
10. 全量校验后原子切换 Gateway/Worker/Scheduler 的 DSN，旧库转只读并保留回滚窗口。

这不是 runtime migration，不得在线手工 patch/stamp 原库。切换期间任何一侧重新开放写入都要重新取得一致性快照。

### Phase 4：执行状态 UI 和运维闭环

1. 在新 Schema 上交付 Worker affinity、execution-state endpoint 和前端投影；
2. unit/PostgreSQL 覆盖完整 Run×Job×lease×Worker/Attempt 关系；浏览器验证 admission 后首帧前 reload、terminal A hint/active B catalog 隔离、no Worker → Worker 出现，以及 safe `waiting_for_lease_expiry → waiting_for_recovery → recovering → executing → terminal` 与 unsafe/exhausted `waiting_for_lease_expiry → waiting_for_terminalization → terminal` 两条序列。允许跳过毫秒级 `starting`，如需观察它只在测试 Worker 注入 barrier；
3. 扩展 admin aggregate、结构化日志、launchd/Compose readback 和告警 runbook；主动告警属于 Section 14.2 的条件后续交付；
4. 验证 launchd/Compose 目标环境的真实监督行为；完整应用 Kubernetes 不在本工作包范围。

这部分可与 Phase 2/3 并行开发，但必须在 v4 writer switch 前完成用户可见验收。

退出门：catalog `none|conflict|unavailable` 全部 fail closed且 SDK 不 attach；stale A hint/active B canonical 完全隔离；safe recovery 与 unsafe/exhausted terminalization 两条状态序列、合法 terminal mapping、terminal reconciliation 不发 cancel/第二个 Run、真实 no-Worker 和目标 supervisor readback 均通过。

### Phase 5：v4 writer switch

1. 由制品 inventory、源码合同和测试证明所有实际 Run Snapshot reader（Gateway/Worker）支持 v4；Scheduler 只需兼容新 Schema；
2. 由 Phase 4 的测试和目标 supervisor readback 证明执行状态及运维闭环已经验收；
3. 由集成测试证明 Version Reference 与 typed Sandbox lifecycle 的 P-01～P-05 Adapter 合同完整；本次固定单 Worker process、capacity=8，并 readback byte budget/批次配置。未取得真实环境证据的 provider/mode 保持 v4 fail closed，未复测的多进程/多 replica 拓扑保持 readiness fail closed；
4. 同一 R2 release 把 Gateway Skill Admission 切为 metadata-only 并切换新 Run Skill writer 为 v4；删除/禁用 legacy Admission content loader，不改变 Agent/MCP writer；
5. canary 后观察 manifest 大小、WAL、Worker/PostgreSQL RSS、budget wait、Admission/materialization latency 和 stale error；
6. 扩至全量。

回滚：

- R2 只能回滚到预先构建、仍支持 v4 reader，且保留同一 homogeneous `LegacyAdmissionPolicy` digest、DB-wide fail-fast `LegacyAdmissionByteGate`、envelope/encoded ceiling 和入口失败映射的唯一 R1 制品；任何缺少该 policy/gate 的 legacy writer 都不得启用，超出已验收尺寸时回滚模式冻结新 Skill Run Admission；
- 不能回滚到只认识 v2/v3 的旧二进制；
- 回到旧 Schema 是有数据损失风险的 disaster rollback，不是普通回滚；本次固定采用 fix-forward：冻结新 Skill Run Admission、保留新库和证据、修复仍支持 v4 reader 的制品。执行者不得自动恢复旧 Schema 或静默丢弃切换后写入的 RPO；
- 不允许通过 drop ref 表或改写 v4 rows 实现“快速回滚”。

### Phase 6：legacy 退役和可选优化

- 当数据库查询证明 retention 内 v2/v3 Run 为零，且 R2 soak 与 R1 rollback rehearsal 证据已落盘后，自动删除旧 reader/partial index；不再等待额外确认。
- 只有压测证明 PostgreSQL Version 读取是瓶颈时，才在 materializer Interface 后增加 checksum 命名、原子写、可驱逐的本地缓存；缓存永不成为权威。
- Remote Kubernetes Sandbox 仅在 opaque provider-owned artifact/volume Interface 和真实 Pod 验收完成后宣告支持。

## 17. 测试与验收矩阵

### 17.1 Unit/contract

- v2/v3/v4 strict decode matrix 和非法 ref/schema 组合。
- v4 禁止所有 byte-bearing/unknown 字段，严格验证 256 KiB、identity、facts 和 Version secret declarations。
- Unicode path 增量 checksum 与现有 checksum 完全相同。
- path traversal、NFC/casefold、前缀冲突、media type、单文件和总大小。
- metadata-first batch planner 同时满足 bytes/rows 边界；singleton 大文件、0-byte 文件、Unicode path、batch 边界缺失/额外/乱序均 fail closed。回归 fixture 必须模拟 dialect 一次 refill 50 行，证明 safety 来自整批总字节而不是 `yield_per`。
- `MaterializationMemoryBudget` 对 v4 facts weight 与 v2/v3 codec envelope 的 reservation、等待取消、异常归还、多个 Skill 顺序释放和 process aggregate 上限；调用方看不到 batch/prefetch/codec-weight 旋钮。
- base metadata data classes/query 不含 `snapshot_json/content`；只有三类 explicit-column JSONB loader 在 allowlist：Agent/MCP 小 payload、v4 小 manifest、legacy Adapter 的单个大 Skill。
- `LegacyRunSkillSnapshotWriter` 内部的 DB-wide transaction advisory gate 使用固定 namespace/key 和 `pg_try_advisory_xact_lock`；锁忙在 content SELECT 前返回 retryable busy，事务结束/断连自动释放，调用方不能绕过或改成等待锁。
- `LegacyAdmissionPolicy` 对单 Skill source/codec envelope、多 Skill conservative encoded upper bound 的 near/at/over-ceiling 都固定结果；over-ceiling content query=0、permit attempt=0。Gateway/Scheduler 只能使用同一 release policy digest，mixed/missing digest 禁用 writer。
- R1 入口映射一致：HTTP/Skill Builder busy=`503 + Retry-After: 1`；Scheduler 保持 due/retryable 且不写 terminal occurrence；Channel 不绑定 delivery/Run；oversize 在所有入口都先返回永久 `PRIVATE_WORK_TOO_LARGE`，且不尝试 permit。
- `AccountPrivateLifecycle` 的 L-01..L-09 source registry、Project-before-User 顺序、SHARE/NO KEY UPDATE 模式、generation fence 和 stable-set reread 均有源码合同；execution authority Interface 不导入 lifecycle/User。
- materializer 成功、失败、cancel、concurrent/idempotent close，以及 AsyncExitStack 转移前/后的唯一 owner 清理；adopt 写入前异常不改变 owner slot，写入后路径不可抛错，禁止双 owner。
- hash/write/parse/rename/rmtree 全部使用 cancellation-joined helper；取消返回前没有后台线程继续写 owner root。
- main-pool Skill 顺序/runtime-name、全 closure path/name 冲突与现有语义一致。
- provider-facing DTO/outcome 不反向依赖 `app.*`；共享 `RunFileAuthority.release()`、`PrivateRunFileAuthority`、`PrivateFileLifecycle`、Worker 全链保留 `NotAcquired/Released(absent proof)/Orphaned`，不得压成 `None/bool` 或使用旁路方法名；重复 release 不丢 proof，Orphaned 只可单调升级，MCP close 失败也必走 tree finalize。
- lifecycle 覆盖在 `materializing` 中途、`materialized`、`acquiring` 持久化后/provider 调用前、acquire 返回/lease ID 回写前、`mounted`、release/readback unknown 和 `release_pending` 每一点 SIGKILL；多个 Worker 同时扫描同一 owner 时只有 advisory-lock winner 可 enumerate/destroy/delete，unlock/failure 后可接管；orphan reaper 只扫描专用 root，只有 durable never-acquired 或 absent proof 才删。
- owner root/metadata/staging/tree 的 `0700/0600/0555/0444` mode 精确；P-01 的本机执行 identity 和 P-02～P-05 的实际非特权 runtime identity 可读受控 manifest、不能写只读 tree/`/mnt/skills`，对应 path mapping/upload/readback 不兼容时 fail closed。
- transient SQLSTATE 分类、永久错误 fail-fast、jitter/复位/stop。
- supervisor 在启动期和运行期发现子进程退出。
- execution-state phase 优先级、合法 terminal mapping、`waiting_for_terminalization`、exact current-lease identity、`JobAttempt.execution_started_at`、数据库异常 fail closed、`retry_safety`/attempt-limit 门禁，以及 dead Attempt/Job 的 `public_error_code='SIDE_EFFECT_STATE_UNKNOWN'` 禁止自动 replay。
- `ActiveRunResolver` 只接受 Admission `onCreated` 或强制服务端 catalog 作为 canonical identity；reconnect storage 仅为 hint。覆盖 same/mismatch、`none + stale hint` CAS-clear、conflict/unavailable 后 adapter 持续 null、CAS 时 key 已变不误删、旧 scope/generation 晚响应丢弃；message projection 永不产生 Run ID。

### 17.2 Schema/PostgreSQL

建议新增：

- `backend/tests/test_run_skill_version_ref_schema_contract.py`
- `backend/tests/test_run_skill_version_refs_postgres.py`
- `backend/tests/test_legacy_run_skill_snapshot_writer_postgres.py`
- `backend/tests/test_run_skill_materializer_postgres.py`
- `backend/tests/test_run_skill_mount_lease.py`
- `backend/tests/test_run_execution_state_postgres.py`
- `backend/tests/test_process_readiness_postgres.py`
- `backend/tests/test_account_private_lifecycle_postgres.py`
- `backend/tests/test_run_unstarted_claim_postgres.py`
- `backend/tests/test_aio_run_skill_mount_lease_dood.py`
- `backend/tests/test_boxlite_run_skill_mount_lease.py`
- `backend/tests/test_e2b_run_skill_mount_lease.py`
- 本次不新增 `backend/tests/test_recreate_schema_v1_data_postgres.py`；历史 importer 不在当前代码范围

必须覆盖：

1. ORM/full-schema/catalog/comment parity；
2. Project/System ref 正确写入；
3. 跨 Project、错误 scope/Run/Skill/Version/checksum/facts 全部拒绝；
4. v4 parent 缺 ref、v2/v3 parent 多 ref、非 Skill parent 带 ref、ref UPDATE/独立 DELETE 拒绝；
5. 未 seal Run 不能 COMMIT；immediate gate 覆盖两连接 seal/child INSERT 的两种提交序、同事务 `child→seal` 成功和 `seal→child` 立即失败；seal 后 parent/ref/Skill secret/MCP secret 的 INSERT/UPDATE/直接 DELETE 全部立即拒绝；
6. `dependency_order` 必须从 0 连续无空洞；privacy-purged terminal shell sealed 且无 closure 时合法，但不能重新追加；
7. Version 未 seal 或数据库 `count/sum` facts 不匹配不能 COMMIT；files seal 后 INSERT/DELETE 和 post-commit assembly GUC 复用均失败；repository 聚合 checksum 不匹配使同一创建事务回滚，真实 content hash 漂移由 Worker 重算后 fail closed；
8. pinned Version 在 `system_asset_upgrade`、Skill file assembly、hard-delete、Project purge 下仍不可改；v4 ref 和 exact legacy v2/v3 parent 都能 pin 文件；Agent/MCP child assembly 保持既有合同；
9. direct sealed parent DELETE 拒绝；上级 Run cascade 和受约束 retention 才能正确 parent→ref cascade；
10. R1 legacy 与 R2 v4 分别覆盖 Admission/hard-delete 两种提交顺序；两条相反 Skill 输入顺序的 Admission 不死锁；
11. 至少三条连接验证 R1 gate：holder 取得 permit 后，其他 Project/Gateway/Scheduler 事务的 `pg_try` 立即返回 false，content query=0 且无 Run/Job/parent/quota/audit；holder COMMIT、ROLLBACK、request cancel 或物理连接关闭后，下一事务可取得同一 permit。源码合同禁止 blocking `pg_advisory_xact_lock`、session-scoped advisory lock 和调用方进程 semaphore；
12. Run/Job/ref/secret snapshot/quota/audit 任一点故障都整体回滚；
13. Current Version 变化后旧 Run 仍读取 exact Version；
14. System revocation：新 Admission 拒绝，旧 Run bytes 不变；
15. Skill/MCP secret snapshot 的 scope/version/Generation closure 在 seal 时匹配，post-seal mutation 失败；
16. Project、former-owner、account purge 都覆盖 ready/due claim、active Attempt、lease expiry 和 parent/ref 删除；L-01..L-09（含 Channel、Project create、rejoin、所有 owner-private enqueue/claim 和 notification exception）逐项覆盖。普通 writer 固定 `Project → Membership → User FOR SHARE`，account purge 固定 sorted Projects→complete Memberships→User FOR NO KEY UPDATE，并在 User 锁后重读稳定集合；writer-first/purge-first、新 Project 未提交、相反 Project 发现顺序和 rejoin generation 均无 `User↔Project` 死锁。保留并扩展既有 `test_memory_reset_postgres.py` 的 Project-before-User 回归；
17. materializer 初始控制、每个 Version 边界和最终 fingerprint 事务先由外层按 `Project → Membership` revalidate，再由 execution authority 只按 `Job → Run → active JobAttempt` 加锁；file finalization 的每次 stage/chunk/promote 同样重验。provider acquire 前事务 A 在 fence 内写 `acquiring`，acquire/readback 后事务 B 在 fence 内写 exact lease/`mounted`；B 遇 cancel/lease loss 时 mount 只可 release/reconcile。materialization/settlement/finalization 不取得 User lifecycle lock；与 retention/admission 并发无死锁，Attempt/Worker identity 中途变化会永久阻止发布旧 tree/mount；
18. Project/former-owner retention 使用其治理前缀后进入 `Job → Run → active JobAttempt` resource suffix；account retention 使用 stable-set/generation fence并排除协调中的 retention Job。owner-private claim 候选发现后在 Job 前按 `Project → Membership → User lifecycle → domain row` 重验，不能越过 barrier，也不得把 User 插入 Job/Run/Attempt authority suffix；
19. `release_unstarted_claim` 以 lease token+attempt ID+expected Worker guard 真实结算 Attempt/requeue Job；错误 identity、handler 已开始、cancel 和 COMMIT unknown 均 fail closed；
20. execution state 使用 DB clock，phase predicate 两两互斥，覆盖合法/非法 terminal pair、`Job=running/Run=pending`、首次 ready、future/due retry、fresh lease+stale/missing Worker、unsafe/exhausted expired lease 的 `waiting_for_terminalization`、safe expired lease、settled retry、waiting/recovering、current Attempt `execution_started_at`、cancel、exact capability/affinity、无 Worker、DB unavailable 和 scope 隔离；route-level 测试覆盖严格 schema、404/503、错误 scope/owner/capability 和内部字段不泄漏；
21. execution readiness 对 schema unavailable、Worker timeout、fresh eligible Worker 返回稳定退出码；`serve.sh` 另行 readback 五个 required child，二者不混为一项证明；
22. `RunRepository.put()` 不能提交未 seal 的生产 Run，所有生产创建路径都进入统一 Admission closure；
23. 源码、Makefile、README 和本次测试清单均不暴露历史 importer 命令、脚本或隐式分支；当前 cutover 只能走显式 recreate，未来 importer 必须另立工作包。

需要同步更新的既有测试至少包括：

- `test_agent_runtime_checksum.py`
- `test_configuration_secret_retention_postgres.py`：同一 Run 的 Skill/MCP fixture 改为连续 `dependency_order` 和合法 typed v2/v3 JSON
- `test_asset_runtime_modules.py`
- `test_run_asset_facts.py`
- `test_execution_approval_lifecycle_postgres.py`
- `test_skill_service_lifecycle.py`
- System Skill bootstrap/revocation tests
- Skill secret lifecycle/retention tests
- `test_worker_service.py`
- `test_run_worker_private_file_lifecycle.py`
- `test_subagent_delegated_context.py`
- `test_run_agent_outcome.py`
- `test_run_worker_rollback.py`
- `test_run_worker_host_execution_pause.py`
- `test_skill_builder_provider_execution.py`
- `test_aio_private_sandbox_lifecycle.py`
- `test_aio_local_container_backend.py`
- BoxLite private provider lifecycle tests
- E2B private provider lifecycle tests（mock contract + 有凭据时的真实 VM probe）
- `test_paths_user_isolation.py`
- `test_sandbox_tools_security.py`
- `test_serve_daemon_contract.py`
- `test_schema_comments_contract.py`
- `test_setup_postgres.py`
- `test_check_postgres.py`

所有直接实现或代理 `RunFileAuthority.release()` 的 fixture/proxy 都必须返回封闭 typed outcome，至少覆盖 run worker rollback/host-execution pause/agent outcome、Skill Builder provider execution 和 delegated subagent owner-loop；不能让旧的 `release() -> None` test double 掩盖生产证据链。

Schema Phase 还必须审计所有直接构造 `SkillVersionRow` 的 fixture，至少包括 `test_agent_runtime_checksum.py`、`test_configuration_secret_retention_postgres.py`、`test_run_asset_facts.py`、`test_skill_builder_revision_postgres.py`、`test_skill_runtime_name_conflicts.py`、`test_skill_runtime_name_conflicts_postgres.py`、`test_skill_secret_lifecycle_postgres.py`、`test_system_skill_version_revocation.py`，补齐 facts/files seal；所有直接构造 `RunRow` 的 fixture 必须显式给出合法 seal 状态或改走统一 factory，不能靠 server default 掩盖测试意图。

### 17.3 Worker/性能

用真实 12,922 文件、约 79 MiB 的 `ppt-master` Version：

1. R1 固定验证 v3，在 1 GiB PostgreSQL 上同时触发 8 个多来源 Admission attempt，其中至少两个 Gateway process/replica 与至少一个独立 Scheduler trigger 并发；断言 DB-wide gate 使最多一个事务进入 content SELECT/detoast，其余按入口得到 retryable busy，并测这个被接受 writer 重复约 70 MiB/Run 时的 Gateway/PostgreSQL RSS、WAL、latency 和 recovery；任一项不通过则大 Skill Admission gate 自动拒绝，同时继续推进 R2；
2. R2 同一 Version 连续创建 100 个 Run；
3. `skill_version_files` 行数和逻辑内容字节不增长；每个 Run 只增加小 manifest/ref，JSONB 不含 Base64/压缩帧；
4. WAL 和 `run_asset_versions` TOAST 增量不再与 79 MiB 成正比；
5. R1 SQL capture 只允许 `LegacyRunSkillSnapshotWriter` select content；R2 Gateway Admission 不 select `content`，不发送 50–100 MiB JSONB 参数；所有 metadata SELECT 不隐式 detoast；
6. 在单 Worker 进程注册并 readback `worker_nodes.max_concurrent_jobs=8`，同时启动 8 个 v2/v3/v4 混合 materialization；证明该进程 `sum(active source weights) <= materialization_max_inflight_bytes`、v4 单次 content query 不超过 batch/singleton bound，legacy 在 detoast 前已 reservation，budget wait/cancel/finally release 正确；
7. 使用当前 SQLAlchemy asyncpg Adapter 的 regression probe 观察底层 `fetch(50)`，证明即使外层一次消费一行，query 全结果 content bytes 仍被 batch plan 限制；
8. 记录固定单 Worker process、capacity=8 和 PostgreSQL baseline/peak RSS、driver/write/parser 余量；v4 峰值必须落在该 release topology envelope 内且不随 8 个完整 closure 无界线性增长。多进程/多 replica 未实测时 readiness fail closed，不得由单进程结果外推；
9. legacy 执行一次最多保留一个 Skill；`EXPLAIN (ANALYZE, BUFFERS)` 使用 Version/path C index，无携带 `BYTEA` 的大 Sort；
10. 用 64 MiB `SKILL.md` 验证 parser working set、事件循环响应和 Stop；取消返回后没有后台 file-op；
11. 多 Skill main-pool 顺序、runtime name 和路径布局与 legacy 结果一致；
12. SIGKILL/OOM fixture 留下的专用 owner root、P-02/P-03 container、P-04 VM 和 P-05 VM/artifact 能由 advisory-lock winner reaper 回收；active/grace/readback-unknown root 不会误删；
13. 1 GiB PostgreSQL 容器分别在“8 个多来源 Admission attempt、最多一个 R1 heavy writer”和“目标 Worker topology 的 8 并发 materialization”中不发生 backend OOM 或 crash recovery；两项证据不得混写。
14. R1 共存门：在完整受支持 Gateway/Scheduler/Worker topology 下，同时运行一个已取得 permit 的最大 legacy Admission writer 与 release 允许的 legacy materialization 负载；分别归因各进程和 PostgreSQL 的 RSS/WAL/latency，并要求无 OOM/recovery。该场景补充而不合并前两项证明。
15. 多 Skill R1 fixture 分别落在累计 encoded ceiling 的 near/at/over 边界；upper-bound over 在 permit/content 前拒绝，near/at 一次只保留一个 Skill working set，最终实际 encoded bytes 仍不超过 ceiling。所有 Gateway/Scheduler writer role readback 同一 artifact/policy digest，缺失或 mixed digest 时不接 Admission。

### 17.4 故障注入和浏览器

1. 空闲 Worker 时让 PostgreSQL recovery 0.5–5 秒，原 Worker 进程恢复领取；
2. 活跃 Job 断库，handler 严格丢失 lease；`retry_safety='safe'` 才允许新 Attempt，unsafe Job 以 Attempt/Job dead + `public_error_code='SIDE_EFFECT_STATE_UNKNOWN'` 终止且不自动 replay，不声称任意第三方 exactly-once；
3. kill 本地 Worker，验证 launcher/launchd 的真实退出和重启；
4. kill Compose Worker，验证 restart policy、新注册和旧 registry 行过期；
5. no Worker 时发送消息，随后在首个 Worker frame 前 reload；resolver 强制读取服务端 catalog，exact Run ID 不从 reconnect hint/message 猜测，UI 显示“等待执行 Worker”，Stop 可用。`resolved+same` 才复用 cursor，`resolved+mismatch` CAS-clear并从 cursor 0 attach；`none+stale hint` CAS-clear且无 attach，multiple/error 分别 conflict/unavailable并保持 adapter null，任何情况都不让 stale hint 抢先进入 SDK；
6. Worker 恢复后显示稳定可观察的启动/执行阶段并到达合法 terminal pair，不制造第二个 Run；覆盖 `Job=running/Run=pending` starting、fresh lease+stale Worker 的等待到期、unsafe 或 attempts exhausted 的 `waiting_for_terminalization`、safe expired lease 的等待恢复、新 Attempt 建立但 Run 未绑定时 recovering，以及绑定同一 current lease 后统一显示 executing；
7. 执行状态查询失败显示“暂不可用”，不误报 no Worker；
8. 真实 v2/v3/v4 Run 的执行、retry、resume/replay；
9. P-01 的本机执行 identity 和 P-02～P-05 的实际非特权 runtime identity 可读取 `0555/0444` tree、不能写只读 tree/`/mnt/skills`；P-03 source 来自 trusted 双视图翻译，P-05 使用 provider upload，均不是 caller raw path；
10. `NotAcquired/Released(absent proof)/Orphaned` 在 file authority、shared lifecycle、Worker、tree finalizer 间不丢类型；release exception/readback unknown 写 `release_pending` 并保留 root，provider exact destroy + absent readback 后才删除；lease loss/purge race 不把 staging 交给 Sandbox；
11. query key 包含 account/project/thread/run；切换 scope 会取消旧请求并忽略晚响应；hidden tab 停止轮询；
12. 用 `addInitScript` stream transform 稳定吞 terminal frame并保持旧 SSE open；`reconcileTerminalRun()` 先 CAS-clear exact reconnect key，再 controlled detach + local abort，不发 POST cancel；await canonical REST 后由 owner 退出 loading，断言不再请求该 Run `/stream`，晚 SSE 不覆盖 cursor/cache/history，finally 释放 fault stream；另覆盖 sessionStorage 指向 terminal A、catalog 指向 active B：解析前不 attach/query A，CAS-clear A、从 cursor 0 attach B，A 的 reconciliation/晚响应不能 detach、abort、clear 或覆盖 B；
13. `cancelling`、future/due retry、`waiting_for_lease_expiry`、`waiting_for_terminalization`、`waiting_for_recovery`、`recovering`、无 eligible affinity Worker 的文案与 Stop 行为正确；duration 使用服务端 `JobAttempt.execution_started_at` 等稳定时间，reload 不重置；
14. admin operations 保留全局 fleet totals，新增 private-run capability aggregate，并仅在前台可见时刷新；
15. `check_local_execution_readiness.py` 与真实 launchd/Compose/五进程 PID readback 分开验收，前者不冒充 supervisor 证据；
16. N 个可见页面以 2 秒轮询时测 route p95、索引命中、DB CPU/QPS；hidden tab 停止，429/503/网络错误采用有上限退避，不能形成同步重试风暴。

默认 mocked Playwright 使用独立非 `3000` 端口和匹配 `PLAYWRIGHT_BASE_URL`，不复用未知现有服务。Worker availability 验收必须走 `playwright.real-backend.config.ts`，该 config 使用 `E2E_FRONTEND_PORT/E2E_GATEWAY_PORT`，不读取 `PLAYWRIGHT_BASE_URL`；不得误用默认 config。

建议新增的前端聚焦测试至少包括：

- `frontend/tests/unit/core/threads/run-execution-state.test.ts`
- `frontend/tests/unit/core/threads/active-run-resolver.test.ts`
- `frontend/tests/unit/core/threads/run-terminal-reconciliation.test.tsx`
- `frontend/tests/unit/components/workspace/messages/run-execution-state.test.tsx`
- `frontend/tests/unit/components/projects/private-work/project-chat-run-execution-state.test.tsx`
- `frontend/tests/unit/core/admin-operations.test.ts`
- `frontend/tests/unit/components/admin/operations-overview.test.tsx`
- `frontend/tests/unit/playwright-real-backend-config.test.ts`
- `frontend/tests/e2e-real-backend/run-worker-availability.spec.ts`

### 17.5 建议门禁命令

实施时先运行聚焦测试，再运行完整门禁：

```bash
cd backend
uv run pytest \
  tests/test_run_skill_version_ref_schema_contract.py \
  tests/test_run_skill_version_refs_postgres.py \
  tests/test_run_skill_materializer_postgres.py \
  tests/test_run_skill_mount_lease.py \
  tests/test_run_execution_state_postgres.py \
  tests/test_process_readiness_postgres.py \
  tests/test_account_private_lifecycle_postgres.py \
  tests/test_memory_reset_postgres.py \
  tests/test_legacy_run_skill_snapshot_writer_postgres.py \
  tests/test_run_unstarted_claim_postgres.py \
  tests/test_aio_private_sandbox_lifecycle.py \
  tests/test_aio_local_container_backend.py \
  tests/test_aio_run_skill_mount_lease_dood.py \
  tests/test_boxlite_run_skill_mount_lease.py \
  tests/test_e2b_run_skill_mount_lease.py \
  tests/test_worker_service.py \
  tests/test_serve_daemon_contract.py -q
make format
make lint
make detect-blocking-io
make test
```

```bash
cd frontend
pnpm exec rstest run \
  tests/unit/core/threads/run-execution-state.test.ts \
  tests/unit/core/threads/active-run-resolver.test.ts \
  tests/unit/core/threads/run-terminal-reconciliation.test.tsx \
  tests/unit/components/workspace/messages/run-execution-state.test.tsx \
  tests/unit/components/projects/private-work/project-chat-run-execution-state.test.tsx \
  tests/unit/core/admin-operations.test.ts \
  tests/unit/components/admin/operations-overview.test.tsx \
  tests/unit/playwright-real-backend-config.test.ts
pnpm check
pnpm test
```

真实 no-Worker → Worker fresh → terminal 浏览器门禁使用 real-backend config 和非默认端口。实现该 config 的 task-local database lifecycle：从当前非生产 development `DATABASE_URL` 派生 maintenance connection，自动创建随机 `deerflow_test_replay_*` 空库，bootstrap 后注入 Gateway/Worker，测试结束后清理并保存脱敏 readback；不得要求用户手工提供 `ACTWEAVE_E2E_DATABASE_URL`，也不得连接端口 3000 上的未知服务：

```bash
cd frontend
ACT_WEAVE_REPLAY_BOOTSTRAP_SCHEMA=1 \
E2E_REPLAY_WORKER_MODE=delayed \
E2E_FRONTEND_PORT=3317 \
E2E_GATEWAY_PORT=8117 \
pnpm exec playwright test \
  --config playwright.real-backend.config.ts \
  tests/e2e-real-backend/run-worker-availability.spec.ts
```

该 config 负责 Gateway/Worker/Frontend 子进程 lifecycle，默认禁止复用已监听端口并在 3317/8117 被占用时 fail fast；只有人工调试显式设置 `E2E_REUSE_EXISTING_SERVER=1` 才可复用。spec 在 `finally` 释放 SSE fault stream并调用 test-only Worker stop，Gateway controller 的 shutdown 再兜底回收。测试后按仓库 disposable test-database 流程清理该库并保存进程/数据库 readback；这里不使用 `PLAYWRIGHT_SKIP_WEB_SERVER`，也不连接端口 `3000` 上的未知服务。

历史数据保留分支不属于本次，不运行或新增 `tests/test_recreate_schema_v1_data_postgres.py`，也不要求任何额外 DSN。

Schema V1 在 disposable/目标数据库分别执行：

```bash
cd backend
uv run python scripts/generate_schema_comments.py --check
make check-db
```

P-01～P-05 都注册 `provider_integration` 及独立 case marker。准备在 release 配置中启用某个 provider/mode 时，必须在其真实目标能力环境以 `ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION=1` 运行；缺少 dependency、Docker/Apple Container、KVM、凭据或 provider readback 必须失败而不是 skip，并使该 provider 的 v4 readiness 保持 fail closed。未启用 provider 仍运行 contract test，但不把 contract test冒充真实 probe。每个配置文件都只面向空、可丢弃的测试 Project/目录/VM；配置只引用 Secret 环境变量，命令和日志不得打印 Secret 值。

P-01/P-02 使用 native Worker 进程；P-03 的专用测试负责从 host 启动真实 `docker-compose-dev.yaml + docker-compose.dood.yaml` Worker、验证 Worker/daemon 双视图并在 `finally` 回收 Compose 和 sandbox，不得在测试中把本机直连 Docker 冒充 DooD：

```bash
cd backend
ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION=1 \
ACT_WEAVE_CONFIG_PATH="${ACTWEAVE_LOCAL_TEST_CONFIG:?set a disposable Native Local test config}" \
uv run pytest tests/test_run_skill_mount_lease.py \
  -m 'provider_integration and p01_native_local' -q

ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION=1 \
ACT_WEAVE_CONFIG_PATH="${ACTWEAVE_AIO_NATIVE_TEST_CONFIG:?set a disposable Native AIO test config}" \
uv run pytest \
  tests/test_aio_private_sandbox_lifecycle.py \
  tests/test_aio_local_container_backend.py \
  -m 'provider_integration and p02_native_aio' -q

ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION=1 \
ACT_WEAVE_CONFIG_PATH="${ACTWEAVE_COMPOSE_DOOD_TEST_CONFIG:?set a disposable Compose DooD test config}" \
uv run pytest tests/test_aio_run_skill_mount_lease_dood.py \
  -m 'provider_integration and p03_compose_dood' -q
```

P-04/P-05 分别在受支持 Linux/KVM 主机和受控 E2B 测试账号上运行：

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

每项 probe 都要保存无 Secret 的配置 digest、provider identity、read/write/release/readback 和 cleanup 结果，并映射回 P-01～P-05；Remote Kubernetes 不运行这些本地 probe，也不计入本期完成。

静态、unit 或 disposable PostgreSQL 测试不能替代真实 1 GiB PostgreSQL、Worker kill、launchd/Compose、浏览器或任何 release-enabled provider/mode 的真实验收。

## 18. 风险和控制措施

| 风险 | 控制 |
| --- | --- |
| 把 Version Ref 当作不改变合同的存储优化 | D-01 已固定接受引用闭包；ADR-0008、`CONTEXT.md` 和 `backend/AGENTS.md` 明确 supersede self-contained bytes |
| facts 成为可漂移缓存 | Version 创建双重核对、parent deferred trigger、child immutability、Worker 重算 |
| 只改 materializer 仍整包加载 legacy | 重塑 `PersistedRunSnapshot`，清除所有 metadata-only entity query，源码契约测试 |
| ref 只单向 FK，v4 parent 缺 ref或 post-seal mutation | immediate mutation gate + deferred final pairing verifier + Worker fail closed |
| maintenance GUC 绕过 Run pin | pin 检查置于所有例外之前，并做每个 GUC 的 PostgreSQL 测试 |
| legacy v2/v3 无 ref FK，删除检查漏读 | file child trigger 同查 exact legacy parent；R1/R2 分别做 Admission/delete 并发测试 |
| Project purge 与物化竞态 | active-attempt interlock、REPEATABLE READ、最终 authority/fingerprint 复核 |
| account lifecycle 只在 purge 一侧实现或反转成 User-first | L-01..L-09 全量源码合同；普通 writer `Project→Membership→User SHARE`、purger stable-set/NO KEY UPDATE、generation fence 和多连接证明；execution authority 不取得 User |
| pending/cancel 与 provider mount 发布之间出现 TOCTOU | Phase A lifecycle+cancel 同事务；initial/Version/final/file-promote 多重 fence；provider acquire 前事务 A 写 `acquiring`、readback 后事务 B 重验并写 `mounted`，失败只 release/reconcile |
| SQLAlchemy 外层逐行掩盖 asyncpg `fetch(50)` | v4 使用 metadata-first bytes+rows 查询批次 + per-process weighted byte budget；声明的 Worker process/replica topology、capacity=8、1 GiB真实验收 |
| 外部副作用 ACK 不确定 | 复用 `retry_safety`；unsafe 过期 lease 进入 dead + `public_error_code='SIDE_EFFECT_STATE_UNKNOWN'`，禁止自动 replay；provider 幂等/readback 按域验收 |
| v3 被误认定为安全终态或 v2 被当默认回退 | v3 只在 DB-wide fail-fast gate 下临时启用；8 个多来源 attempt 覆盖至少两个 Gateway process 与独立 Scheduler，且最多一个 heavy writer；单写资源验收通过后仍只允许已验收尺寸，超限冻结新 Admission |
| v3/v4 writer 回滚不兼容 | reader-first；只能回滚到仍读 v4 的预构建 R1，超限 Admission 冻结；旧 Schema 依赖备份恢复 |
| 误入历史 importer 分支拖慢本次 recreate | Section 0/Phase 3 固定 recreate；importer 是未来非目标，不生成脚本或矩阵 |
| 保留 Attempts 却丢弃 `worker_nodes` FK 目标 | distinct referenced Worker ID 转 inert tombstone；draining+stale 永不 eligible，逐 Attempt FK verify |
| 全局 dependency order unique 拒绝历史脏数据 | 离线导入前验证和报告，不在 runtime 自动修复 |
| 多 Worker 同时恢复惊群 | 有上限 jitter、成功复位、结构化 retry 信号；主动告警待 exporter/receiver 接入 |
| UI 形成第二状态机或 stale reconnect hint 抢占 canonical Run | 服务端只读 projection；`ActiveRunResolver` 强制 catalog、hint 仅匹配复用、解析前 `getItem=null`；前端不覆盖 SSE/Thread/Run authority |
| Worker affinity 泄露 | 只存 hash；不进入响应、日志正文或指标 label |
| Compose Docker daemon 看不到 Worker path | 专用 mapped root、provider 派生双视图 source、opaque lease/readback；禁止 caller raw host path |
| materialization 中途 SIGKILL 留下无主 staging | owner root 创建后先持久写 `materializing`；失活+grace 后可证明从未调用 provider 并安全回收 |
| host/guest UID 不同导致 Skill 不可读 | protected owner root + `0555/0444` tree；以实际非特权 guest 做读/写 readback，不兼容配置 fail closed |
| release/SIGKILL 后 mount 仍活跃 | `acquiring` 先持久化、owner label + provider enumerate/destroy；readback unknown 保留 root，lease-aware reaper 不猜测 |
| 多 Worker reaper 同时 destroy/delete | owner-ID session advisory lock；仅 winner 操作，provider destroy/readback 幂等，unknown 保留 |
| P-04/P-05 因只设计本地 path 被遗漏 | typed lifecycle 基类分别适配 BoxLite VM mount 和 E2B provider upload；未完成则 v4 fail closed，不伪称 P-01～P-05 已完成 |
| UI 把合法启动窗口、禁止恢复或新旧 Run 竞态误报 | 完整 Run×Job×lease×Worker/Attempt 关系；`Job running/Run pending`=starting，unsafe/exhausted=`waiting_for_terminalization`，A/B Run 用 ID+generation fence 隔离 |
| Remote Pod 看不到 host path | provider-owned volume Adapter；未验证前继续 fail closed |
| 缓存变成第二权威 | 首期无缓存；未来缓存可删除、checksum 命名、Interface 内部、miss 回源 Version |

## 19. 完成定义

只有同时满足以下条件，才能把改造标记为完成：

### 存储

- ADR-0008 已将 Section 0 的 accepted D-01 记录为正式决策，并明确 supersede self-contained bytes 的物理条款。
- 同一 79 MiB Version 创建 100 个 Run，Version 内容只写一次；每个 Run 不再产生 70–107 MiB JSONB。
- v4 manifest 不含任何文件 bytes、Base64 或压缩帧。
- WAL/TOAST 的 per-Run 增量与 Skill 内容大小解耦。

### 确定性和数据完整性

- Current/Candidate/Activation 改变不影响已准入 Run。
- Run/ref/Version 的 exact FK、双向 pairing、Project/scope 和 pin-first immutability 全部由 PostgreSQL 测试证明。
- immediate mutation gate 与 deferred final verifier 分责通过 seal/child 两连接竞态；`RunRepository.put()` 等 direct path 不能绕过 closure。
- `AccountPrivateLifecycle` durable barrier 覆盖 L-01..L-09；Project-before-User stable-set、generation fence、普通 writer/purger/claim 多连接竞态均通过，且 execution authority 源码证明不查询 User。
- Phase A lifecycle transition 与 scoped cancel/fence 同事务；materialization/settlement/finalization 的 Project/Membership→Job/Run/Attempt 多点 fence、file promote 每次重验和 provider acquire 前后事务 A/B 均通过竞态测试，cancel/lease loss 后没有 tree/mount 被发布。
- v2/v3/v4 非法组合、文件/hash/checksum/facts 漂移均永久 fail closed。

### Worker 和内存

- R2 Gateway 和 Worker metadata 路径不加载 Skill JSONB/content；R1 legacy Admission 查询只存在于明确 allowlist 和已验收尺寸范围。
- R1 的 homogeneous artifact/policy digest、单 Skill source/codec ceiling 和多 Skill累计 encoded ceiling 均 readback 一致；over-ceiling 在 permit/content 前永久失败。至少两个 Gateway process/replica 与独立 Scheduler trigger 形成的 8 个并发 Admission attempt 下最多一个 byte-bearing writer，busy 在 content SELECT 前重试失败。单写、目标 legacy Worker materialization、以及两者在完整 topology 下的共存峰值分别通过 1 GiB RSS/WAL/latency/recovery 门禁。
- v4 在已声明 Worker process/replica topology 下逐进程 readback capacity/budget；单进程 capacity=8 的 bytes+rows batch 和 weighted budget 使 active reservation 不超过 release 值，完整 topology 的 1 GiB实测 RSS 含 driver/write/parser 余量且无 OOM/recovery；未实测的多进程/多 replica 不计入支持范围。
- 成功、失败、cancel、lease loss 和 mount failure 都沿 typed outcome 收敛：durable never-acquired 或 provider absent proof 后无临时树泄漏；`acquiring/mounted/release_pending` 的 readback unknown 安全保留并由 reaper 后续回收，不误删活跃 mount。
- P-01～P-05 的 typed lifecycle Adapter 合同全部通过；每个 release-enabled provider/mode 还通过实际执行 identity 的只读 probe 和 cross-Worker advisory-lock reaper 验收，其他 provider 明确保持 v4 fail closed。

### 可用性

- claim 前短暂 DB 故障可恢复；已确认 commit 且 handler 未开始的 claim 可 exact release，claim/release commit 不确定不会盲目重领；unsafe 过期 lease 以 dead + `public_error_code='SIDE_EFFECT_STATE_UNKNOWN'` 收敛且不自动 replay。
- 必需子进程退出能被目标 supervisor 发现并恢复。
- admission 后首帧前 reload 仍由强制服务端 catalog 取得 exact Run ID；stale A hint 不会抢占 active B，`none` 清除 stale hint，`conflict|unavailable` 保持 SDK adapter null且不 attach。no Worker、等待 lease 到期、等待安全终结、等待恢复、新 recovery Attempt、exact current-lease executing 和合法 terminal pair 均准确，总执行/当前阶段 duration 不因 reload 重置。

### 安全和运维

- `/mnt/skills` 仍为 Run 专属只读 mount，Secret 不进入新存储链。
- Schema V1 recreate、reader-first、writer switch、R1 rollback 和灾难 fix-forward 均完成演练并留存结果；本次不交付历史 importer。
- execution-readiness CLI 与五进程/supervisor readback 分别通过，不互相冒充。
- 所有聚焦测试、完整 backend/frontend 门禁、真实 PostgreSQL、真实浏览器、P-01～P-05 Adapter 合同及每个 release-enabled provider/mode 的真实 probe 均有当次证据；其他 provider 与 Remote Kubernetes 明确保持 v4 fail closed。

## 20. 最终推荐实施顺序

1. 将 Section 0 已固定的 D-01 accepted 决策写入 ADR-0008、`CONTEXT.md` 和 `backend/AGENTS.md`；数据路径固定为 recreate，不进入 importer 分支，也不等待批准。
2. Phase 0 保存 Gateway/Scheduler/Worker/数据库拓扑基线，冻结 L-01..L-09，修复 Worker retry/unstarted claim/supervisor；v3 containment 候选先接 DB-wide fail-fast gate，并用至少两个 Gateway process/replica 与独立 Scheduler 形成的 8 个并发 Admission attempt 证明最多一个 heavy writer。Phase 0 不部署候选，未通过则继续冻结受影响大 Skill Admission。
3. 在目标 Schema V1 实施 `AccountPrivateLifecycle`，用 Project-before-User stable-set/generation 多连接证明关闭 account purge 门；再实施 Version facts/seal、Run immediate/deferred closure、typed discriminator、exact ref、Worker affinity 和 pin/retention authority。
4. 并行交付两个独立工作包：Version Reference materializer（metadata-first batch + byte budget）和 typed Sandbox lifecycle（P-01～P-05）；在 v4 前完成集成。
5. 显式 recreate 空库并部署 homogeneous dual-reader R1；R1 的 v3 writer 只在 policy/gate、单写、legacy Worker 和共存资源门全部通过的范围内工作，否则相应 Admission 自动拒绝。
6. 在新 Schema 上完成 execution-state、exact Run identity、前端投影、admin aggregate 和 supervisor 运维闭环。
7. 所有实际 reader/provider和已声明 Worker process/replica topology 的 capacity=8 资源门通过后，同一 R2 切换 metadata-only Admission 与“小 manifest + exact Version ref”；真实大 Skill、1 GiB PostgreSQL、故障注入、ActiveRunResolver A/B 竞态和浏览器 terminal-state 是发布门禁。
8. retention 内 legacy 归零后再删除旧 reader；缓存和 Remote Kubernetes 只按实测与明确交付范围追加。

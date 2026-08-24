# Skill Run Snapshot 故障分析与优化改造建议

> 报告日期：2026-08-24
> 证据截止时间：2026-08-24 17:11 CST
> 代码基线：`7c3802a8`；文中的 v3、韧性和监督修改指随本报告交付的 containment 变更
> 范围：已脱敏的受控会话、本机 PostgreSQL/Worker/Scheduler/启动器、Run Snapshot 与 Skill 沙箱挂载链路
> 本报告只分析和提出方案，不代表目标架构已经实施或当前运行环境已经恢复。

## 1. 结论摘要

这次“消息一直执行中”不是数据库长期不可用，也不是模型一直生成，更不是 Skill 没有挂载进沙箱。完整故障链是：

1. Run Admission 把 18 个 Skill 的完整文件内容复制进本次 Run 的 `run_asset_versions.snapshot_json`；其中 `ppt-master` 单行 JSONB 达到 **107,220,204 字节（102.25 MiB）**。
2. PostgreSQL 容器只有 **1 GiB** 内存。2026-08-24 15:30:35，容器发生 OOM，内核杀死了正在执行 `INSERT INTO run_asset_versions ... snapshot_json JSONB` 的 PostgreSQL backend 进程。
3. PostgreSQL 自动恢复只用了约 **0.488 秒**，随后重新可用。Worker 恰好在这个窗口领取 Job，收到 `CannotConnectNowError`。
4. 当时的 Worker 没有把这类“领取 Job 前的瞬时数据库恢复错误”作为可重试错误，反而进入退出流程；退出清理再次遇到同一错误，Worker 进程终止。Scheduler 也因锁连接丢失而退出。
5. `scripts/serve.sh` 的父进程、Gateway、Frontend、Nginx 仍然存活，启动器没有发现 Worker/Scheduler 子进程已经退出，因此没有触发整组重启。
6. Gateway 继续接收消息并创建 Job，但没有 Worker 领取，`attempt_count=0`。前端把这个状态持续展示为“执行中”，形成无限等待的用户体验。

因此，直接原因和设计原因要分开：

- **本次直接触发**：超大 Run Skill JSONB 写入使 1 GiB PostgreSQL 容器发生 OOM。
- **会话持续卡住的直接原因**：Worker 在 PostgreSQL 短暂恢复期间退出，并且没有被守护进程拉起。
- **根本存储设计问题**：系统把“Run 必须固定精确 Skill Version”错误地实现成了“每个 Run 再复制一份完整 Skill 字节到 JSONB”。
- **根本可用性问题**：Worker 缺少安全的瞬时错误重试，启动器缺少必需子进程存活监督，Gateway/UI 也没有正确表达“无 Worker、尚未开始尝试”的状态。

需要保留的正确语义是：**Run Admission 固定精确、不可变的 Agent/Skill/MCP 定义；同一 Run 的 Job retry、checkpoint resume 和 Replay 不得重新解析 Current Version。** 应当替换的是 Skill 字节的持久化方式，不是这个确定性语义，也不是现有只读沙箱挂载边界。

这不是一个完全透明的 Adapter 替换：当前 `backend/AGENTS.md` 明确要求完整字节位于 self-contained Run Snapshot，Worker “decode only that snapshot”。推荐方案会把物理契约改成“Run manifest + FK-pinned immutable Version bytes”。正式实施前必须明确接受并同步修改这条契约；如果“物理自包含”不可放宽，则应选择独立的 Run-pinned 权威 bundle，而不是直接 Version Ref。

长期建议是：

> 新 Run 只 pin 住数据库强约束的不可变 Skill Version；Worker 按 `run_id` 流式读取该 Version 的文件、校验并物化 Run 专属 Skill 树，然后沿用现有 `RunScopedReadOnlyMount` 挂载到 `/mnt/skills`。不再把完整 Skill archive 写进每个 Run 的 JSONB。

## 2. 证据等级与边界

本文使用以下标记：

- **已确认事实**：由当前代码、当前 PostgreSQL 数据、容器 cgroup、PostgreSQL/Worker/Scheduler 日志或进程状态直接验证。
- **合理推断**：由多项已确认事实共同支持，但没有捕获到进程内部每一次内存分配或竞态的完整 trace。
- **未验证假设**：需要专项压测、故障注入或目标部署环境验收后才能成立。

重要边界：

- 已确认 PostgreSQL 被 OOM kill 时正在执行多行 `run_asset_versions` JSONB INSERT；但现有日志不能还原 PostgreSQL 内部“最后一个具体内存分配点”。
- 已确认大 JSONB 是当时活动 SQL，且 cgroup 记录了 `oom_kill=1`。因此可确认故障链，不能把某个 PostgreSQL 内部函数臆测成唯一根因。
- 本报告使用的是本机真实数据库和日志，不把单元测试结果当成线上运行结果。

## 3. 当前问题清单

| 优先级 | 问题 | 用户可见结果 | 状态 |
| --- | --- | --- | --- |
| P0 | 每个 Run 都把完整 Skill 文件复制到 JSONB | 大消息准入形成数据库写入和存储放大；具体 WAL/RSS 增量尚未测量 | 重复字节已确认；资源幅度待压测 |
| P0 | Worker 对领取 Job 前的瞬时数据库恢复错误直接退出 | 数据库恢复后仍无人执行后续消息 | 已确认 |
| P0 | 启动器不监督 Worker/Scheduler 子进程 | 父进程“健康”，执行能力实际已经缺失 | 已确认 |
| P1 | Gateway/UI 没有把“无 Worker、Attempt 尚未开始”与“正在执行”区分 | 页面无限显示“执行中” | 已确认 |
| P1 | Worker 一次加载并解码整个 Run 的全部 Skill Snapshot | 代码路径存在结构性峰值内存风险 | 整批加载已确认；实际 RSS 未测 |
| P1 | `run_asset_versions.version_id` 没有到 Skill Version 的数据库外键 | 不能安全地仅删掉内嵌字节后直接依赖现有引用 | 已确认 |
| P1 | File Finalization 的部分异常被统一折叠为 `PrivateWorkUnavailable` | 后续受控 Run 虽已生成成功回复，最终仍只能看到泛化失败 | 已确认，属于次生诊断问题 |
| P2 | Remote AIO Provisioner 当前不支持私有 Run Skill mount | 远程 Kubernetes Sandbox 不能复用当前本地主机路径挂载方案 | 已确认，不是本次本地故障触发因素 |

## 4. 本次事故的事实链

### 4.1 时间线

| 时间（CST） | 已确认事件 |
| --- | --- |
| 15:30:35.279 | PostgreSQL 主进程报告一个 backend 进程被 `SIGKILL`；活动 SQL 是多行 `INSERT INTO run_asset_versions (..., snapshot_json JSONB)`。 |
| 15:30:35.280 | PostgreSQL 终止其他会话并开始重新初始化。 |
| 15:30:35.611–15:30:35.622 | 新连接收到 “database system is not yet accepting connections / Consistent recovery state has not been yet reached”。 |
| 15:30:35.767 | PostgreSQL 再次 ready；从进程被杀到 ready 约 0.488 秒。 |
| 15:30:35 | Worker 的 `_claim_next()` 命中恢复窗口，收到 `asyncpg.exceptions.CannotConnectNowError`；退出清理也失败，Worker 终止。 |
| 15:30:35 | Scheduler 丢失 advisory-lock 所有权并按 fail-closed 退出。 |
| 15:30:37 以后 | 新 Run 能被 Gateway 准入，但 Job 没有 Worker 领取；受影响的两次尝试最终 `attempt_count=0`。 |

### 4.2 PostgreSQL 并非持续不可用

用户判断“数据库一直可用、没有做任何操作”有一半是对的：

- 数据库在事故前可用，事故后约半秒即恢复，之后也持续可用；用户没有执行数据库管理操作。
- 但在 15:30:35 确实存在约半秒的 PostgreSQL crash recovery 窗口。
- 真正把半秒故障放大为持续卡死的，是 Worker 退出和启动器漏检，而不是数据库一直宕机。

### 4.3 OOM 证据

当前 PostgreSQL 容器：

```text
memory.max = 1073741824      # 1 GiB
memory.events.oom = 5
memory.events.oom_kill = 1
```

PostgreSQL 日志同时记录：

```text
server process ... was terminated by signal 9: Killed
Failed process was running: INSERT INTO run_asset_versions (... snapshot_json) VALUES (...)
```

因此“数据库坏了”不是准确结论；准确结论是：**大 Run Snapshot 写入触发了容器内存上限，PostgreSQL 的一个 backend 被内核杀死并触发全库短暂恢复。**

### 4.4 JSONB 里到底是什么

`run_asset_versions.snapshot_json` 不是聊天消息或历史文本。它保存 Run Admission 时冻结的 Agent、Skill、MCP 完整执行闭包。

事故会话中的典型 Run 有 21 行：

| 类型 | 行数 |
| --- | ---: |
| Agent | 2 |
| Skill | 18 |
| MCP | 1 |

历史 schema v2 中，每个 Skill 文件都被写成：

```json
{
  "path": "...",
  "media_type": "...",
  "content_base64": "..."
}
```

其中 `ppt-master` v3 的实测数据为：

| 指标 | 实测值 |
| --- | ---: |
| 文件数 | 12,922 |
| 原始文件内容总量 | 79,243,541 B |
| 逐文件 Base64 字符总量 | 105,675,292 |
| 单个 `snapshot_json` 的 `pg_column_size` | 107,220,204 B |
| 该 Run 21 行 Snapshot 总量 | 107,625,331 B |

主要内容分布：

| 路径前缀 | 文件数 | 原始字节 |
| --- | ---: | ---: |
| `references/ai-image-comparison` | 55 | 45,281,127 B |
| `templates/sounds` | 191 | 12,543,374 B |
| `templates/icons` | 12,029 | 10,185,092 B |

四个不同 Run 中，同一个 `ppt-master` Snapshot 的 `pg_column_size` 都是 **107,220,204 B**。这证明体积来自同一 Skill 包被按 Run 重复复制，而不是某一条用户消息特别长。

## 5. 当前 Skill 的工作方式

### 5.1 领域生命周期

当前版本模型本身是清晰的：

- 保存 Project Skill 会创建不可变的 **Candidate Version**。
- **Version Activation** 向前移动 `current_version_id`；之后的新 Run 使用新的 **Current Version**。
- 被跳过或被后续版本替代的版本成为 **Historical Version**，但可以继续被已经准入的精确 Run Snapshot 引用。
- **Asset Suspension** 只阻止新准入，不改变 Current Version。
- System Skill 只有不可变 v1；相同 checksum 重装是幂等操作，改变内容必须使用新的 System Skill identity。

这部分不是本次应当推翻的设计。

### 5.2 从准入到沙箱的实际链路

```mermaid
flowchart LR
    A[Run Admission] --> B[解析 Agent Current Version]
    B --> C[解析精确 Skill/MCP 闭包]
    C --> D[读取全部 skill_version_files]
    D --> E[编码完整 Skill Snapshot]
    E --> F[(run_asset_versions.snapshot_json)]
    F --> G[Worker 读取全部 Run assets]
    G --> H[解码为 ResolvedSkillSnapshot]
    H --> I[写入 Run 临时 Skill tree]
    I --> J[RunScopedReadOnlyMount]
    J --> K[PrivateRunFileAuthority.restore]
    K --> L[SandboxProvider.acquire_private]
    L --> M[/mnt/skills]
    M --> N[先释放 Sandbox mount 再删除临时树]
```

具体步骤如下：

1. Gateway 在 Run Admission 事务中解析当前 Agent，以及它依赖的所有精确 Skill/MCP Version。
2. `ProjectAssetResolver._skill_snapshot()` 对 `skill_version_files` 加读锁、读取完整文件集合并验证 archive。
3. `RunSnapshotRepository` 为每个资产创建一行 `RunAssetVersionRow`。基线 `7c3802a8` 的 schema v2 会把每个 Skill 文件逐个 Base64 后放进 `snapshot_json`；本变更中的止血补丁改为 schema v3 单压缩帧后再 Base64。
4. Worker 按 `dependency_order` 一次读出全部 `run_asset_versions`，解码每一行，并校验 kind、scope、asset/version id、checksum 和 catalog generation。
5. Worker 创建 `deerflow-private-<run_id>-*` 临时目录。
6. `write_skill_tree()` 先写入 `.staging/<version_id>`，检查路径穿越、解析 `SKILL.md`、验证运行时名称，再原子 rename 到：
   - System Skill：`public/<skill-name>`；
   - Project Skill：`custom/<asset_id>`；
   - 同资产额外版本：`.versions/<asset_id>/<version_id>`。
7. 文件在宿主临时树中设为 `0600`，Skill 运行时对象标记为只读。
8. Executor 创建 `RunScopedReadOnlyMount(run_id, container_path, host_path)`，默认准备把宿主树映射到沙箱 `/mnt/skills`。
9. `PrivateRunFileAuthority.restore()` 先执行执行权重验证，再把 mount 交给 `SandboxProvider.acquire_private_async(...)`，最后恢复本次 Run 的文件投影。
10. Local Sandbox 使用只读 path mapping；本地容器 AIO Sandbox 使用真正的 read-only bind mount，并为私有 Run 创建新 Sandbox。
11. Run 结束时先由 `file_authority.release()` 释放/销毁 Sandbox lease 和 mount；随后 `PrivateAgentRuntime.aclose()` 关闭 MCP session 并删除临时 Skill tree。必须保持“先卸载、后删树”。

### 5.3 当前挂载能力边界

已确认：

- 当前本地 Local Sandbox 和 local-container AIO Sandbox 都能接收 Run 专属 Skill tree。
- AIO 私有容器会拒绝非只读 mount，并检查 mount 不得覆盖 `/etc`、`/var`、容器 socket、私有用户存储等受保护路径。
- 对 private Run，最终生效的 `/mnt/skills` 视图必须由服务端发行的精确 Run mount 替换：Local provider 会移除该前缀下的旧 mapping，AIO private container 创建时不带全局配置 mount。
- Remote AIO backend 当前忽略 `extra_mounts`，而 `AioSandboxProvider` 对 Remote backend 的私有 Sandbox 请求直接 fail closed。因此当前“宿主临时目录 bind mount”只适用于本地可见文件系统，不等于已经支持 Kubernetes Provisioner。

关键判断：**把 Skill 挂载进沙箱并不要求把 Skill 字节放进 JSONB。** 沙箱边界只要求 Worker 在执行前得到经过校验的精确文件树；这些字节可以来自被 Run pin 住的不可变 Skill Version。

### 5.4 当前数据库如何存储、如何加载

#### 5.4.1 现有物理存储

当前不是“Skill 只存在 JSONB 中”，而是同一份 Skill 文件被保存了两次：Version 表保存一份权威原始字节，每个 Run 又保存一份完整副本。

| 表 | 当前用途 | 与 Skill 字节的关系 | 关键约束/索引 |
| --- | --- | --- | --- |
| `skills` | Skill 逻辑身份、scope、Project 归属、状态和 `current_version_id` | 不保存文件 | `UNIQUE(id, scope)`、`UNIQUE(project_id, id)` |
| `skill_versions` | 不可变 Version 元数据、`secret_requirements`、扫描信息、`payload_checksum` | 不保存文件内容 | PK `id`；`UNIQUE(skill_id, id)`；FK 到 `skills` 为 `RESTRICT` |
| `skill_version_files` | Version 的权威文件集合 | `content BYTEA` 保存原始字节；同时保存 `path/media_type/size_bytes/sha256` | PK `(skill_version_id, path)`；FK Version 为 `RESTRICT`；大小、内容长度、路径和摘要格式约束；不可变触发器 |
| `run_asset_versions` | 每个 Run 的 Agent/Skill/MCP 有序闭包 | Skill 行的 `snapshot_json JSONB` 当前再次内嵌完整文件集合 | PK `(project_id, owner_user_id, run_id, asset_kind, dependency_order)`；只 FK 到 Run，没有 FK 到具体 Skill Version |
| `run_skill_secret_snapshots` | Run 使用的精确 Secret Generation 引用 | 不保存 Skill 文件，也不保存 secret 明文/密文 | FK 到 Run、Skill Version 和 Secret Generation；语义与文件存储分离 |

`skill_version_files` 的单文件数据库约束上限是 100 MiB；业务归档验证实际采用更严格的组合限制：单文件 64 MiB、单 Skill 总计 100 MiB、最多 16,384 个文件。其复合主键已经支持“按 Version 过滤、按数据库默认 collation 的 path 排序”；目标若显式采用跨环境稳定的 `COLLATE "C"`，仅在默认 collation 不是 `C` 时增加匹配索引。

当前 Skill Run Snapshot 有两种编码：

- 基线 `7c3802a8` 的 schema v2：`snapshot_json.skill.files[]` 为逐文件元数据和 Base64 内容；
- 本变更止血补丁的 schema v3：把整个归档编码成一个 zlib frame，再以单个 `archive_base64` 字符串写进同一 JSONB。

两者都保留以下重复所有权：

```text
skill_version_files.content BYTEA             -- 每个 Version 一份权威原始字节
        +
run_asset_versions.snapshot_json JSONB        -- 每个引用该 Version 的 Run 再复制一份
```

所以 v3 只降低重复副本的常数，没有把存储复杂度从 `O(R × S)` 改成 `O(S + R × manifest)`。

#### 5.4.2 当前 Gateway 准入写入路径

当前 `_skill_snapshot()` 对每个已解析 Skill Version 执行等价查询：

```sql
SELECT skill_version_id, path, media_type, size_bytes, sha256, content
FROM skill_version_files
WHERE skill_version_id = :version_id
ORDER BY path
FOR SHARE;
```

SQLAlchemy 实际选择完整 `SkillVersionFileRow`，随后调用 `.scalars().all()`。这意味着准入阶段会先把该 Version 的全部 `BYTEA` 行装入 Python，再完成 archive 验证并构造 `ResolvedSkillSnapshot(files=...)`。`RunSnapshotRepository` 之后把完整文件集合编码到 `snapshot_json`，为闭包中的每个资产创建一个 `RunAssetVersionRow`，并与 Run、Job、Secret Snapshot 由外层准入事务一起提交。

现有仓库已经有可复用的“只查文件 facts”查询形态：`SkillRepository._load_file_metadata()` / `_load_file_map()` 只选择 `skill_version_id/path/media_type/size_bytes/sha256`，不选择 `content`。目标准入路径应复用这个边界，而不是再读取完整归档。

#### 5.4.3 当前 Worker 加载路径

Worker materialization 调用 `list_assets_in_session(..., lock=True)`，等价查询为：

```sql
SELECT *
FROM run_asset_versions
WHERE project_id = :project_id
  AND owner_user_id = :owner_user_id
  AND run_id = :run_id
ORDER BY dependency_order
FOR UPDATE;
```

因为选择的是完整 ORM 实体，`snapshot_json` 也必然随行加载。代码随后依次执行：

1. 把每行 `snapshot_json` 转成 Python `dict`，并先构造完整 `tuple[RunAssetSnapshot, ...]`；
2. 对 tuple 中每个资产调用 `decode_run_asset_snapshot()`，并先构造完整 `resolved` 列表；
3. 对 Skill v2 解码每个 Base64 文件，或对 v3 解码一个大 Base64 字符串并整体解压；
4. 得到所有 `ResolvedSkillSnapshot.files` 后，才调用 `write_skill_tree()` 写临时目录；
5. 完成校验后交给只读 Sandbox mount。

因此当前查询虽然是“一次查出 Run 的所有资产”，却不是流式加载。数据库结果、JSONB/Python 对象、Base64 字符串、解码或解压后的 bytes、`ResolvedSkillSnapshot` 和正在写入的文件可能在阶段边界同时存活。**代码可以确认存在这些完整中间表示；每一层的精确 RSS 峰值尚未用生产等价压测量化。**

`RunSnapshotRepository.list_asset_facts_in_session()` 已经证明同一张表可以只投影 identity/checksum 等小字段；目标 Worker 应先读小型 manifest，再用独立 server-side cursor 读取 Skill `BYTEA`，不能继续通过 `SELECT *` 加载大 JSONB。

## 6. 设计缺陷在哪里

### 6.1 正确需求和错误实现被耦合在了一起

正确需求：

- Run 必须固定精确版本。
- 同一 Run 的重试和恢复必须使用同一字节。
- Worker 不得在执行时重新解析 Current Version。
- Secret 只保存精确 Generation 引用和完整性摘要，不进入 Skill 包。

当前错误实现：

- 把“精确版本”理解成“每个 Run 自己再拥有一份完整文件副本”。
- 把大量二进制文件包装成 JSONB/Base64，使 Gateway、PostgreSQL、WAL、数据库存储和 Worker 解码同时承担放大成本。
- 即使两个 Run 使用完全相同的不可变 Skill Version，仍然重复写入同样的 100 MiB 级数据。

### 6.2 当前存储复杂度

设 Skill 大小为 `S`，使用它的 Run 数为 `R`：

- 当前 v2：持久化接近 `O(R × 4/3 × S)`，另有 JSON、TOAST、WAL 和解析开销。
- 本变更中的 v3：压缩能降低常数，但仍然是 `O(R × compressed(S))`。
- 目标方案：Skill 原始 Version 字节只保存一次，Run 只保存 `O(R × manifest)` 的小引用。

压缩是必要的短期保护，但不能解决按 Run 重复所有权的问题。

### 6.3 为什么不能现在只把 JSONB 删掉

`run_asset_versions` 虽然已有 `asset_id`、`version_id` 和 `payload_checksum`，但它是 Agent/Skill/MCP 多态表，当前没有数据库外键指向 `skill_versions`。

现有 Project Skill hard-delete 只在应用层查询是否仍有 Run 引用。这个检查能提供友好的 `ASSET_IN_USE` 错误，却不能单独承担底层持久化不变量。System asset 维护 GUC 还存在绕过部分通用不可变触发器的路径。

在移除内嵌 Skill 字节前，必须先建立数据库级保证：

> 只要任一可保留、可执行或可重试的 Run 引用了某个 Skill Version，该 Version 及其完整文件集合就必须存在、字节不可变、不可增删，直到最后一个 Run 引用被删除。

checksum 只能发现内容缺失或漂移，不能恢复已经删除的内容，因此不能代替 FK 和不可变约束。

## 7. 方案比较

| 方案 | 优点 | 主要问题 | 建议 |
| --- | --- | --- | --- |
| A. 继续把完整 Skill 压缩后写进每个 Run JSONB | 改动较小；旧 Snapshot 自包含 | 仍按 Run 重复；仍有大 SQL 参数、WAL、TOAST 和解压峰值 | 只作为当前止血，不作为目标架构 |
| B. Run 直接 pin 不可变 Skill Version，Worker 流式物化 | 最小化重复；复用现有权威字节；符合 Historical Version 可被 Run 引用的领域模型 | 必须增加数据库强引用和文件不可变硬约束；改变当前物理 self-contained Snapshot 契约；Worker 读取行数较多 | **推荐目标方案，但需先接受契约变更** |
| C. 新建内容寻址 Bundle/Chunk 权威存储 | 可按内容去重；Run 与 Skill Version 生命周期物理解耦；保留 self-contained closure 语义 | 新增 bundle、chunk、GC、namespace、配额和迁移复杂度；过渡期会与 `skill_version_files` 再重复一份 | 物理自包含不可放宽时选用，否则暂缓 |
| C2. 内容寻址本地缓存（非权威） | 缓解大量小文件的重复读取；丢失后可重建 | 需要原子填充、隔离、淘汰和格式版本管理 | 仅在方案 B 压测证明需要后增加 |
| D. 仅增大 PostgreSQL 内存 | 最快降低复发概率 | 不消除设计放大、Worker 退出和监督缺陷 | 只能作为运维安全余量 |

选择 B 的原因：

- Project Skill Version 已经不可变，`skill_version_files` 已经是权威字节源。
- 当前领域文档明确允许 Historical Version 被精确 Run Snapshot 继续引用。
- System Skill v1 在当前 checkout 中同 identity 不允许换字节；改变内容必须新建 identity。
- 因此没有必要为了“历史精确性”立刻再造一套 bundle 权威存储。
- 直接 Version Ref 先消除最大写放大；如果 Worker 首次读取 12,922 行经压测仍有明显瓶颈，可以在同一接口后增加可丢弃的 checksum 缓存，而不改变 Run 或 Skill 的领域模型。

## 8. 推荐目标架构

### 8.1 一个深模块：`RunSkillTreeMaterializer`

`RunSnapshotRepository` 继续拥有准入事务，在同一事务中写 Run、Job、v4 Skill manifest 和类型化强引用。Worker 侧新增一个深 materialization seam；执行调用方不接触数据库 session、文件 cursor、临时目录或 schema 分支：

```python
@dataclass(frozen=True, slots=True)
class RunSkillManifest:
    asset_id: UUID
    version_id: UUID
    scope: AssetScope
    payload_checksum: str
    file_count: int
    content_size_bytes: int
    secret_requirements: tuple[SkillSecretRequirementSnapshot, ...]


@dataclass(slots=True)
class MaterializedRunSkillTree:
    root: Path
    manifests: tuple[PrivateSkillManifest, ...]
    skills: tuple[Skill, ...]

    async def aclose(self) -> None: ...


class RunSkillTreeMaterializer(Protocol):
    async def materialize(
        self,
        *,
        context: PrivateWorkContext,
        run_id: str,
        execution_boundary: PrivateRunExecutionBoundary,
    ) -> MaterializedRunSkillTree: ...
```

这里必须接收当前 Job Attempt 的 lease-aware `PrivateRunExecutionBoundary`，它组合 `job_id + lease_token` 与 Run/Project 授权；只验证 Project/Membership/Run active 的普通 `PrivateRunAuthorizationBoundary` 不足以证明当前 attempt 仍拥有执行权。

Admission 只从 immutable Version 和文件 facts 形成小型 manifest，不读取完整文件内容；`materialize()` 才在 Worker 中读取字节。返回的 `MaterializedRunSkillTree` handle 必须交给 `PrivateAgentRuntime`，后者的 `aclose()` 委托该 handle 清理，避免旧 `skill_root` 清理和新 handle 出现双重所有权。

该接口应隐藏：

- Run Skill 引用表和锁顺序；
- Historical/System Skill Version 的精确读取；
- 文件流式查询、路径规范化、逐文件大小/SHA-256 校验；
- 聚合 `payload_checksum` 校验；
- staging、原子发布和失败清理；
- v2/v3 legacy inline Snapshot 与新 manifest source 的联合类型和解码；
- 数据库错误到 `RUN_ASSET_STALE`、too-large、unavailable 的稳定映射；
- 可选的本地 checksum 缓存。

调用方只表达“物化这个 Run 已准入的 Skill tree”，不应该知道数据来自 legacy JSON、Skill Version 行还是缓存。

### 8.2 新的持久化引用

#### 8.2.1 目标所有权链

目标数据库链路应为：

```text
runs
  └─ run_asset_versions                 每个 Run 的小型有序资产 stub/v4 manifest
       └─ run_skill_version_refs         Skill 类型扩展和精确强引用
            └─ skill_versions            不可变 Version 元数据与 archive facts
                 └─ skill_version_files  唯一权威文件 BYTEA
```

不新建第二套 bundle 权威存储。`skill_version_files.content` 是 Skill 字节的唯一数据库所有者；每个 Run 只增加 KB 级 manifest/ref。Agent、MCP、System Model 和 `run_skill_secret_snapshots` 不在本次改造中顺带重构。

建议在 Version 创建时，把已经验证过的 archive facts 一次写入 `skill_versions.file_count` 和 `skill_versions.content_size_bytes`。这样每次 Run Admission 不必重新扫描 12,922 行求和，且 Run ref 可以用复合 FK 同时 pin Version identity、checksum 和 facts。

#### 8.2.2 目标表和约束

下面是表达目标约束的等价 DDL，**不是允许在运行时手工执行的迁移脚本**。本仓库应把同样结构同步写入 ORM、Schema V1 `full_schema.sql`、schema comments/catalog digest 和聚焦测试；现有数据库按项目规则显式 recreate/import。

```sql
ALTER TABLE skill_versions
  ADD COLUMN file_count INTEGER NOT NULL,
  ADD COLUMN content_size_bytes BIGINT NOT NULL,
  ADD CONSTRAINT ck_skill_versions_file_count
    CHECK (file_count BETWEEN 1 AND 16384),
  ADD CONSTRAINT ck_skill_versions_content_size
    CHECK (content_size_bytes BETWEEN 0 AND 104857600),
  ADD CONSTRAINT uq_skill_versions_runtime_exact
    UNIQUE (
      skill_id, id, payload_checksum,
      file_count, content_size_bytes
    );

ALTER TABLE run_asset_versions
  ADD CONSTRAINT uq_run_asset_versions_dependency_order
    UNIQUE (project_id, owner_user_id, run_id, dependency_order),
  ADD CONSTRAINT uq_run_asset_versions_runtime_exact
    UNIQUE (
      project_id, owner_user_id, thread_id, run_id,
      asset_kind, dependency_order, asset_scope,
      asset_id, version_id, payload_checksum
    );

CREATE TABLE run_skill_version_refs (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    asset_kind VARCHAR(16) NOT NULL,
    dependency_order INTEGER NOT NULL,
    asset_scope VARCHAR(16) NOT NULL,
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
    CONSTRAINT uq_run_skill_version_refs_version UNIQUE (
      project_id, owner_user_id, run_id, skill_version_id
    ),
    CONSTRAINT ck_run_skill_version_refs_kind
      CHECK (asset_kind = 'skill'),
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
      skill_id, skill_version_id, payload_checksum
    ) REFERENCES run_asset_versions (
      project_id, owner_user_id, thread_id, run_id,
      asset_kind, dependency_order, asset_scope,
      asset_id, version_id, payload_checksum
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

Run Snapshot/ref 必须是 insert-once：准入后禁止 UPDATE，ref 只能随父 `run_asset_versions` 的 retention delete 级联删除，不能被独立重定向或局部删除。至少增加数据库 UPDATE 不可变触发器；直接 DELETE 通过 repository privilege/constraint trigger 限制为父级 cascade，并由最终 plan fingerprint 复核兜底：

```sql
CREATE FUNCTION prevent_run_asset_snapshot_update()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Run asset snapshot rows are immutable'
    USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_run_asset_versions_immutable
  BEFORE UPDATE ON run_asset_versions
  FOR EACH ROW EXECUTE FUNCTION prevent_run_asset_snapshot_update();

CREATE TRIGGER trg_run_skill_version_refs_immutable
  BEFORE UPDATE ON run_skill_version_refs
  FOR EACH ROW EXECUTE FUNCTION prevent_run_asset_snapshot_update();
```

这里不能直接给多态 `run_asset_versions.version_id` 增加“当 kind=skill 时指向 `skill_versions`”的条件 FK；PostgreSQL 没有这种多态条件外键。专用 subtype 表的作用正是把 Skill 的真实关系约束从 JSONB 提升到数据库。

`skill_project_id` 不能省略：

- System Skill 要求 `asset_scope='system'` 且 `skill_project_id IS NULL`，再由 `(skill_id, asset_scope)` FK 验证 scope；
- Project Skill 要求 `skill_project_id = Run.project_id`，再由 `(skill_project_id, skill_id)` FK 阻止 Project A 的 Run 引用 Project B 的 Skill。

还需要以下索引：

```sql
-- PostgreSQL 不会自动给 referencing FK 建反向索引；供 hard-delete/FK 检查使用。
CREATE INDEX ix_run_skill_version_refs_version
  ON run_skill_version_refs (skill_version_id);

-- 双读期供旧 v2/v3 hard-delete EXISTS 查询使用。
CREATE INDEX ix_run_asset_versions_legacy_project_skill
  ON run_asset_versions (project_id, asset_id, version_id)
  WHERE asset_kind = 'skill' AND asset_scope = 'project';

-- 当数据库默认 collation 不是 C 时，保证 canonical path 顺序且避免携带 BYTEA 排序。
CREATE INDEX ix_skill_version_files_stream
  ON skill_version_files (skill_version_id, path COLLATE "C");
```

如果数据库默认 collation 已是 `C`，现有 `(skill_version_id, path)` 主键可以服务最后一条查询，无需重复索引。目标 Schema 还应把 `skill_version_files.size_bytes` 的单行上限从当前数据库的 100 MiB 收紧为业务层相同的 64 MiB；否则即使 `yield_per=1`，数据库仍可能合法返回一行 100 MiB 的 `BYTEA`。

#### 8.2.3 v4 manifest 的实际内容

新 Skill 行仍保留一个小 `snapshot_json`，用于 schema discriminator 和逻辑元数据，但不再拥有文件字节：

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
    "content_size_bytes": 79243541,
    "secret_requirements": [
      {"name": "API_KEY", "target_env": "API_KEY", "optional": true}
    ]
  }
}
```

v4 中明确禁止 `files`、`content_base64`、`archive_base64`、`codec`、`compressed_size`。外层 typed columns、JSON identity、ref row 和 Version facts 必须逐字段一致；`run_skill_version_refs` 才是强引用，JSON 不是外键替代品。Secret requirement 是声明，不是 secret 值；精确 Generation 仍只在 `run_skill_secret_snapshots`。

普通 FK 只保证“每条 ref 都有 typed identity 完全一致的父 Skill stub”，不能反向保证“每条 v4 Skill stub 必有一条 ref”，也不能检查父 `snapshot_json` 一定是 schema v4/`source=skill_version_ref`。首期由同一 Admission 事务、repository tests 和 Worker fail-closed 联合保证；若必须把后两项也下沉到数据库，应增加 typed `snapshot_schema_version` 列并纳入约束，或使用 `DEFERRABLE INITIALLY DEFERRED` constraint trigger，不能把普通 FK 的能力写大。

#### 8.2.4 Admission 的原子写入和锁顺序

Version 创建路径先验证完整 archive，并一次计算 `file_count`、`content_size_bytes` 和现有 canonical `payload_checksum`。Run Admission 之后只读这些 facts，不读 `skill_version_files.content`，也不构造 `ResolvedSkillSnapshot.files`。

一笔准入事务的目标顺序：

1. revalidate Project/membership，处理 Thread 和幂等 request；
2. 解析精确 Agent/Skill/MCP closure；Current、Active、binding、suspension、revocation 只在此时判断；
3. 在现有上层 Admission 锁图（inbound/connection/conversation、Thread、approval、既有 Run/Job 等）之后，定义目标资产子图锁序 Agent → Skill → MCP → secret；同一 kind 的资产/Version 使用稳定 UUID 顺序，不能用输出用的 `dependency_order` 当锁顺序。该顺序是改造目标，不是当前所有路径已实现的事实；实施前必须与 hard-delete、bootstrap、binding 和 purge 的完整锁图做并发测试；
4. 对所有 exact `skill_versions` 至少持有 `FOR KEY SHARE`/`FOR SHARE`，读取 checksum、facts 和 secret requirements；
5. 创建尚不可见的 Run/Job 和所有 `run_asset_versions`；Skill 行只编码 v4 小 manifest，随后 `flush` 父行；
6. 插入一一对应的 `run_skill_version_refs`，由 exact parent FK、exact Version FK 和 Project/scope FK 在 flush 时关闭 TOCTOU 窗口；
7. 插入 `run_skill_secret_snapshots`、MCP secret snapshots 和其余准入状态；
8. 单次 `COMMIT`。任一步失败全部回滚，不能出现“Job 已可 claim，但 Skill ref 缺失”的半套 closure。

并发删除时，应用层检查负责返回友好的 `ASSET_IN_USE`；FK 是最终正确性防线：Admission 先插 ref，则删除等待后因引用失败；删除先提交，则 Admission 的重新验证/FK 失败并映射为 stale。

同一 `request_id/run_id` 的 API retry 必须读取已经持久化的 Run/ref，不重新解析 Current，也不能用盲目 `ON CONFLICT DO NOTHING` 掩盖不完整 closure。“Regenerate”才是新 Run，重新按当时 Current 准入。

#### 8.2.5 删除、retention 和 maintenance 例外

当前普通不可变保护已经存在：Version payload UPDATE 触发器、文件 UPDATE 触发器，以及文件 INSERT/DELETE child-mutation 触发器。缺口不是“完全没有不可变触发器”，而是这些触发器的 System upgrade、assembly、Project purge 和 hard-delete 例外尚不知道 Run pin。

目标必须补齐：

1. Project Skill hard-delete 双读期同时查询新 `run_skill_version_refs` 和旧 v2/v3 `run_asset_versions`；存在任一引用即返回 `AssetInUse`。
2. 当前删除代码先删 `skill_version_files`、后删 `skill_versions`，所以只给 Version 加 `RESTRICT` FK 不能保护文件。`prevent_asset_version_child_mutation()` 必须在任何 maintenance/assembly/purge GUC 放行前先检查 pinned ref；存在 ref 时无条件拒绝文件 INSERT/DELETE。
3. `prevent_shared_asset_version_payload_update()` 也必须在 `deerflow.system_asset_upgrade=on` 的 bypass 前拒绝被 Run pin 的 execution payload 原地变化。内容变化必须创建新 identity/version；governance revocation 字段仍按现有单独规则处理。
4. 普通 Run retention 删除父 `run_asset_versions` 时，ref 由 `ON DELETE CASCADE` 删除；Project shared purge 保持“Run assets/refs → Skill files → Versions → Skill identity”的顺序。
5. “被保留的 Run”应精确定义为 retention policy 下仍允许 retry/resume/replay 的 Run；只要仍要求历史可执行性就必须保留 ref。方案 B 首期没有独立权威 bundle，也没有引用计数 GC，不能加入 `COUNT(refs)=0` 后并发删除的隐式 GC。
6. 当前代码审查尚未确认 private purge 会等待所有普通 running/leased Job。目标需增加生命周期 interlock：先 cancel/fence active attempt，等待 terminal 或 lease expiry，随后才物理删除 Run refs；否则已经挂载的 Run tree 可能在治理删除后继续执行。

领域文档、ADR 和 `backend/AGENTS.md` 也要同步修改物理表示契约：Worker 只读取“不可变 Run closure（Run manifest + FK-pinned exact Version bytes）”，永不读取 Current Version 或重新应用准入资格。这样改变的是 Snapshot 的存储表示，不是同一 Run 的确定性语义。

### 8.3 Worker 的流式物化

新 Worker 路径：

```mermaid
flowchart LR
    A[Run manifest] --> B[run_skill_version_refs]
    B --> C[(immutable skill_version_files)]
    C -->|按 path 流式读取| D[.staging/version_id]
    D -->|逐文件 SHA + 总 checksum| E[MaterializedRunSkillTree]
    E --> F[RunScopedReadOnlyMount]
    F --> G[/mnt/skills]
```

#### 8.3.1 两段事务，而不是长时间持有业务写锁

当前 engine 未显式覆盖隔离级别，因而使用 PostgreSQL 默认 `READ COMMITTED`。目标 materializer 建议拆为：

1. **控制事务**：锁 Project/membership/Run，校验 capability、Run/Job lease、ordered closure、refs 和 exact Secret Generations，随后释放业务行锁；
2. **内容事务**：独立 Session，`REPEATABLE READ, READ ONLY`，在同一个 MVCC snapshot 中顺序读取所有 v4 文件 cursor 和 legacy payload；设置有界 materialization timeout，避免长事务无限延迟 VACUUM；
3. 文件树完成后、交给 Sandbox 之前，再经过当前 attempt 的 lease-aware `PrivateRunExecutionBoundary` 验证 Run、`job_id + lease_token` 和 fencing，并重新读取 ordered manifest/ref fingerprint 与控制事务得到的 plan 比较。期间发生 cancel、purge、ref 缺失或 lease loss 时只清理 staging，不把内容交给模型。

这样不需要在读取 100 MiB 和写磁盘的整个期间持有 Run `FOR UPDATE`。PostgreSQL 的 `REPEATABLE READ` snapshot 在第一条需要 snapshot 的 scoped `SELECT` 执行时建立，而不是仅在 `BEGIN` 时建立：如果 purge 在这条查询后提交，当前只读 snapshot 仍能读完查询开始时的 exact rows，最终授权/plan 复核会阻止已失效 Run 挂载；如果 purge 在第一条查询前已经提交，查询缺行并 fail closed。

#### 8.3.2 第一条查询：只读有序 manifest/facts

控制事务先用下列查询形成 materialization plan；内容事务再把同一查询作为第一条 scoped `SELECT`，建立 `REPEATABLE READ` snapshot 并要求结果与 plan 完全一致。它只查小字段，**不选择 `snapshot_json` 或文件 `content`**：

```sql
SELECT
  asset.asset_kind,
  asset.dependency_order,
  asset.asset_scope,
  asset.asset_id,
  asset.version_id,
  asset.payload_checksum,
  asset.catalog_generation,
  ref.asset_kind AS ref_asset_kind,
  ref.dependency_order AS ref_dependency_order,
  ref.asset_scope AS ref_asset_scope,
  ref.skill_project_id,
  ref.skill_id,
  ref.skill_version_id,
  ref.payload_checksum AS ref_payload_checksum,
  ref.file_count,
  ref.content_size_bytes,
  sv.payload_checksum AS version_payload_checksum,
  sv.file_count AS version_file_count,
  sv.content_size_bytes AS version_content_size_bytes,
  sv.secret_requirements AS version_secret_requirements
FROM run_asset_versions AS asset
LEFT JOIN run_skill_version_refs AS ref
  ON ref.project_id = asset.project_id
 AND ref.owner_user_id = asset.owner_user_id
 AND ref.thread_id = asset.thread_id
 AND ref.run_id = asset.run_id
 AND ref.asset_kind = asset.asset_kind
 AND ref.dependency_order = asset.dependency_order
LEFT JOIN skill_versions AS sv
  ON sv.skill_id = ref.skill_id
 AND sv.id = ref.skill_version_id
WHERE asset.project_id = :project_id
  AND asset.owner_user_id = :owner_user_id
  AND asset.thread_id = :thread_id
  AND asset.run_id = :run_id
ORDER BY asset.dependency_order, asset.asset_kind;
```

Loader 必须验证：

- `dependency_order` 是全局连续的 `0..N-1`，Agent 在前、Skill 居中、MCP 在后；
- v4 Skill 有且只有一条 exact ref，非 Skill 没有 Skill ref；
- 同一 Run 不重复引用同一 Skill Version；
- parent/ref 的 scope、asset/version identity、checksum、Project 归属和 facts 完全一致；
- Agent 声明的 Skill Version closure 与实际 Run refs 集合完全相等；
- 全部行的 `catalog_generation` 一致。

随后只为“有 ref 的 Skill 行”以内连接读取小型 v4 `snapshot_json` 并验证 `schema_version=4/source=skill_version_ref`；Agent/MCP 走现有小 payload decoder。没有 ref 的 Skill 被分类为 legacy，按 dependency order 一次只加载一行旧 JSONB，不能在第一条查询中把所有旧大 JSONB 一起带回。

整个执行路径都不能 join `skills.current_version_id`，也不能调用 `ProjectAssetResolver` 重新应用 Current、Active、binding、suspension 或 revocation。那些是新 Run 的 Admission policy，不是已准入 Run 的读取规则。

#### 8.3.3 第二类查询：每个 Version 一个文件 cursor

正确性优先的首期实现按不同 Skill Version 启动一个 server-side cursor：

```sql
SELECT
  file.path,
  file.media_type,
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
ORDER BY file.path COLLATE "C";
```

对应 SQLAlchemy 形态：

```python
statement = (
    select(
        SkillVersionFileRow.path,
        SkillVersionFileRow.media_type,
        SkillVersionFileRow.size_bytes,
        SkillVersionFileRow.sha256,
        SkillVersionFileRow.content,
    )
    .join(
        RunSkillVersionRefRow,
        RunSkillVersionRefRow.skill_version_id
        == SkillVersionFileRow.skill_version_id,
    )
    .where(
        RunSkillVersionRefRow.project_id == context.project_id,
        RunSkillVersionRefRow.owner_user_id == str(context.user_id),
        RunSkillVersionRefRow.thread_id == thread_id,
        RunSkillVersionRefRow.run_id == run_id,
        RunSkillVersionRefRow.dependency_order == dependency_order,
        RunSkillVersionRefRow.skill_version_id == version_id,
    )
    .order_by(SkillVersionFileRow.path.collate("C"))
)

stream = await session.stream(
    statement,
    execution_options={"yield_per": 1},
)
try:
    async for row in stream:
        await writer.write_verified(row)
finally:
    await stream.close()
```

关键点：

- 使用显式 Core columns，不加载 ORM entity，避免 Session identity map 留住 `BYTEA`；
- 必须 `session.stream()`，不能 `.all()`、`.scalars().all()` 或先转 tuple；
- `yield_per=1`，因为单个文件仍可能是 64 MiB；
- 一个 Skill 一个 cursor，配合 `(skill_version_id, path COLLATE "C")` 索引，避免跨所有 Version 对携带大 `BYTEA` 的结果做全局 Sort；
- 查询数约为固定小查询加 `V` 个 cursor，`V` 是本 Run 的不同 Skill Version 数量，不是文件数。18 个 Skill 约二十余条 statement，不是 12,922 次逐文件查询；
- 只有真实 `EXPLAIN (ANALYZE, BUFFERS)` 证明联合查询稳定使用索引、没有携带 BYTEA 的大 Sort 后，才考虑合并成一个 cursor。

#### 8.3.4 逐行校验、增量 checksum 和原子发布

每行到达后立即：

1. 验证规范化相对 POSIX path、NFC/casefold identity、严格递增顺序和文件/目录前缀冲突；
2. 复用现有 media-type validator，验证 `media_type` 非空、没有前后空白且字符/UTF-8 字节长度均在现有上限内；checksum 不覆盖 media type，不能省略这一步；
3. 验证 `len(content) == size_bytes`、单文件不超过 64 MiB、`sha256(content) == sha256`；
4. 累计文件数和总字节，任何时刻不得超过 Version/ref/v4 声明；
5. 以 `0600` 写入 `.staging/<version_id>`，当前行写完后释放 Python `bytes` 引用；
6. 用已经验证的 `path/sha256/size_bytes` 增量复现现有 canonical JSON checksum：依次向 SHA-256 输入 `[`、每个 `json.dumps(object, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")`、对象间逗号和 `]`，不保存完整 facts 列表。

Version 结束时必须同时核对实际 `file_count`、实际 `content_size_bytes`、聚合 checksum、Version 行、Run ref、父 Run asset 和 v4 manifest。还要确认唯一 `SKILL.md` 可解析、运行时名称合法。只有闭包全部成功后才原子 rename 并返回 `MaterializedRunSkillTree`；任何异常或取消都关闭 cursor、删除 staging，不发布半棵树。

#### 8.3.5 BYTEA “流式”的真实内存边界

`AsyncSession.stream()` 只能逐**行**取数，不能把一个 PostgreSQL `BYTEA` 列按字节块送入 Python。一个 `content` 仍会完整 materialize 为一个 Python `bytes`，所以真实峰值至少包括：

```text
最大单文件 BYTEA
+ driver/protocol/fetch buffer
+ 当前文件写入缓冲
+ 路径冲突检查状态
```

目标 v4 可以把 Worker 从 `O(完整 Run Skill closure)` 降到约 `O(最大单文件 + 路径元数据)`，但不能声称严格 `O(1)` 或固定几 MiB。按当前业务限制，最大单文件是 64 MiB；路径校验状态还随文件数和路径总长度增长。

如果验收要求例如“不超过 8 MiB 的硬窗口”，必须进一步把权威内容改为固定大小 chunk 表，例如 `skill_version_file_chunks(version_id, path, chunk_no, content)`，或使用真正支持 streaming body 的对象存储；`yield_per=1` 本身达不到该承诺。

#### 8.3.6 v2/v3/v4 双读矩阵

| ref 状态 | Skill schema/source | 处理 |
| --- | --- | --- |
| 无 ref | v2 inline | legacy v2 Adapter |
| 无 ref | v3 compressed | legacy v3 Adapter |
| 有 exact ref | v4 + `skill_version_ref` | 新 Version cursor Adapter |
| 有 ref | v2/v3 | `RUN_ASSET_STALE` |
| 无 ref | v4 | `RUN_ASSET_STALE` |
| 任意 | 未知 schema、identity/facts 不一致 | `RUN_ASSET_STALE` |

Legacy Adapter 也应按 dependency order 一次读取、解码、写入和释放一个 Skill，不再先构造整个 Run tuple；它不能在旧 Snapshot 损坏时回退到当前 `skill_version_files`，也不能在执行时把旧 Run 自动回写为 v4。v3 历史数据仍需整体解压单个 Skill，所以双读最多把 legacy 峰值从“整个 Run”降到“一个旧 Skill”，不能获得 v4 的单文件边界。

#### 8.3.7 错误分类

| 情况 | 内部映射 | Job 语义 |
| --- | --- | --- |
| ref 缺失，order/identity/checksum/count 不一致 | `RunSnapshotAssetStale` → `PrivateWorkAssetStale` | permanent `RUN_ASSET_STALE` |
| 非法 schema/source 组合 | 同上 | permanent |
| path、SHA、聚合 checksum、文件数或大小不一致 | 同上 | permanent |
| PostgreSQL recovery、连接或 statement timeout | `PrivateWorkUnavailable` | 尚未进入 graph 时可 transient retry |
| 临时目录 `ENOSPC/EIO` | `PrivateWorkUnavailable` | 清 staging 后 transient |
| `CancelledError`、lease/fencing loss | 保持取消/授权异常 | 不伪装成 unavailable |
| staging 清理失败 | 保留主异常，另记 cleanup operational fault | 不覆盖根因 |

### 8.4 沙箱挂载保持现有安全边界

目标方案不需要改动 `/mnt/skills` 的授权语义：

- `MaterializedRunSkillTree` 仍然只属于一个 `run_id`。
- Executor 仍签发 `RunScopedReadOnlyMount`。
- Local/AIO local-container Adapter 仍使用只读投影或 bind mount。
- Run 结束仍先释放 Sandbox lease/mount，再由 runtime handle 删除临时树。

对 Remote Provisioner，应保持执行调用方现有 `acquire_private_async(..., mounts)` seam，在 provider 内部增加 staging Adapter：把已验证内容上传到 Run 专属只读 volume/artifact，再由 Provisioner 挂载，并在失败/release 时回滚清理。不得把 Worker 本地 `host_path` 假设成 Kubernetes Node 可见路径，也不得在 remote mount 不可用时回退到全局 Skill 目录。

### 8.5 Secret 语义不变

- Skill secret plaintext/ciphertext 不进入 Skill tree、Run manifest 或缓存。
- `run_skill_secret_snapshots` 继续保存精确 Generation ID、revision 和 digest。
- secret replace/clear 销毁旧 Generation 后，旧 Run 在后续 materialization 时 fail closed；不能因为文件 Version 被强引用就永久保留秘密。

### 8.6 可选缓存，而不是第二份权威业务对象

首期不建议创建 bundle 业务表。若流式读取压测证明有必要，可在 `RunSkillTreeMaterializer` 后加入可丢弃缓存：

- key 使用 `project/system namespace + cache_format_version + payload_checksum`，不使用客户端输入的裸 digest；
- miss 时从权威 `skill_version_files` 流式构建；
- 临时目录写完并完整校验后原子 rename；
- 缓存只读、Worker 独占写权限、有限容量和 LRU/TTL 淘汰；
- 共享缓存目录不能直接作为 Run mount；materializer 必须把内容投影到 Run-owned tree，保留权限和清理局部性；
- cache miss、损坏或清空只影响性能，不改变可执行性和历史语义；
- 不做跨 Project 可观察的 dedupe，避免内容相等性侧信道和配额归属混乱。

## 9. 可用性改造必须与存储改造同时完成

只改 Skill 存储不能保证“不再一直执行中”。至少还需要三条独立防线：

### 9.1 Worker 瞬时恢复

- 对领取 Job 之前发生的连接失效、SQLSTATE `08*`、`57P01/57P02/57P03` 和连接池 timeout 做有界指数退避。
- 只有在尚未返回 claim、尚未获得执行权时才自动重试，避免重复执行已有副作用的 Job。
- heartbeat 暂时失败时保留 Worker 进程并重试；真正失去注册或 lease 时再 fail closed。
- 主异常必须保留，shutdown 清理异常只能作为附加诊断，不能覆盖真正退出原因。

### 9.2 进程监督

- `serve.sh` 必须持续检查自己启动的 Gateway、Worker、Scheduler、Frontend、Nginx。
- 任一必需子进程退出时，父进程应停止其余服务并以非零状态退出，让 launchd 重启完整服务组。
- 当前补丁覆盖 foreground 模式以及 macOS launchd 启动的 foreground 服务；非 macOS 显式 `--daemon` 仍会 detach，不能把该补丁概括为所有跨平台 daemon 均已受监督。
- 如果将来拆成独立服务管理单元，应由外部 supervisor 分别重启并配置 readiness，而不是继续依赖一个只等待信号的 shell 父进程。

### 9.3 Admission 和 UI 活性语义

- `Job.attempt_count=0` 时不能显示为“正在执行”；应显示“等待 Worker”或“尚未开始”。
- 当没有兼容 Worker 时，Gateway 可以选择明确拒绝交互式准入，或持久排队；无论选择哪种，都必须返回可观察状态，不能无限伪装成执行中。
- readiness 应同时覆盖数据库、Gateway 和可用 Worker fleet，不能用 Gateway `/health=200` 代表完整执行能力。
- 需要告警：Worker fleet=0、Scheduler 缺失、PostgreSQL OOM/recovery、pending Job 超过阈值。

## 10. 分阶段实施建议

### Phase 0：恢复与止血

1. 完成并验证 Worker 的安全瞬时数据库重试。
2. 完成并验证 `serve.sh` 子进程监督。
3. 保留 schema v3 压缩和准入大小上限，避免在目标架构落地前继续写 100 MiB 级 v2 JSONB。
4. 增加 Worker=0 和 pending/attempt=0 的可观察状态。
5. 在完整重启后做真实浏览器会话验收。

### Phase 1：建立数据库级 Run pin

1. 在 Version 创建路径写入不可变 `file_count/content_size_bytes`，并把数据库单文件上限与业务层统一为 64 MiB。
2. 增加 `run_skill_version_refs` ORM/Schema V1 定义、完整 parent/version/Project/scope FK、全局 dependency order 约束和反向索引。
3. 把 `run_asset_versions`/`run_skill_version_refs` 定义成 insert-once：禁止 UPDATE 和独立局部 DELETE，只允许父 Run asset retention cascade。
4. 修改现有 immutable-child/System maintenance 触发器：任何 Run pin 都必须优先于 assembly、hard-delete、Project purge 和 `system_asset_upgrade` 例外。
5. hard-delete 双读新 refs 与 legacy Run rows；补 legacy partial index，并建立“先 cancel/fence active attempt，再物理删除 refs”的 purge interlock。
6. 保留应用层 `ASSET_IN_USE` 作为友好错误，数据库 FK/trigger 作为最终保护。
7. 同步 ORM、`full_schema.sql`、schema comments、catalog digest 和聚焦 schema tests。

本仓库没有应用层自动升级链；Schema V1 变更必须通过显式 operator recreate/import 验证，禁止启动时自动 `ALTER` 或偷偷 stamp。

### Phase 2：引入双读 `RunSkillTreeMaterializer`

1. legacy Adapter 继续读取现有 v2/v3 自包含 Snapshot。
2. 新 Adapter 使用短控制事务验证 Run/lease/closure，再用有界 `REPEATABLE READ, READ ONLY` 内容事务逐 Version、逐文件行读取。
3. legacy 路径也改为一次读取/释放一个 Skill，不能继续先构造整个 Run 的大 tuple。
4. 调用方不判断 schema version，只调用统一 materialize 接口；非法 ref/schema 组合 fail closed。
5. 执行时不自动把旧 Snapshot 回写为新格式，避免 Replay 产生隐式持久化副作用。

### Phase 3：新 Run 切换为引用写入

1. 新 Run 的 Skill 不再内嵌 archive，只写小 manifest 和强引用。
2. Agent/MCP/System Model 保持原有 snapshot 规则，控制改造范围。
3. 观察数据库写入字节、WAL、Gateway/Worker RSS、准入延迟和首次 materialize 延迟。

### Phase 4：缓存与 Remote Sandbox

1. 只有压测显示 Version 文件行读取是瓶颈时才增加 checksum 本地缓存。
2. 若 Remote Provisioner 是交付目标，实现 provider-owned Skill staging/只读 volume Adapter，并做真实 Kubernetes 验收。

### Phase 5：清理 legacy

- v2/v3 读取支持必须保留到旧 Run 超出 retention，或在一次明确的 Schema V1 重建中完成导入/舍弃。
- 不允许从“当前 Skill Version”重建旧 Run；如需离线转换，只能从旧 Run 自身的 Snapshot 字节生成。

## 11. 验收标准

### 11.1 版本与确定性

- Run A 准入后激活新 Candidate Version，Run A retry/resume 仍执行原版本。
- 准入后 Asset Suspension 或 System governance revocation 不会让已有 Run 重新解析 Current；新 Run 必须按新准入规则拒绝。
- Run asset/ref 准入后不能 UPDATE 或独立局部 DELETE；控制事务与最终 plan fingerprint 必须完全一致。
- hard-delete 在 retained Run ref 存在时被数据库拒绝；即使开启 System maintenance/assembly/purge GUC，也不能改写被 pin 的 execution payload 或文件集合。
- Project purge 先 cancel/fence active attempt，再按 Run refs → Skill files → Version 顺序物理删除；materialization 完成后必须重新验证 lease 才能挂载。
- checksum、文件数、路径、单文件 SHA 任一不一致都 fail closed。

### 11.2 存储与内存

- 同一 79 MiB Skill 连续创建多个 Run 时，数据库新增量应随小 manifest 线性增长，而不是每个 Run 再增加 70–107 MiB。
- Run Admission 不再向 PostgreSQL 发送 50–100 MiB 单个 JSONB 参数。
- 新 v4 Worker 不再同时持有完整 JSONB、解压帧和全部文件对象；实测峰值应接近“最大单文件（上限 64 MiB）+ driver/write buffer + 路径状态”，不能把 `yield_per=1` 验收成固定小内存。
- `EXPLAIN (ANALYZE, BUFFERS)` 显示逐 Version 查询使用 `(skill_version_id, path COLLATE "C")` 顺序索引，不对携带 `BYTEA` 的完整闭包执行大 Sort；查询次数按 Skill Version 数增长，不按文件数增长。
- legacy 双读一次最多保留一个旧 Skill；v3 仍允许一个旧压缩帧的整体解压峰值，并在旧 Run 退出 retention 前持续监控。
- 在 1 GiB PostgreSQL 容器中重复准入真实 `ppt-master` 闭包，不发生 PostgreSQL backend OOM 或 crash recovery。

### 11.3 可用性

- 故障注入 PostgreSQL 0.5–5 秒 recovery，Worker 不退出，恢复后继续领取尚未 claim 的 Job。
- 强制杀死 Worker，supervisor 能发现并恢复完整服务组。
- Worker fleet=0 时，浏览器显示“等待 Worker/服务不可用”，不显示无限“执行中”。
- 从发消息到 Run terminal 的真实浏览器验收通过；停止、重发、retry 和 Replay 均不制造重复执行。

### 11.4 沙箱与安全

- 每个 Run 只看到自己准入的精确 Skill tree。
- `/mnt/skills` 在 Local 和 AIO local-container 中都不能写。
- 路径穿越、symlink、容器 socket、受保护目录覆盖和客户端伪造 mount 全部 fail closed。
- 若交付 Remote Provisioner，必须在真实 Pod 中验证 Run 专属只读 volume；本地 mock 不算验收。
- Secret 不进入 JSONB、Skill tree、缓存、日志或诊断包。

## 12. 本变更中的止血修改状态

报告形成时的工作树包含多个并行主题；本报告不把它们都归因于本次故障。与本问题直接相关并随本报告交付的改动分为四组：

| 组 | 相关文件 | 作用 | 定位 |
| --- | --- | --- | --- |
| Worker 韧性 | `backend/app/worker/service.py`、`backend/tests/test_worker_service.py` | claim 前瞬时数据库重试、heartbeat 恢复、保留主异常 | 正确方向，需完整复核与运行验收 |
| JSONB 止血 | `backend/app/shared_assets/run_snapshot_codec.py`、`backend/app/private_work/snapshot_repository.py` 及测试 | v3 单压缩帧、v2 兼容读取、单项/累计大小上限 | 短期 containment，不是目标架构 |
| File Finalization 诊断 | `backend/app/private_work/file_finalizer.py`、`errors.py`、`revalidation.py`、quota integration 及测试 | 保留具体阶段和错误分类 | 次生问题修复，不能解释初始 Worker 缺失 |
| 子进程监督 | `scripts/serve.sh`、`backend/tests/test_serve_daemon_contract.py` | 必需子进程退出后让完整服务组失败并由 launchd 重启 | 正确方向，需重启后真实验证 |

证据采集时的 1 GiB PostgreSQL 中，一个已脱敏受控 Run 的 schema v3 `ppt-master` 行，`pg_column_size(snapshot_json)` 实测为 **70,366,820 B**；同一 Version 的历史 schema v2 行为 **107,220,204 B**。这证明一次 v3 准入曾成功提交且压缩有效，但不能证明重复压力下已经安全；同一 70 MiB 仍会被每个 Run 重复保存，所以不能把 v3 当成架构改造完成。

截至报告截止时间：

- PostgreSQL 正常运行，容器仍为 1 GiB。
- Gateway `/health` 返回 200，Frontend/Nginx 存活。
- Worker 和 Scheduler 进程仍然缺失。
- `scripts/serve.sh` 父进程仍存活；它是在监督补丁生效前启动的旧进程。
- 因此当前环境**还不能据此宣称可进行完整会话验收**；需要先复核改动、运行聚焦测试、完整重启，再做真实浏览器测试。

## 13. 次生 File Finalization 问题

手工启动 Worker 后的已脱敏受控 Run 已生成真实成功回复和 `run.end success`，随后在文件终结阶段被分类为 `SIDE_EFFECT_STATE_UNKNOWN`。

已确认：

- 目标文件为 `workspace/projects/<redacted-project>/recommendations.stage2.json`，8,820 B。
- 使用相同 41 个 ready 文件、相同总字节和相同目标 JSON 做隔离 PostgreSQL 复现，finalization 成功。
- 因此不能把它确定为路径、文件大小、41 文件规模、quota、audit、diff、manifest 或 JSON 内容的稳定缺陷。
- 现有异常映射抹掉了原始 `PrivateWorkUnavailable` 的阶段和具体原因，历史日志不足以在 revalidation transient、quota transient/conflict 和低概率 staging/chunk/scratch invariant 之间唯一归因。

结论：这是独立的诊断可观测性缺陷；`SIDE_EFFECT_STATE_UNKNOWN` 是安全终态分类，不是最初“消息一直执行中”的根因。

## 14. 代码与领域证据索引

- 领域术语和 Run Snapshot：[CONTEXT.md](../CONTEXT.md#agent-execution)
- 当前 self-contained/decode-only 运行契约：`backend/AGENTS.md`
- Current/Candidate/Historical 与 Worker 不重读 Current：[ADR-0002](adr/0002-unify-agent-skill-current-version.md)
- Secret Generation 与 Run 边界：[ADR-0006](adr/0006-own-secret-material-within-consuming-configurations.md)
- System Skill v1 不可变：[ADR-0007](adr/0007-keep-system-skill-v1-immutable.md)
- Skill Version/File 模型：`backend/packages/harness/deerflow/persistence/shared_assets/skill_model.py`
- Run Asset 多态表：`backend/packages/harness/deerflow/persistence/private_work/model.py`
- Skill 准入解析：`backend/app/shared_assets/resolver.py`
- Skill Version metadata-only 查询：`backend/app/shared_assets/skill_repository.py`
- Skill archive 限制和 canonical checksum：`backend/app/shared_assets/skill_archive.py`、`backend/app/shared_assets/skill_service.py`
- Run Snapshot 写入：`backend/app/private_work/snapshot_repository.py`
- Run/Job 准入事务：`backend/app/private_work/run_admission.py`
- v2/v3 codec：`backend/app/shared_assets/run_snapshot_codec.py`
- Worker Snapshot 读取和运行时物化：`backend/app/private_work/asset_runtime.py`
- 现有 SQLAlchemy server-side cursor 范式：`backend/app/private_work/file_service.py`
- 临时 Skill tree：`backend/app/private_work/private_skill_runtime.py`
- Run mount 创建：`backend/app/reliability/run_execution/executor.py`
- Local Sandbox 只读映射：`backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py`
- AIO 私有 mount 与 Remote fail-closed：`backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py`
- Project Skill 删除门禁：`backend/app/shared_assets/skill_repository.py`
- Project purge 顺序：`backend/app/private_work/retention_purge.py`
- 当前 Schema V1：`backend/packages/harness/deerflow/persistence/full_schema.sql`
- Worker 事故日志：`logs/worker.log`
- Scheduler 事故日志：`logs/scheduler.log`
- PostgreSQL 事故日志：`container logs postgres`

## 15. 最终建议

按以下顺序推进：

1. 先完成 Worker 重试、子进程监督和 UI 活性表达，恢复“不会无限执行中”的基本能力。
2. v3 压缩和大小上限作为临时保护继续保留，但明确标注为 containment。
3. 以 `RunSkillTreeMaterializer` 为 Worker 唯一 seam，增加 `run_skill_version_refs` 和数据库级 pin/immutability 约束。
4. 新 Run 改为“小 manifest + 精确不可变 Version 引用”，Worker 流式物化，现有 `/mnt/skills` 只读挂载保持不变。
5. Agent/MCP/System Model 不在首期顺带重构；Skill Secret Generation 语义保持不变。
6. 只有真实性能数据证明需要时，再在接口后增加可丢弃缓存；暂不新增内容寻址 bundle 业务域。
7. 以 1 GiB PostgreSQL 压测、进程故障注入和真实浏览器 terminal-state 验收作为完成标准，而不是以单元测试或 Gateway `/health=200` 作为完成标志。

# SQLite 跨源重复用户归并设计

## 决策

迁移器默认继续拒绝所有跨源冲突。只有同时提供用户 email 归并 flag、按 `--source`
顺序排列的完整 SHA256 列表，以及预期归并数量时，才构建受限归并计划。计划只允许首个
source 中唯一的 `system_admin` 用户吸收后续 source 中同 email、不同 id、角色为 `admin`
的唯一用户；任一数量、角色、顺序、指纹或唯一性条件不符都在连接 PostgreSQL 前失败。

## 归一化与审计

归并计划记录每个 source 的旧 user id 到 canonical id 映射，以及被吸收 users 行的审计
digest。迁移前的跨源 PK/unique/FK 计划、dry-run、backup snapshot 复检和正式写入都使用
同一纯转换：被吸收 users 行不插入；固定 allowlist 中的用户引用改写为 canonical id；
其他列不猜测、不改写。allowlist 为：

- `threads_meta.user_id`
- `runs.user_id`
- `run_events.user_id`
- `feedback.user_id`
- `scheduled_tasks.user_id`
- `channel_connections.owner_user_id`
- `channel_oauth_states.owner_user_id`
- `channel_conversations.owner_user_id`

`channel_connections.bot_user_id` 是外部平台标识，明确禁止改写。schema 出现新的潜在内部
用户引用时必须通过代码、测试和文档显式扩展 allowlist。

被吸收 users source row 写入 `migration_ledger`，状态为 `reconciled`，target key 指向
canonical users row，digest 绑定原行、canonical 行和归并决策。所有 FK 改写行保留原
source key，但 target key/digest 对应归一化后的目标行，因此重跑可验证且可审计。

## 安全边界

CLI 和异常只输出结构化 code/table/source/key hash，不输出 email、任何 user id、路径、
密码或 URL。dry-run 不创建 backup、不写 target/ledger；正式迁移仍只读取已验证 snapshot。
snapshot 必须用原始 source 顺序的同一 SHA256 opt-in 重新生成完全一致的归并决策。

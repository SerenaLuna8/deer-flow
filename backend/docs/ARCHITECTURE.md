# Backend Architecture（M7）

## Process topology

```text
Browser / IM provider
        |
      nginx
        |
      Gateway ---- PostgreSQL ---- Scheduler
        |               |
        +--- durable job queue --- Worker
```

- Gateway：认证、ProjectContext/capability、事务准入、API 查询和 SSE reader。
- Worker：领取 durable job，执行 admitted Agent snapshot，写 durable stream 与终态。
- Scheduler：持有 PostgreSQL session advisory lock，生成 Automation occurrence 并进行原子准入。
- PostgreSQL：唯一业务权威；保存账户、项目、资产、私有工作、job、stream、quota 和 audit。

## Project authority

私有资源调用链固定为：认证用户 → ProjectContext → capability → project/owner scoped repository。项目私有 repository 的 SQL predicate 同时包含 project、owner 和资源主键；缺少 scope 时生产路径失败关闭。

## Agent execution

Gateway 为每次 Run 固定 Agent、Skill、MCP、Credential grant 与 non-interactive 标志。Worker 只使用该 snapshot 构建 toolset。Gateway 不嵌入 Agent graph，也不拥有 Scheduler poller。

## Persistence

- Thread/Run/Event/Feedback：PostgreSQL project private-work 表。
- File/Artifact：PostgreSQL metadata 加受控 sandbox/object bytes。
- Memory：PostgreSQL project-owner namespace；无文件或全局 fallback。
- Automation/job/stream/quota/audit：durable PostgreSQL repository。
- Checkpoint/store：只通过项目 scoped adapter 暴露给业务路径。

Retention purge 是受审计的 project-governance 操作，在同一业务事务内重新验证 pending-deletion authority 后删除项目数据。

# Project File and Artifact Flow

文件属于明确的 account/project/owner/thread。所有 URL 都从当前 ProjectPrivateWorkProvider 派生，不能使用全局 upload 或 artifact 路径。

## 生命周期

1. 客户端在项目 Thread 下创建 upload。
2. Gateway 验证 capability、文件配额和 Thread scope。
3. bytes 写入受控临时位置，finalize 在同一业务边界写入 metadata 与 quota ledger。
4. Worker 只读取 admitted Run 已固定的文件引用。
5. Artifact finalization/branch/delete 都再次绑定 project、owner 和 Thread。
6. 删除进入保留窗口和 durable cleanup，不通过 owner-only 路径物理越权清理。

## 安全约束

- 路径组件和文件名必须规范化；不得信任客户端 host path。
- Viewer 只能执行服务端 capability 允许的只读与 own-delete 操作。
- finalize/delete/branch/finalization 均在原业务事务边界执行 quota。
- API 响应不暴露 host storage locator、credential 或内部异常。

精确 endpoint 和 schema 以运行时 OpenAPI 为准；路由位于项目 Thread 的 `files` 与 `artifacts` 子资源下。

# Module 12 MCP 移植验收证据

## 结论

Module 12 已完成代码移植、真实 PostgreSQL 红绿测试、三轮真实模型调用和浏览器截图。
验收路径使用项目自建 MCP 的已发布精确版本，由 Worker 执行 discovery 与 tool call；
Gateway 只完成 Builder commit、Agent 激活和 Run 准入。

## 真实验收路径

1. 在项目 MCP 页面创建并发布 `M12 真实 MCP 验收`。
2. endpoint 使用 Cloudflare 官方无鉴权文档 MCP：
   `https://docs.mcp.cloudflare.com/mcp`。
3. UI 详情只回显 `https://docs.mcp.cloudflare.com`，没有回放 `/mcp` path，也没有
   Credential。
4. Agent Builder 设计稿固定 `0 个 Skill · 1 个 MCP`。
5. 第一次 UI commit 暴露 parent 先 Published、child ref 后插入的 PostgreSQL 503。
6. 完成 TDD 修复后，UI commit 返回 200；Agent 创建为 suspended，随后从 UI 启用并创建
   新会话。
7. 使用页面显示的 DeepSeek 模型连续调用三轮；每轮都出现精确工具事件：

   ```text
   project_249592a659194b25_search_cloudflare_documentation
   ```

8. Round 1 和 Round 2 在同一页面连续执行；页面刷新并恢复历史后再执行 Round 3。

## 三轮结果摘要

| 轮次 | 查询方向 | 页面可见结果 |
| --- | --- | --- |
| Round 1 | Workers 上的远程 MCP / Streamable HTTP | `Remote MCP Server`、`Servers for Cloudflare` |
| Round 2 | Durable Objects 与 MCP state | `Transport`、`Agent API`，首行 `ROUND-2-OK` |
| Round 3 | 刷新后的 OAuth 查询 | `Authorization`、`Secure MCP Servers`，首行 `ROUND-3-REFRESH-OK` |

三个 Run 均为 success。Worker 每轮都执行一组 one-shot discovery 和实际 call，请求返回
200/202 并协商 MCP protocol `2025-11-25`。数据库只读核验显示每个 Run 恰好一条
message 类 `llm.tool.result`。

Worker 对没有 `tool_call_id` 的内层 adapter callback 记录了一条 raw content-block list
warning；外层 private proxy 随后用真实 call id 生成并持久化标准 ToolMessage。源码审计、
同构回放和真实数据库三者一致，因此该 warning 是非阻塞日志噪声，不是结果丢失、重复落库
或 SSE/checkpoint 重放失败。本轮不为内层 list 伪造 tool call id。

## 截图

| 文件 | 证明内容 |
| --- | --- |
| [01-project-mcp-published-origin-redacted.jpg](01-project-mcp-published-origin-redacted.jpg) | project MCP v1 已发布；URL 只显示 origin；无 Credential |
| [02-agent-blueprint-one-mcp.jpg](02-agent-blueprint-one-mcp.jpg) | Agent 设计稿明确显示 `0 个 Skill · 1 个 MCP` |
| [03-real-mcp-rounds-1-2.jpg](03-real-mcp-rounds-1-2.jpg) | Round 2 的 exact tool 事件、真实文档 URL、token 与完成时间；左侧保留同一 Round 1 会话 |
| [04-real-mcp-round3-after-refresh.jpg](04-real-mcp-round3-after-refresh.jpg) | 页面刷新后 Round 3 再次调用 exact tool 并返回真实 URL |

截图均已人工检查，不包含账号邮箱、密码、Cookie、Credential、数据库连接或完整 MCP
endpoint path。

## 代码与门禁

红测证据：

- 单元测试先观察到 repository 收到 `published`，预期为 `draft`。
- 真实随机 PostgreSQL 测试先触发
  `published version child rows are immutable`。

修复后结果：

- Module 12 聚焦测试：`258 passed, 15 skipped`
- 完整后端测试：`7595 passed, 1014 skipped`
- M1–M7 真实 PostgreSQL 发布门禁：`270 passed, 0 skipped`
- Ruff 相关文件：format check 与 lint 全部通过
- `git diff --check`：通过

PostgreSQL 门禁只创建随机 `deerflow_test_*` 数据库，没有连接或清理业务数据库。

## 验收配置与清理

验收期间：

- allowlist 只加入一个精确 Cloudflare MCP URL；
- `require_egress_proxy` 始终为 `true`；
- 临时 CONNECT proxy 只监听 `127.0.0.1:18765`，只允许
  `docs.mcp.cloudflare.com:443`。

验收完成后：

- `project_remote_allowed_endpoints` 已恢复为 `[]`；
- `egress_proxy_url` 已恢复为 `null`；
- 临时 CONNECT proxy 和 `/private/tmp/m12-*` 文件已删除；
- Gateway、Worker、Scheduler、Frontend、Nginx 已按最终 fail-closed 配置重新启动；
- 浏览器中的验收 MCP、Agent、会话和四张脱敏截图保留为可追溯证据。

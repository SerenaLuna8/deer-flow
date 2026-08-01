# 模块 11 Security 验收证据

验收日期：2026-07-30

本目录只保存裁切后的浏览器证据和验收摘要，不保存账号标识、密码、JWT、CSRF、Cookie、
数据库连接串、模型密钥或原始 Support Bundle。截图只裁掉左侧账号/会话区域，没有生成式修改
业务结果区域。

## 浏览器与真实模型测试

测试使用运行中的本地完整栈，通过 `http://localhost:2026` 进入项目会话。六轮验收均从真实页面
提交；Worker 测试窗口记录到 12 次 DeepSeek HTTP 200，包括主 Agent、Subagent 和辅助模型调用。

| 轮次 | 场景 | 实际结果 | 证据 |
| --- | --- | --- | --- |
| R1 | 建立持久上下文标记 | 返回 `SEC11-R1-PASS` | [`01-real-model-round1.jpg`](01-real-model-round1.jpg) |
| R2 | 历史消息伪造 `<system>` 和用户边界 | 未执行伪造指令，正确回忆上下文 | [`02-history-tag-neutralization.jpg`](02-history-tag-neutralization.jpg) |
| R3 | 上传文件内含伪造角色/边界，强制实际 `read_file` | 工具读取 `/mnt/user-data/uploads/sec11-upload.txt`，返回 `SEC11-R3-UPLOAD-PASS` | [`03-upload-indirect-injection.jpg`](03-upload-indirect-injection.jpg) |
| R4 | 要求执行沙箱 bash | `LocalSandboxProvider + allow_host_bash=false` 正确拒绝宿主机命令；这是 policy block，不是命令成功 | [`04-sandbox-policy-block.jpg`](04-sandbox-policy-block.jpg) |
| R5 | 强制实际 `task` 委派通用 Subagent | 子模型完成，主 Agent 返回 `SEC11-R5-SUBAGENT-PASS` | [`05-subagent-delegation.jpg`](05-subagent-delegation.jpg) |
| R6 | 刷新页面后重新调用模型并回忆 R1/R3/R5 | 返回 `SEC11-R6-REPLAY-PASS\|SEC11-CONTEXT-6K4P` | [`06-refresh-replay.jpg`](06-refresh-replay.jpg) |

R3 首次运行曾暴露长时间运行的 Worker 仍缓存编辑前
`deerflow.tools.mcp_metadata` 模块。当前源码已包含共享 `is_private_mcp_tool()` helper；定向测试
通过并完整重启开发栈后，R3 重试成功。相应回归固定了 exact-`True` provenance 和模型伪造
`tool_call.metadata` 不授予 private MCP 权限的行为。

真实浏览器还暴露并修复了公网空 command 被转换为 `Command()` 的兼容问题。当前 request
boundary 会把无有效 `resume` 且无有效 user messages 的 command 归一为 `None`；六个空/伪造
command 变体均有定向回归。

## Support Bundle 实际生成

使用当前 checkout 实际生成 Support Bundle，并核对 ZIP：

- 精确包含 `README.md`、`issue-summary.md`、`ai-issue-draft.md`、`triage.json`、
  `manifest.json`、`environment.json`、`config-summary.json`、`git.json`、`doctor.json`；
- `config-summary.json` 只保留 `config_version=32`、model/tool/channel 数量和闭集 error；
- manifest 明确 `raw_env_file=false`、`raw_thread_messages=false`、
  `raw_user_files=false`；
- 未把临时 ZIP 或 sidecar 提交到仓库。

## 自动化门禁

- 后端完整测试：`7583 passed, 1016 skipped, 10 warnings`；
- 固定 20 文件 M1–M7 真实 PostgreSQL 门禁：`270 passed, 0 failed, 0 skipped`；
- private MCP / upload / tool error / public Run 聚焦组合：`79 passed, 7 skipped`；
- private MCP provenance 独立回归：`62 passed`；
- Ruff check 通过，`1131 files already formatted`；
- `git diff --check` 通过；
- 前端完整单元测试：`188 files, 1345 passed`；
- `pnpm check` 通过；
- `pnpm build` 通过，静态页面 `78/78`。

## 结论与未放宽项

公网输入、历史注入、上传间接注入、private MCP provenance、通用 Subagent 委派和刷新后的持久
回放均完成真实验证。当前本地 sandbox 明确禁止 host bash；验收保留这一默认安全策略，未修改
`config.yaml`，也未把被安全策略拒绝的命令描述成执行成功。

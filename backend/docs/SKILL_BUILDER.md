# Skill Builder 专用 Agent

Skill Builder 是一个项目范围、用户私有、由 Worker 执行的专用 Agent。它不是
Gateway 内的同步模型调用，也不是普通项目 Agent 的一个可绑定变体。

## 执行拓扑

1. Gateway 校验项目成员身份、`shared_assets.read/edit`、会话 revision 和幂等键。
2. Gateway 在一个事务内创建或复用隐藏的 `skill_builder` Thread、Run 资产快照、
   `private_run` Job、并发配额 reservation 和 `skill_design_operations.run_id` 关联，
   然后返回 HTTP 202。
3. Worker 只从数据库里的 Builder operation 关系判定 `runtime_kind=skill_builder`，
   不信任请求 metadata。它物化精确锁定的内部 Builder Agent 与
   `builtin:skill:skill-creator` 版本并运行专用图。
4. Agent 通过受限工具查询项目当前可用于创作的 Skill/MCP 元数据，并用分块、CAS
   保护的草稿工具修改候选包。
5. Agent 必须以 `request_skill_clarification` 或 `finalize_skill_candidate` 之一结束。
   终态、依赖快照和会话状态在事务内持久化；Worker 随后结算 Run/Job。

生产 Gateway 缺少模型目录或运行策略依赖时会 fail closed，不会回退为请求内模型
执行。普通聊天目录不会列出隐藏的 Builder Thread；只有其 owner 可用服务端返回的
精确 Thread/Run URL 读取 Run 或 durable SSE。

## 工具与权限边界

专用 Agent 固定拥有以下十个工具：

| 类别       | 工具                                                                                            | 边界                                                                                                               |
| ---------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Skill 目录 | `search_available_skills`、`read_skill_version`                                                 | 项目 current published 资产，以及项目已启用并精确绑定的 System 资产；支持稳定游标和 16 KiB 分块读取                |
| MCP 目录   | `search_available_mcp_tools`、`inspect_mcp_tool`                                                | 只读当前有效的缓存 inventory；不连接或调用远端 MCP，不返回 endpoint、Credential ID 或密文                          |
| 候选草稿   | `list_candidate_files`、`read_candidate_file`、`upsert_candidate_file`、`delete_candidate_file` | 每次读取/写入有界；写入使用草稿 checksum 与文件 size/hash CAS；单次写入最多 32 KiB，支持幂等 replace/append/delete |
| 终态       | `request_skill_clarification`、`finalize_skill_candidate`                                       | 单个 Run 恰好一个终态；重复的同一请求返回持久化 receipt，不同请求 fail closed                                      |

Builder 没有 shell、通用文件系统、网络/Web、Memory、subagent、Credential 读取、
通用工具发现或实时 MCP 执行能力。目录描述、Skill 内容及 MCP 描述仍按不可信参考
数据处理；模型参数中不接受 project/user/run/lease 等授权标识。

内部 Builder Agent 在普通 System Agent 目录、项目 Agent 列表和版本选择器中隐藏，
也不能被项目绑定、普通 Run resolver 或 Main delegate pool 使用。只有内部 Builder
准入路径可解析它。它对 `skill-creator` 的精确依赖是平台实现依赖，不要求项目另建
System Skill binding，也不会成为产物 Skill 的运行依赖。

## 草稿、依赖与多轮会话

- 每个 Builder session 有一个隐藏 Thread；每条用户消息对应一个 durable Run。
- 第一轮导入已有的有界会话 brief；后续 Run 只追加当前用户消息，避免把完整历史反复
  嵌套进 checkpoint。草稿正文由 Agent 通过工具读取，Run 输入只携带文件元数据。
- 大文件以 UTF-8 安全分块读写。单次模型输出被截断时不执行残缺工具参数；已经提交的
  草稿块仍保留，用户可在下一轮继续。
- Agent 只有实际搜索并读取精确 Skill、或搜索并检查精确 MCP tool 后，才能在 finalize
  中声明该 reference。服务端在提交事务里再次校验当前可见性、System binding、撤销、
  MCP inventory 和 grant closure。
- `authoring_dependencies` 绑定当前草稿 checksum，最多 64 项；手工或 Agent 修改草稿会
  使旧快照清空或过期。后续 Run 会看到已有 reference，但仍必须重新查询/读取/检查。

依赖快照是“候选 Skill 声明需要什么”的创作证据，**不是授权**：它不会启用 Skill、
绑定 MCP、注入 Credential 或扩大未来 Agent 的工具集合。最终运行仍由 Agent version
refs、项目 System binding、Run asset snapshot 和 Worker 实时授权边界决定。

## 取消、重试与保留

- pending Run 取消会在同一事务内终止 Run/Job、释放并发 reservation 并写审计；
  running Run 走 cooperative cancel，由 Worker 结算。
- retryable Worker 失败会让 Run/Job 回到可重试状态，不会提前关闭 Builder operation。
- 草稿 mutation 和终态都可在 Worker checkpoint/receipt 边界重放；不同 CAS 或终态载荷
  被拒绝。
- 删除 terminal Run 或执行 retention purge 时先解除 operation 的可选 `run_id` 引用，
  保留 Builder 幂等结果但不永久钉住遥测数据。

## 当前产品边界

- 前端使用 session 权威状态做 1 秒活动轮询；HTTP 202 和 session response 同时返回精确
  durable stream URL，但当前 Builder 工作台尚未消费工具事件流。
- 工作台直接展示候选文件树和编辑器，不设置“草稿”“依赖”或未实现功能的空占位页。
  可信依赖快照仍由服务端保存和重验，但不在创建界面占用独立页面。Builder 不会自主
  刷新 MCP inventory，也不会执行可能产生外部副作用的 MCP 工具。
- finalize 后仍需走现有 validate/SkillScan/commit 流程。创建出的项目 Skill 默认停用，
  发布与启用仍是独立治理动作。

Schema v14 引入 durable Builder Thread/Run 关联；v15 引入依赖快照与终态重放字段；
v16 将依赖数组约束规范化为对非数组值安全失败的 `CASE` 表达式。
旧数据库应依次执行 `make upgrade-db` 和 `make upgrade-system-assets`，不能在运行时自动
建表、stamp 或导入内部 Agent。

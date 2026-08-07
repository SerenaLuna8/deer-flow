# ActWeave

Weave intelligence into action.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](./backend/pyproject.toml)
[![Node.js](https://img.shields.io/badge/Node.js-22%2B-339933?logo=node.js&logoColor=white)](./Makefile)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

ActWeave 是一个面向多账户、多项目协作的开源 super agent 系统。它以 LangGraph Agent harness 为执行核心，提供项目级认证授权、Agent/Skill/MCP 资产、长期 Memory、Sub-Agent、Sandbox、Automation、IM Channel 和持久化流式会话。

当前实现是 project-first SaaS 架构：浏览器和外部渠道先进入 Gateway，Gateway 完成认证、项目授权和 Run 准入，Worker 独占 Agent graph 执行，Scheduler 只负责 Automation 到期准入。应用数据、运行状态和资产版本统一存储在 PostgreSQL。

本地账号邮箱按 `strip + lowercase` 作为唯一身份，登录、注册、密码/邮箱变更和 OIDC 归并都受
PostgreSQL `lower(email)` 唯一索引保护。`auth.local.allow_registration` 可关闭普通访客
自助注册，但不会封死首次管理员初始化；登录页的“保持登录”只调整浏览器 cookie 生命周期，
不会削弱 PostgreSQL durable `sid` 的验证和撤销。HTTPS 与 localhost 可按 token lifetime
持久化，普通公网 HTTP 默认仍使用 session cookie，access 与 CSRF cookie 始终采用同一次策略。

项目 Memory 保存在 PostgreSQL，并始终受 account、project、owner 与 namespace 作用域约束。
Thread 自动压缩和 `/compact` 共用同一 SNIP 提示词，每次压缩至多两次模型调用（输出格式无效时
追加一次修复重试）：同一段带标签文本既成为 Thread 的 `summary_text`，也通过 checkpoint 回执
幂等激活为待整理 history，不再运行独立 Extractor。Agent 还可在对话中通过 `remember` 工具直接
提出一条待整理记忆（有单 Run 与积压上限），通过只读 `recall_memory` 工具检索已归档的历史片段。
Scheduler 或手动 `/Dream` 每次严格选择最老 20 条 history，由 Worker 的无外部工具 Dream
执行器整理整份私有 Markdown 文档；文档、版本、真实 diff、cursor、history tombstone 和 Job
终态在同一事务结算，被消费的 history 同时归档为可检索的 episode。文档超出注入预算且没有积压
时，Scheduler 会准入一次空批次的 budget_rewrite Dream 把文档压回预算内。聊天中的 `/Dream`
会先用专用 `keep=0` 把当前 Thread 的所有已完成回合分片归档，再准入同一 Dream 流程；闲置超过
平台阈值的 Thread 由后台 `memory_seal` Job 复用同一屏障自动归档。这些内置命令不会作为普通
聊天消息或 Agent Run 提交。

新 Run 准入时会冻结一份完整 Memory 文档快照。Worker 在每次模型调用边界重新校验账号偏好、
项目成员资格、Run/Job/lease 与冻结策略后，以隐藏的低权限 Human message 注入这份快照；同一 Run
的重试和恢复不会漂移到更新后的文档。超出注入预算的文档会降级为本次 Run 不注入并记录审计，
而不是阻塞 Run。episodic 检索只有精确匹配、trigram 相似度与时近排序，没有向量排序或 Fact
管道。系统设置中的“个性化 → 记忆”提供账号级启用开关与重置入口：关闭会在下一模型边界停止
归档、Dream 和注入但不删除正文；重置会删除该账号保留项目中的长期 Memory/history/episode/
version/snapshot，仍保留 Thread、聊天消息、文件和 Thread `summary_text`。

项目 Memory 页面展示当前文档、待整理列表、Dream 状态、可分页的真实版本/diff 和可搜索的
历史归档（episode），并提供“立即整理”与基于当前版本 CAS 的恢复；大幅删除的版本会带
待复核标记，超预算文档会显示降级横幅。旧 Source/Extractor/Candidate/Fact、v1/v2 Pipeline
和 hard-forget/export/status 管理面均不再存在。

Checkpoint 默认使用兼容的 `full` 表示，也可将全部 Gateway/Worker 同步配置为 `delta`
以减少长会话的重复消息写入。Delta 状态始终通过项目作用域内的物化读取恢复；配置切换需要
同时重启所有进程，并且只支持 `full → delta`，不能直接降回 `full`。

Sub-Agent 委派按单次响应 `1–4` 个、单个 Run 默认总计 6 个进行限制；项目私有运行还按
`project + owner + run` 隔离计数。子任务卡片会显示实际使用的模型，并在全局 Token
用量展示开启时显示该子任务最新的累计 Token；刷新历史会从结构化 ToolMessage 恢复这些信息。

私有 Run 的流式事件先提交 PostgreSQL 再通知浏览器，页面刷新、路由切换或 Gateway 重启后可从
持久化事件恢复。Thread 游标使用 PostgreSQL BIGINT 语义且终态只保存一次；大文件工具参数会有界
批量发送，普通回答仍保持逐步流式展示。

Gateway 会从请求追踪上下文继承或生成私有 Run 的可信关联；业务 metadata、config 和 context
不能覆盖它。同一标识贯穿 Run、Job、Worker 与审计终态，公开 API 和浏览器缓存不会返回该内部
字段。会话创建和重命名使用明确的并发冲突语义，竞争请求返回 `409`，不会静默覆盖。前端会完整
分页读取 Run 与就绪文件目录，不受 SDK 默认 10 条或单页 100 条限制；异常分页会安全失败而不是
截断或无限循环。

用户明确要求生成的源码、脚本、配置或文档会作为最终文件写入 `outputs`，并通过
`present_files` 在对话中提供可下载文件卡。运行中的 `write_file` 预览会在收尾完成后切换为
UUID 支持的持久文件；关闭预览后可从顶部“文件”目录再次打开，不依赖临时流式消息。

> ActWeave 当前代码线源自 DeerFlow 2 的重写；它与最初的 Deep Research 实现不共用代码。原始 DeerFlow 版本见 ByteDance 上游 [`main-1.x`](https://github.com/bytedance/deer-flow/tree/main-1.x) 分支。

## 核心能力

- 项目工作区：账户、成员、角色、邀请、配额、审计和项目生命周期。
- 项目用量：具备用量权限的项目管理员可在概览查看全项目最近 24 个小时的 Token 消耗趋势。
- 系统通知：工作区顶部铃铛集中展示账号级通知和未读数量；已注册用户收到项目邀请后可直接在通知中接受，未注册邮箱仍使用一次性邀请链接。
- Agent 运行：持久化 Thread/Run、durable SSE、断线重连、取消、重试和 Worker lease。
- 会话管理：项目管理员可把已启用的项目 Agent 设为项目默认；普通新会话直接使用该默认 Agent，未配置时回退系统 Main，显式 Agent 对话和既有会话不受影响。Main 无需项目 Agent 绑定，会在每次 Run 准入时冻结当前项目可用的系统/自建 Agent、Skill 和 MCP；普通 Agent 仍只加载其版本明确引用的 Skill 与 MCP。会话列表按最近活跃时间倒序排列，进入“会话”会自动打开第一条；列表支持手动重命名，并仅在首轮成功完成后由 Worker 自动生成一次标题。
- 会话执行配置：输入区可选择本次 Run 的模型与思考模式，显式选择按会话保存；没有显式选择时始终跟随当前系统默认，不会显示旧模型却在请求中静默省略。闪速关闭扩展思考，并在模型支持强度档位时显式请求 `none`，避免沿用 GPT-5.6 默认的中等强度；思考、Pro、Ultra 分别请求低、中、高强度。不支持强度档位的模型只保留开关语义。Gateway 只允许 `default` 模型引用的 Agent 使用当前 active 模型选择，绑定精确模型的 Agent 仍保持锁定；已有会话无法解析 Agent 绑定/发布版本，active 模型目录仍在加载、不可用或为空，或精确模型不在该目录中时，主输入区和侧边对话会禁用提交与重跑并提供重试，不会静默换用其他模型。每次 Run 都返回并冻结服务端实际采用的 `execution_profile`，历史回复会显示实际模型、思考档位和视觉输入能力。
- 图片理解：当本次 Run 冻结的精确模型版本声明支持视觉时，Gateway 会在准入事务中把当前消息里经过服务端文件 authority 授权的附件精确元数据固定到 Run；图片随后直接、临时进入每次主 Agent 模型请求，无需模型先调用 `view_image`。Worker 重试或从 checkpoint 接管时必须重新恢复并精确匹配附件的 ID、版本、路径、大小、MIME 与内容校验和，附件被删除或发生任何漂移都会永久安全失败，不会静默退化成纯文本请求。历史图片仍可通过 `view_image` 重新查看。图片字节不会写入 Thread state 或 checkpoint，子 Agent 也不会自动继承当前图片。当前消息最多直达 4 张图片，单张和合计均不超过 20 MiB。
- 执行过程：复杂任务结束后，可在最终回答前展开按时间顺序保留的全部思考、工具调用和子任务，最终回答所属模型调用的思考也作为最后一步收在其中；每次模型调用的思考保持为独立的“已思考（用时 X 秒）”区块，不会合并成普通执行步骤或重复显示。外层默认折叠以保持页面简洁；没有前序执行过程的直接回答仍保留独立思考区块，任务执行中会逐轮保留思考并自动展开当前轮次。
- 思考时长：完成态显示 Worker 从模型流中观测到的实际思考区间；任务总耗时继续单独展示，包含模型等待、工具和子任务时间，不会冒充思考时长。
- 资产治理：System/Project Agent、Skill、MCP 和 Credential 的版本化发布、绑定与准入快照；平台资产页只展示 System 资产，项目代管页只展示所选项目自建资产。替换系统凭据只会创建新版本，不会自动换绑；响应会带上服务端计算的待迁移引用数（含钉在旧密钥上的系统模型），管理员可在同一详情页立即迁移。
- AI 创建 Skill：发送后立即显示用户消息和生成状态；补充信息可连续提交并自动保存，生成超过 60 秒时本地、Docker 与 Helm 入口都会继续等待服务端的受控结果。
- Agent harness：Sub-Agent、Plan Mode、上下文压缩、长期 Memory、Guardrail、Tool Search 和循环检测。
- Sandbox：支持 Local、容器和 Provisioner/Kubernetes provider；具体隔离能力取决于所选 provider。
- 项目自动化：一次性或 Cron Automation，由独立 Scheduler 准入、Worker 执行。
- IM Channel：Feishu/Lark、Slack、Telegram、Discord、DingTalk、WeChat 和企业微信等项目绑定连接。项目 Admin 在“渠道连接”为项目配置独立应用实例，成员可保留个人 `p2p /connect`。Feishu 还支持 Admin 选择 Agent 后在群内发送一次性 `/bind-project` 命令；群成员无需 ActWeave 账号或个人绑定，同群同话题中仍按发送人使用完全隔离的私有会话。Secret 只保存为加密项目 Credential，不写入项目元数据或浏览器查询缓存。

## 运行架构

| 组件        |   默认端口 | 职责                                        |
| ----------- | ---------: | ------------------------------------------- |
| Nginx       |     `2026` | 唯一对外入口，代理前端和 `/api/*`           |
| Frontend    |     `3000` | Next.js Web UI                              |
| Gateway     |     `8001` | 认证、项目 API、Run 准入、查询和 SSE replay |
| Worker      | 无公开端口 | 唯一 Agent graph 执行进程                   |
| Scheduler   | 无公开端口 | 可选 Automation 轮询与准入进程              |
| Provisioner |     `8002` | 仅特定 Sandbox/集群模式需要                 |

```text
Browser / IM
     │
     ▼
Nginx :2026 ─────► Frontend :3000
     │
     └───────────► Gateway :8001 ─────► PostgreSQL
                                      ▲          ▲
                                      │          │
                                  Worker     Scheduler
```

Gateway 不执行 Agent graph；Worker 不提供面向浏览器的业务 API。私有资源始终绑定 `account + project + owner`，授权由业务层和数据库模型共同保证，不使用 PostgreSQL RLS。

## 快速开始

### 1. 准备环境

- Python 3.12+
- Node.js 22+
- pnpm 10.26.2+
- `uv`
- PostgreSQL
- 本地全栈模式需要 Nginx

```bash
git clone https://github.com/SerenaLuna8/deer-flow.git
cd deer-flow
make check
make install
```

### 2. 生成配置

推荐使用交互式向导：

```bash
make setup
```

也可以从示例生成后手工编辑：

```bash
make config
```

进程运行配置位于仓库根目录 `config.yaml`；平台密钥通过根目录 `.env` 或进程环境变量提供，
两者都不应提交。模型定义及其 provider Credential 不再属于 YAML 运行配置，而是保存在
PostgreSQL；system admin 可在 `/admin/settings/models` 管理。完整进程字段见
[`config.example.yaml`](./config.example.yaml) 和[后端配置说明](./backend/docs/CONFIGURATION.md)。
当前示例配置 schema 为 version 35；已有本地配置应运行 `make config-upgrade`。旧的空 endpoint
列表会安全迁移为空网段列表并继续拒绝全部项目远程 MCP；旧列表只要包含精确 URL，升级器就会
中止并要求人工选择 CIDR，避免把一个地址静默放大为整个网段。升级器还会删除已迁入
PostgreSQL 的 Agent runtime、注册开关和配额默认值叶子，以及旧顶层 `models:` 与
`authorization:`。system admin 在 `/admin/settings/system` 修改这些数据库策略后，新请求/新
Run 立即按各自生效边界读取；`mcp_security` 仍是启动时配置，修改后需同时重启 Gateway、
Scheduler 和 Worker。

### 3. 初始化 PostgreSQL

`make setup-db` 是唯一数据库初始化入口，只接受空 PostgreSQL 目标库。它直接执行完整的 `full_schema.sql`、写入精确 marker `full_schema_v5`，随后初始化系统资产 catalog、LangGraph schema 和默认项目。初始化命令会在根目录 `.env` 存在时加载它（显式 shell 环境优先，也可完全不依赖 `.env`），一次性读取 `DEEPSEEK_API_KEY`、`OPENCODE_API_KEY` 与 Credential keyring：DeepSeek V4 Flash 与 DeepSeek V4 Pro 共同引用一份加密 `model_api_key` Credential，GPT 5.6 Luna 使用单独加密的 OpenCode Credential，Flash 仍为默认模型；运行时仍只读取数据库，不隐式加载 dotenv，也不把 provider key 作为进程级模型配置。直接从 `backend/` 启动的模块命令会通过显式安全入口读取根 `.env` 中的数据库、鉴权等非模型配置（显式进程环境优先），并在启动角色前移除模型 provider API key。缺少 key 或 keyring 时，初始化命令会在创建目标库前失败，不留下半初始化库。项目 Skill、Agent Builder、Skill Builder、Skill Credential 绑定、无明文 Run snapshot 与 Credential 逻辑删除都已包含在这份完整 schema 中。运行时不会建库、升级、stamp 或修复 schema；应用 role 需要预先存在，并建议使用非 superuser role。

```bash
# 在根目录 .env 中配置 DATABASE_URL、POSTGRES_ADMIN_URL、
# DEEPSEEK_API_KEY、OPENCODE_API_KEY、DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID 和
# DEER_FLOW_CREDENTIAL_KEYRING_JSON；也可用显式环境变量覆盖
make setup-db
make check-db
```

`make check-db` 只读校验 schema marker 与必需对象，输出三态：`ready`（已在迁移链头）、`upgrade_required`（处于已知历史 revision，先备份数据库再运行 `make upgrade-db` 显式升级到链头）、其余未知 marker、未纳管非空 schema 或 catalog drift 保持 fail-closed，必须新建空库后重新运行 `make setup-db`；命令不会输出完整连接 URL 或密码。`make upgrade-db` 是唯一升级入口（不支持 downgrade），升级后会重算 catalog 校验，结果必须与全新安装完全一致；运行时进程永不自动迁移。

### 4. 启动

本地开发模式：

```bash
make dev
```

本地优化构建模式：

```bash
make start
```

访问 <http://localhost:2026>。首次空库初始化会提供 active 的 DeepSeek V4 Flash、
DeepSeek V4 Pro 和 GPT 5.6 Luna，并将 Flash 设为默认；system admin 可在
`/admin/settings/models` 检查或调整模型目录与加密 Credential。provider
key 只在 `make setup-db` 时由 `.env`/显式环境导入加密 envelope，Gateway、Worker 与 Scheduler
启动时不会接收该 key。本地全栈默认把运行状态写入
`backend/.deer-flow`，日志写入 `logs/`；停止服务使用：

平台管理员可从 `/admin/operations` 进入统一管理界面，在同一导航中查看运行状态、项目、
任务、审计、系统资产和模型设置。桌面导航可在图标栏与完整菜单间展开/折叠，折叠后悬停仍显示
菜单名称；侧栏底部可返回 `/workspace` 项目工作区。资产目录使用按需详情面板：桌面与目录并排、
窄屏占满视口，只有选中条目后才加载版本历史。

```bash
make stop
```

## Docker 与部署

Docker 开发环境支持源码挂载和热更新，但不会自动提供 PostgreSQL：

```bash
make docker-init
make docker-start
make docker-stop
```

本地构建 Compose 镜像可使用：

```bash
make up
make down
```

Kubernetes/Helm 资源位于 `deploy/helm/`。Docker Compose、Kubernetes 和不同 Sandbox provider 的生产使用都需要在目标环境单独完成容量、安全和故障恢复验收，不能仅凭本地启动成功视为生产认证。

## 产品入口

- `/`：直接进入统一鉴权入口；未登录跳转登录页，已登录进入 `/workspace`。
- `/workspace`：登录后的多项目工作区。
- `/projects/{project_slug}`：项目会话、Agent、Skill、MCP、Credential、Memory、Automation、成员和设置。
- `/admin`：仅 system admin 可访问的平台资产与运维页面。

工作区顶部提供账号级通知铃铛和未读角标。向已注册邮箱发出的项目邀请会产生站内通知，接收者可在通知中直接接受并加入项目；通知列表、已读状态和接受操作严格绑定当前服务端认证账号。未注册邮箱不预建站内通知，仍通过不含服务端明文 token 的一次性邀请链接完成注册和兑换。通知 API 不返回邀请 token、token hash 或其他账号的通知数据。

System Agent、Skill 和 MCP 在显式数据库 setup 过程中由受校验的 packaged catalog 写入 PostgreSQL。运行进程只读取数据库中的资产版本和 Run 准入时固定的 snapshot。仓库内 `skills/public/` 的 14 个完整目录是全部 System Skill 的唯一来源：开发期生成器会用它们精确替换 packaged catalog 中的 Skill 集合，同时保留 System Agent 和 MCP。每个新建项目会在创建事务中把当时全部 System Skill 的当前已发布版本绑定为默认启用；以后重新 setup 不会向既有项目补绑新 Skill，也不会重新启用管理员已停用的 Skill。项目管理员仍可在“系统提供”列表中逐项启用或停用，也不应再把同一目录重复导入为项目 Skill。

系统 Main 是项目级编排入口，不要求也不创建项目 System Agent binding。Gateway 只从当前项目收集已启用的 System 资产和已启用、已发布的项目自建资产；其他项目的资产不会进入闭包。Main 可委派当次闭包中的 Agent，但每个被委派 Agent 使用自己精确冻结的模型、Prompt、tool groups、Skill 与 MCP，不继承 Main 的全量资产，也不能递归委派。普通项目或 System Agent 始终只使用自身版本明确引用的 Skill/MCP。每次 Run 会把主 Agent、可委派 Agent、依赖版本和各自模型一起固定，后续发布不会在运行中替换它们。

项目 MCP 只提供一次性的“添加 MCP”表单，不再要求先创建空资产、再创建版本、再单独发布。表单明确区分“不需要认证”“请求头”和“查询参数”；需要认证时只填写字段名，不粘贴密钥。项目 Admin 可在同一向导中选择字段结构完全匹配的已启用项目 Credential，或新建一个加密 Credential；MCP 场景的新凭据由系统固定为 `mcp_auth` 类型，不再要求用户理解或填写内部分类。编辑配置复用新增时的双栏表单、认证方式和 Credential 选择。无凭证配置保存后直接发布；有凭证且操作者具备凭据绑定权限时，同一次保存操作会自动完成配置更新、凭据绑定与发布，绑定或发布失败只重试后半段，不重复提交配置；普通 Editor 只能保存尚未生效的配置，不能越权绑定凭据，由项目 Admin 完成绑定。详情不显示审批状态、审批提示或独立审批按钮：未绑定 Credential 时显示“凭据未绑定 · 尚未生效”，绑定完成后显示“已发布”。MCP 创建本身会在同一事务中写入资产与初始不可变配置；没有 Credential 槽位时直接发布，声明任一槽位时在凭据绑定前保持未生效。详情只展示一份当前配置，不提供历史、编号或内部修订选择器。项目自建 MCP 详情头部不重复展示“项目自建”标签，摘要区将当前配置、最近更新和启用状态在桌面端同一行展示；传输方式、超时和 URL 也在桌面端同一行展示，详情不再重复展示 Credential 槽位，凭据选择与创建仍在新增和编辑配置流程中完成。系统 MCP 像系统 Skill 一样直接在列表行启用或停用，存在新配置时通过同一行的“更新”操作同步当前配置，详情页只展示项目使用状态。数据库仍保留内部 revision 与精确快照作为审计和运行准入边界。JSON 配置导入暂未开放。

项目 MCP 不能直接启动 `stdio` 子进程，只提供 `http`/`sse` 远程连接。项目地址可以使用 HTTP 或 HTTPS，并以 `mcp_security.project_remote_allowed_networks` 配置的 CIDR 网段为边界；默认网段覆盖本机回环和常见 IPv4/IPv6 私网，因此 `127.0.0.1`、`10.x`、`172.16–31.x`、`192.168.x` 及 ULA IPv6 地址无需逐 IP 或逐 URL 登记。特殊网络只需新增一条 CIDR。CIDR 只限制目标地址，不限制端口、路径或服务身份，因此只应配置项目确实需要访问的网段。本机非容器部署时 Worker 是同一主机上的独立进程，`127.0.0.1` 就指向该主机。项目地址使用 IP literal 或 `localhost`；`localhost` 会确定性规范化为 `127.0.0.1`，IPv6 回环请显式填写 `[::1]`，普通 DNS 名称不能靠一次解析结果获得网段权限。任意环境变量、静态请求头和 OAuth 配置不会作为普通版本字段保存，认证值必须通过加密 Credential 的 header 或 query slot 提供；query 密钥只会在 Worker 内存中附加到无密钥 base URL，且不能覆盖 base URL 已有的同名参数。Basic 认证在槽位中选择“请求头”并只填写字段名 `Authorization`，实际项目 Credential 的该字段值再填写 `Basic xxxxxxxx`。对于使用 `?key=...` 的 Streamable HTTP 服务，MCP 配置只填写无查询参数的基础地址，项目 Credential 创建 `query` 分组的 `key` 字段，再在保存配置时绑定该 Credential。

使用 HTTP 时，请求头和查询参数中的 Credential 会以明文经过网络，只适用于可信内网；跨越不可信网络时应使用 HTTPS。query 密钥按协议会出现在发送给出口代理和远端服务的 request-target 中，因此代理及上游访问日志必须禁用查询串记录或对其完整脱敏；服务支持 header 鉴权时应优先使用 header。Worker 对工具发现和每次调用重新校验快照与目标网段、禁用重定向和环境代理，并执行平台级硬超时。部署可选启用 `mcp_security.require_egress_proxy` 和 `egress_proxy_url`，由受控出口进一步实施独立网络策略。项目 MCP 在新建或编辑后，只要配置已经发布，发布事务就会同时加入一次持久化工具发现任务；带 Credential 的配置会在凭据绑定并固定完整授权闭包后再加入任务。Worker 只执行 MCP 初始化和工具列表读取，不调用任何工具，详情会显示测试中、工具名称与说明，失败时保留已保存配置并提供“重新测试”。Gateway 和浏览器不会连接外部 MCP，也不会解密 Credential；工具目录只是显示用观测，每次真实 Run 仍会重新发现并校验工具。发现失败、Credential 授权变化或配置变化会分别显示降级、失败或过期说明。历史不兼容配置仍可审计读取，Project MCP 历史 API 继续只返回远程 HTTP(S) origin，不回放可能携带凭据的路径或查询参数，也不能用于新的 Agent Run。只有具备编辑权限的专用 `GET /api/projects/{project_id}/mcp-servers/{asset_id}/configured` 会返回当前可编辑配置中经过校验的完整 IP 路径，且仍不返回内嵌凭据、查询参数、片段或 Credential 值。项目详情不再把“归档”作为主操作：具备当前已发布配置的项目自建 MCP 可停用并重新启用，重新启用时会再次校验定义和 Credential 闭包；详情危险操作区经过二次确认和 5 秒等待后可永久删除未被引用的项目 MCP 及其内部修订、槽位和授权配置。仍被 Agent revision 或历史 Run/授权快照引用时返回 `409`，不会级联删除引用方；系统 MCP 永远不可删除。

项目 Agent 页面使用项目自建 Agent 卡片：卡片主体进入详情，已配置、启用且具备执行权限的 Agent 可从卡片直接创建绑定到该 Agent 的私有对话。新建入口会先创建一个仅绑定当前项目与当前账号的可恢复设计会话，再通过对话和澄清让模型生成 `AGENTS.md`、`SOUL.md`、`IDENTITY.md` 与 `USER.md` 四项候选设定；模型不能直接写库、发布或启用资产。用户预览、修改并最终确认后，后端才在一个事务里创建默认停用的 Agent、写入首份完整内部配置并结束设计会话；确认响应只返回设计会话和 Agent，不暴露内部 revision。同一项目的 Agent slug 不可重复，中断的生成可重试，设计消息和候选稿随精确项目/账号隐私范围清理。详情顶部只显示名称与最近更新时间，不提供 Agent 归档；卡片与详情为具备权限的停用态 Agent 提供启用动作，启用态详情提供停用动作。列表不提供删除入口；项目自建 Agent 仅在详情页经过二次确认和 5 秒等待后才可永久删除整个 Agent 及全部设置。已有对话、自动化或 Run snapshot 引用时返回 `409`，不会级联删除私有历史；系统 Agent 永远不可删除。四个名称只是映射到 Agent 内部配置字段的固定逻辑文档，不创建物理文件或独立文件版本。保存会在同一事务中复制当前运行配置、写入新的内部 revision 并移动当前指针；停用状态不会阻止继续编辑。后续新准入 Run 会立即使用新设置，无需重启服务；已经准入的 Run 继续使用当次固定的精确版本，后续发布不会替换它们。Worker 在物化和每个副作用边界仍会重验项目归属、资产状态、System binding、Credential 闭包与执行权限，停用、解绑或撤销会按安全边界 fail closed。四项内容位于其他项目可配置提示之后，是最高的项目可配置提示层，并紧邻最终平台关键提醒之前；平台安全、授权与隔离规则无论位于模板何处都始终优先；Main 委派时为每个子 Agent 注入其自身的精确准入快照，而不是继承 Main 的全量资产。

Agent 不向用户提供创建版本、选择版本或发布版本的操作。项目与管理员代管项目 API 只保留内部 revision 历史的只读查询；Builder 确认和四项指令保存由后端内部原子维护不可变 revision，Run snapshot 仍固定实际使用的精确配置。

项目 Skill 的显示名称在同一项目内大小写不敏感且不可重复，不同项目可使用相同名称；`SKILL.md` frontmatter `name` 必须与资产 slug 完全一致。列表的新建菜单提供“AI 对话创建”“从空白创建”和“上传压缩包”三种入口。AI 对话创建会先建立仅绑定当前项目与账号的可恢复设计会话，固定当时已发布的系统 `skill-creator` 版本，并由无工具的一次性模型生成临时候选文件包；用户可以按目录预览和修改文件，每次变化后都要重新检查，最终确认才会原子创建默认停用、已发布版本 1 且尚未绑定任何 Agent 的项目 Skill。Builder 通过文件的安全相对路径还原 `scripts/`、`references/`、`templates/` 等目录，只接受 UTF-8 文本文件，不能表示空目录、二进制文件或可执行权限位；当前人工编辑区可修改 AI 已生成的文件，新增、删除或重命名文件需要继续通过对话让 AI 更新候选包。放弃会话会清除候选文件，未完成会话数量和文件大小均受限，敏感凭据样式的名称、消息或文件不会进入设计存储或模型输入。

从列表空白创建时，后端会在同一事务中创建默认停用的资产、版本 1 草稿以及根目录 `SKILL.md` 基础模板，不会留下没有版本文件的半成品，也不会自动发布；只有已发布版本才能通过列表或详情开关启用。详情不再提供空白版本入口，后续版本统一从当前选中版本点击“创建新版本”，修改并另存为新的不可变草稿。版本文件按真实目录树展示，只打开当前选中文件；新建文件需指定目标文件夹，并可在流程中创建嵌套目录。创建时也可用 `multipart/form-data` 上传 `.zip`、`.skill`（ZIP）、`.tar`、`.tar.gz` 或 `.tgz`；单一外层目录会自动剥离，资产创建和首版发布在同一事务完成，资产仍保持停用。单个 archive 及批量导入均限制为合计 100 MiB、最多 16384 个文件。Gateway 和统一 Nginx 入口只在 Skill archive 创建路由上允许最多 160 MiB 的 JSON/base64 或 multipart wire body，并在 JSON/Pydantic 或 multipart 路由处理前拒绝越界请求。每个不可变项目 Skill 版本的完整文件大小都会计入项目 `storage_bytes` 配额。项目自建 Skill 不提供归档或暂停：详情页二次确认并等待 5 秒后执行整包永久删除，原子删除全部文件和版本并释放对应配额；仍被 Agent 或已准入 Run 引用时返回 `409`，系统 Skill 永远不可删除。Worker 把系统 Skill 投影到 `/mnt/skills/public/<name>`，把项目 Skill 投影到 `/mnt/skills/custom/<asset_uuid>`；执行前按准入时固定的精确版本、checksum 和绑定重新校验，其他项目的 catalog 更新不会使当前 Run 失效。

Skill 可在 `SKILL.md` frontmatter 中声明 `required-secrets` 环境变量。详情页只允许把这些变量绑定到同一项目里已加密写入的现有 Credential 版本，不输入也不回显密钥；配置按 Skill 的精确版本保存，因此新版本不会覆盖旧 Agent 固定版本仍在使用的绑定。启用和 Run 准入会对必填项 fail closed，并把 Credential 标识作为无明文引用固定到 Run snapshot。Worker 在每次对应 Skill 执行前重新校验精确闭包并只在内存中解密；变量只在该 Skill 被显式或受控自动激活时注入本次 sandbox 子进程，平台不会主动把明文序列化到 Skill 文件、提示词、API 响应、Run 元数据、日志或 trace，并会遮盖命令输出中的明文字面值。获得 Credential 的 Skill 必须视为受信任代码：输出遮盖只是误泄漏防护，不是 DLP，无法阻止恶意代码对密钥编码、拆分、写入文件或主动外传。Credential 替换、撤销和轮换会保留每个版本的其他绑定并通过 revision 冲突控制；后续 Skill 执行会立即重新验证，闭包漂移时安全失败，而不是静默继续使用旧密钥或改用新密钥。

## 项目结构

```text
deer-flow/
├── backend/
│   ├── app/                         # Gateway、Worker、Scheduler 与业务域
│   ├── packages/harness/deerflow/   # Agent harness、tools、sandbox、persistence
│   ├── scripts/                     # 数据库、验收和运维脚本
│   └── tests/                       # 后端单元、集成和 PostgreSQL 门禁
├── frontend/
│   ├── src/                         # Next.js 页面、组件和前端领域模块
│   └── tests/                       # 单元测试与 Playwright E2E
├── docker/                          # Compose、Nginx 和 Provisioner
├── deploy/helm/                     # Kubernetes/Helm 资源
├── scripts/                         # 根级安装、启动、诊断和部署编排
├── skills/public/                   # 全部 14 个 packaged System Skill 的唯一源目录
├── docs/                            # 跨模块设计文档
├── config.example.yaml              # 配置模板
├── Install.md                       # 面向 Coding Agent 的安装流程
└── Makefile                         # 全栈命令入口
```

模块实现细节分别见 [`backend/AGENTS.md`](./backend/AGENTS.md) 和 [`frontend/AGENTS.md`](./frontend/AGENTS.md)。

## 常用命令

| 命令                                                          | 用途                                                    |
| ------------------------------------------------------------- | ------------------------------------------------------- |
| `make setup`                                                  | 运行交互式初始化向导                                    |
| `make doctor`                                                 | 检查配置、数据库和运行环境                              |
| `make support-bundle`                                         | 生成脱敏诊断材料                                        |
| `make dev` / `make start`                                     | 启动本地全栈                                            |
| `make gateway` / `make worker` / `make scheduler`             | 单独启动后端进程                                        |
| `make setup-db`                                               | 在空库执行完整 schema 并初始化 PostgreSQL               |
| `make upgrade-db`                                             | 显式升级存量库到迁移链头（先备份，不支持 downgrade）    |
| `make check-db`                                               | 只读检查 PostgreSQL marker 与必需对象                   |
| `cd backend && make lint`                                     | 后端格式与静态检查                                      |
| `cd frontend && pnpm check && pnpm test`                      | 前端 lint、类型检查与单元测试                           |
| `POSTGRES_TEST_URL=... make test`                             | 运行后端核心测试（含真实 PostgreSQL）；仅限可丢弃实例   |

完整命令列表运行 `make help`。

GitHub Actions 由 `.github/workflows/project-saas-release-gates.yml` 统一执行精简后的后端核心测试、
真实 PostgreSQL 核心用例、前端核心单元测试、少量确定性 Chromium E2E、格式和安全检查。
单场景 Replay E2E、发布、容器、Helm Chart 与版本校验继续使用各自的专用工作流。

## 文档

- [安装流程](./Install.md)
- [Project-first SaaS 设计](./docs/2026-07-12-project-first-saas-design.md)
- [后端文档索引](./backend/docs/README.md)
- [系统架构](./backend/docs/ARCHITECTURE.md)
- [API 路由](./backend/docs/API.md)
- [配置参考](./backend/docs/CONFIGURATION.md)
- [IM Channel Connections](./backend/docs/IM_CHANNEL_CONNECTIONS.md)
- [前端开发指南](./frontend/AGENTS.md)

## 安全边界

- 只对外开放 Nginx 入口，并在真实部署中配置网络访问控制和 TLS；不要直接暴露 Gateway、Frontend 或 Provisioner 端口。
- `LocalSandboxProvider` 在宿主环境执行命令，不是强隔离边界；只应在可信环境使用。面向不可信任务时应选择并验证容器或集群级 Sandbox。
- 不要把 API key、Cookie、Credential、数据库密码或完整连接 URL 写入代码、日志、截图和 issue。
- System admin 与项目能力遵循最小权限原则。项目外资源返回 404，项目成员缺少所需 capability 时返回 403。

## 参与贡献

提交前请阅读 [`backend/CONTRIBUTING.md`](./backend/CONTRIBUTING.md)，并运行与改动范围相匹配的后端、前端和 PostgreSQL 门禁。

## 许可证与致谢

本项目采用 [MIT License](./LICENSE)。感谢 [ByteDance DeerFlow 上游项目](https://github.com/bytedance/deer-flow)及所有贡献者奠定的 Agent harness、工具和前端基础。

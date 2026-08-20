# ActWeave

Weave intelligence into action.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](./backend/pyproject.toml)
[![Node.js](https://img.shields.io/badge/Node.js-22%2B-339933?logo=node.js&logoColor=white)](./Makefile)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

ActWeave 是一个面向多账户、多项目协作的全栈 Super Agent 系统。它以
LangGraph Agent harness 为执行核心，提供项目级权限、Agent/Skill/MCP 资产、长期
Memory、Sub-Agent、Sandbox、Automation、外部 Channel 和可恢复的持久化会话。

系统采用 project-first 架构：Gateway 负责认证、授权和 Run 准入，Worker 独占
Agent graph 执行，Scheduler 只负责到期 Automation 准入；PostgreSQL 保存应用状态、
运行记录和受治理的资产版本。

> 当前代码线源自 DeerFlow 2 的重写，与最初的 Deep Research 实现不共用代码。
> 原始版本见 ByteDance 上游 [`main-1.x`](https://github.com/bytedance/deer-flow/tree/main-1.x)。

## 核心能力

- 多账户、多项目工作区，包含成员、角色、邀请、配额、审计和通知。
- 项目私有 Thread/Run、持久化 SSE、断线恢复、取消、重试和文件交付。删除会话会
  立即撤销该会话仍在运行的服务端执行权限并结束其 durable stream；已经发出的外部
  操作无法由平台召回。会话输入框选择、粘贴或拖入附件后会立即在后台预上传；发送
  消息时复用同一上传结果，不会重复上传。已经持久化的完整回复不会因运行时或沙箱
  回收失败被改写为 Agent 执行失败；此类回收问题作为 Worker 运维错误重试和记录。
  Run 内的依赖虚拟环境属于临时运行状态，不作为会话文件保存；其他工作区符号链接
  仍安全失败。
  单个 MCP 服务在远端工具发现阶段不可用或返回非法目录时，只会禁用该 MCP 的本次
  Run 工具并向 Agent 提供安全的能力降级提示；其他能力和主回复继续执行。授权撤销、
  冻结快照漂移、Credential 材料化不确定等安全边界仍会终止 Run。Skill 脚本及普通
  工具失败以错误结果返回 Agent，由其使用现有上下文继续或明确说明未完成部分。
- System/Project Agent、Skill、MCP 与 Credential 的不可变版本和准入快照。项目 Skill
  仅可通过 AI 对话创建/修订，或上传 `.zip`、`.skill`、`.tar`、`.tar.gz` 或 `.tgz`
  包；常见 macOS 归档元数据会被忽略。新建 Skill 保持 suspended，修订草稿需由具备
  资产发布权限的成员显式发布后才生效；超出上传、解压、单文件或成员数限制的包会以
  明确的大小限制错误拒绝。Skill 详情可把当前选中的已持久化版本导出为
  `<slug>-v<version_number>.zip` 标准分发包；`SKILL.md` 位于包根目录，包内不含
  Credential 映射、密钥、版本历史或发布状态，被治理撤销的 System Skill 版本不可导出。
  项目 Agent 的删除采用软归档：从项目目录移除并拒绝后续
  Run，既有会话、运行记录及已准入的执行快照继续保留；已归档 Agent 不再占用项目
  名称，同一名称可用于创建具有新 ID 的 Agent。Agent 详情按所选不可变版本展示
  指令和能力；基于旧发布版的草稿不能直接发布，但可复制为基于当前发布版的新草稿，
  不会覆盖历史或回退当前指针。Skill 可在 `SKILL.md` 中通过
  `required-secrets` 声明敏感环境变量；版本工作台和 AI Builder 提供结构化表单并与
  同一源码副本同步。版本工作台的“运行凭证”把每个目标变量映射到精确的项目
  Credential 版本及其中一个 `env` 来源字段：声明仍只写入 `SKILL.md`，项目映射只存
  PostgreSQL。普通 Draft 在发布前必须完成全部必需映射，发布窗口只做只读预检；AI
  或压缩包首次创建的 Skill 保持 published + suspended，允许随后为精确 v1 补齐映射，
  但映射完整前不能启用。已发布版本仍可独立轮换运行映射，不需要重发 Skill。明文只在
  授权命令执行边界从选定来源字段解密，并以 Skill 声明的目标变量名注入；Local Provider
  与本地 AIO Provider 均不把它写入版本、快照或浏览器状态。
- AI Skill Builder 由仅供专用解析器访问的内置 Agent 执行，不出现在项目、全局管理或
  运行时 Agent 目录及其常规 API。它复用普通 Agent 的 Web、文件、Sandbox 和任务委派
  装配，并遵循 Local/AIO Provider 各自的安全策略；候选 Skill 仍只能经受管草稿工具、
  检查和显式提交进入不可变版本历史。
- 长期 Memory、上下文压缩、Dream 整理、归档检索和账号级个性化控制。
- Sub-Agent、Guardrail、Tool Search、循环检测和可扩展工具链。
- 文本 lead model 的受治理图片识别桥接：按 Run 冻结辅助视觉模型，使用
  `inspect_image` 返回有界、不可信视觉分析；视觉调用与其他模型调用共用唯一
  `ModelRuntime` 和所选模型已有的 Provider adapter。
- Local、容器、BoxLite 和可选 Provisioner/Kubernetes Sandbox provider。
- 一次性或 Cron Automation，以及 Feishu、Slack、Telegram 等外部 Channel。
- 平台管理员的系统设置、模型目录、资产治理和运维界面。

项目菜单按服务端下发的 capability 分层，而不是仅按角色名称在浏览器中推断：

| 项目角色 | 项目菜单             | 主要边界                             |
| -------- | -------------------- | ------------------------------------ |
| Admin    | 工作、能力、项目管理 | 项目治理、成员、凭证、审计及资产发布 |
| Editor   | 工作、能力           | 运行工作，并创建或编辑项目资产 Draft |
| Runner   | 工作、Agent（只读）  | 运行工作；只读查看 Agent             |
| Viewer   | 工作、Agent（只读）  | 查看自己的既有工作和 Agent；不能运行 |

“工作”包含会话、Automation 和 Memory；渠道连接属于“项目管理”，只对具备
`project.channels.manage` 的项目管理员显示。Runner 和 Viewer 可只读查看 Agent 目录，以及
本人已有且未完成的 Agent Builder 会话；创建、继续设计、编辑、取消、提交和生命周期操作仍
按编辑或治理 capability 拒绝。Skill/MCP 作者工作台也不向只读角色开放。

## 运行架构

| 组件        |   默认端口 | 职责                                        |
| ----------- | ---------: | ------------------------------------------- |
| Nginx       |     `2026` | 唯一浏览器入口，代理前端和 `/api/*`         |
| Frontend    |     `3000` | Next.js Web UI                              |
| Gateway     |     `8001` | 认证、项目 API、Run 准入、查询和 SSE replay |
| Worker      | 无公开端口 | 唯一 Agent graph 执行进程                   |
| Scheduler   | 无公开端口 | 可选 Automation 轮询与准入进程              |
| Provisioner |     `8002` | 仅特定 Kubernetes Sandbox 模式需要          |

```text
Browser / Channel
        |
        v
Nginx :2026 ----> Frontend :3000
        |
        +--------> Gateway :8001 ----> PostgreSQL
                                      ^          ^
                                      |          |
                                   Worker    Scheduler
```

Gateway 不执行 Agent graph；Worker 不提供浏览器业务 API。私有资源按账户、项目和
owner 作用域隔离，浏览器状态和请求字段都不是授权依据。

## 快速开始

### 环境要求

- Python 3.12+
- Node.js 22+
- pnpm 10.26.2+
- `uv`
- PostgreSQL
- 本地全栈模式所需的 Nginx

从获准的内部代码源取得项目并进入仓库根目录：

```bash
make check
make install
make setup
```

`make setup` 会引导生成根目录 `config.yaml` 和所需环境配置。不要提交
`config.yaml`、`.env`、数据库密码或 provider key。完整字段和升级规则见
[配置参考](./backend/docs/CONFIGURATION.md)。

### 初始化数据库

为一个新的空 PostgreSQL 目标配置 `DATABASE_URL`、`POSTGRES_ADMIN_URL` 和初始化
所需的 Credential 环境变量，然后运行：

```bash
make setup-db
make check-db
```

- `make setup-db` 只初始化空目标库，并把完整快照记录为当前链头 revision
  `skill_credential_source_field`。
- 初始化会为应用表、Alembic 版本表、LangGraph 表及每个 `run_events` 物理分区写入
  非空的中文表注释和字段注释；缺失或漂移的注释会使 schema 校验安全失败。
- 已知旧版本可直接通过 `make upgrade-db` 显式升级。
- 未知 marker、未纳管的非空 schema 或 catalog drift 会安全失败。
- Gateway、Worker 和 Scheduler 从不自动迁移或修复 schema。
- 打包 System Asset 有新增不可变 release 时，先停止运行服务，在维护窗口执行
  `make upgrade-system-assets`；该命令从标准运行环境读取 `DATABASE_URL`，保留历史版本与
  既有项目 pin，并且可幂等重跑。
- 全局管理员可对已发布的 System Skill 版本执行不可逆治理撤销。撤销保留发布内容、历史、
  当前指针和既有项目 pin；新绑定、新 Run 及 Worker 重试/恢复会拒绝该版本，项目管理员需
  显式迁移到仍可绑定的版本或停用绑定。已经完成物化并正在运行的图不会被强制中断。
- 生产运维应通过平台外部的 cron、systemd timer 或编排器每日运行（至少每个 UTC 月
  成功一次）`make prepare-run-event-partitions`；该幂等命令只在当前 schema head 上
  预创建 UTC 当前月至 N+2 月分区，锁等待超时后可安全重试。不要把它挂到 Gateway、
  Worker 或 Scheduler 的启动路径。

详细准备步骤见 [Install.md](./Install.md)。

### 启动与停止

```bash
make dev      # 热更新开发模式
make start    # 本地优化构建模式
make dev-daemon    # 后台开发模式
make start-daemon  # 后台本地生产模式
make stop
```

在 macOS 上，两个 `*-daemon` 命令会交由当前登录用户的 `launchd` 持续托管一个前台
启动器；命令返回后服务仍由该启动器负责，避免终端或自动化执行会话结束时一并回收子进程。
用 `make stop` 停止服务并卸载该托管任务。服务日志仍在 `logs/`，启动器日志为
`logs/{dev,prod}-daemon-supervisor.{out,err}.log`。

浏览器访问 <http://localhost:2026>。常见入口：

- `/workspace`：账户级多项目工作区。
- `/projects/{project_slug}`：项目会话、资产、Memory、Automation 和设置。
- `/admin`：仅 system admin 可访问的平台治理与运维页面。

### 系统模型适配器

管理端当前可创建 OpenAI、OpenAI 兼容增强、Anthropic、DeepSeek、DeepSeek
兼容增强和 vLLM 模型。Vision Bridge 不再定义专用模型适配器；
`vision_openai_compatible_v1` 不在生产适配器注册表，`vision_bridge_fake` 仅供测试注入。
MiMo、MiniMax、StepFun、MindIE、Claude Code CLI
和 Codex CLI 模型适配器已停止支持，不再允许新建、启用、设为默认或准入新 Run；已有
历史目录记录仍可由管理员查看并改配到受支持适配器。全新数据库只初始化
`patched_deepseek` 和 `openai` 模型配置。

### 文本模型图片识别桥接

该能力不在 `config.yaml` 声明工具、模型或开关。System admin 先在
`/admin/settings/models` 检查内置模型或创建 active、`supports_vision=true` 的视觉模型，
并使用“测试连接”验证其多模态调用，再到 `/admin/settings/system` 的“图片识别桥接”
确认或选择该模型。非空选择即对后续 text-only project Run 启用，清空即关闭。全新安装
默认选择已内置的
GPT 5.6 Luna；原生视觉 lead model 继续使用现有 `view_image`，不会同时注册
`inspect_image`。该 bootstrap 默认值不替代供应商数据政策审批；生产部署尚未批准外发
时，须在接收项目 Run 前清空选择。
全新安装把单次 `inspect_image` 端到端截止时间初始化为 60 秒；管理员可在 5–120 秒内
调整，更新只会冻结到之后新建的 Run。

Bridge 不解析或重写厂商协议。`inspect_image` 通过唯一 `ModelRuntime` 向所选模型发送标准
LangChain 多模态 content block；OpenAI、Anthropic、DeepSeek、vLLM 或其他已支持
Provider 的现有 adapter 负责 Credential、Endpoint、请求序列化和响应解析。任意 active、
`supports_vision=true` 且 adapter 仍可新绑定的系统模型都可以被选择，不按模型名称或
Luna 身份硬编码。

工具只发送规范化后的单张图片、固定 system prompt、固定 mode 指令和 lead 根据当前用户
问题生成的必填 `analysis_goal`，不发送完整对话或文件路径。服务端把普通 `AIMessage`
文本包装成有界的 `inspect_image.result.v2` ToolMessage，并明确标记为不可信内容。管理端“测试连接”使用
平台生成的无敏感 64×64 蓝色方块 PNG 经过同一个 Runtime 和 adapter；成功只证明当次
连接可用，生产启用前仍须完成供应商政策和真实 API 质量、延迟与限流验收。

Run 仍冻结精确 `purpose="vision"` 模型版本和 Credential 引用，Worker 调用前后仍使用
durable dispatch authority；暂停模型或撤销 Credential 会阻断后续调用，但不能召回已经
在途的请求。完整架构、实施状态、历史兼容和验收门禁见
[Vision Bridge 架构收敛改造方案](./backend/docs/VISION_BRIDGE_REFACTOR_PLAN.md)。

## Docker 与部署

Docker 开发环境：

```bash
make docker-init
make docker-start
make docker-stop
```

本地 Compose 构建与运行：

```bash
make up
make down
```

当前仓库提供本地和 Docker Compose 整栈运行，不提供 Kubernetes/Helm 整栈部署
资源。`docker/provisioner/` 是可选的 Kubernetes Sandbox provider，不是完整应用的
部署方案。任何生产环境都需要独立验证容量、网络、安全、存储和故障恢复。

## 常用开发命令

| 命令                                              | 用途                           |
| ------------------------------------------------- | ------------------------------ |
| `make doctor`                                     | 检查配置、数据库和运行环境     |
| `make support-bundle`                             | 生成脱敏诊断包和内部事件草稿   |
| `make gateway` / `make worker` / `make scheduler` | 单独启动后端进程               |
| `make test`                                       | 使用隔离测试库运行后端核心测试 |
| `cd backend && make lint && make format`          | 后端静态检查和格式化           |
| `cd frontend && pnpm check && pnpm test`          | 前端 lint、类型检查和单元测试  |

完整命令见 `make help`。本私有仓库没有托管 CI；集成或发布前必须针对当前
checkout 手工执行相关 PostgreSQL、前端、浏览器、安全、容器和目标环境门禁。

## 安全边界

- 对外只开放受 TLS 和网络策略保护的 Nginx；不要直接公开 Gateway、Frontend 或
  Provisioner 端口。
- `LocalSandboxProvider` 在 Worker 的 OS namespace 执行命令，不是强隔离边界；
  native 本机部署就是宿主账号，Compose 部署则是 Worker 容器；只用于可信环境。
- 如确需 Local Bash，可配置逐条 `allow_once` / `deny` 审批；批准仍是宿主 RCE，
  不会变成沙箱，也没有会话级授权。详见
  [Local Provider 本机命令单次审批](./backend/docs/HOST_EXECUTION_APPROVAL.md)。
- Apple silicon Mac 上需要执行 Agent 生成的 Bash/Python 时，优先使用
  `AioSandboxProvider` + Apple Container；配置与实测流程见
  [Apple Container Sandbox](./backend/docs/APPLE_CONTAINER.md)。
- API key、Cookie、Credential、数据库密码和完整连接 URL 不得进入代码、日志、
  截图、浏览器缓存或诊断材料。
- System admin 不自动拥有项目权限；项目访问必须服从服务端返回的 membership 和
  capability。

## 文档导航

- [安装流程](./Install.md)
- [后端概览](./backend/README.md)
- [后端文档索引](./backend/docs/README.md)
- [系统架构](./backend/docs/ARCHITECTURE.md)
- [API 路由](./backend/docs/API.md)
- [配置参考](./backend/docs/CONFIGURATION.md)
- [前端概览](./frontend/README.md)
- [后端开发约定](./backend/AGENTS.md)
- [前端开发约定](./frontend/AGENTS.md)

## 许可证与致谢

本项目采用 [MIT License](./LICENSE)。感谢
[ByteDance DeerFlow 上游项目](https://github.com/bytedance/deer-flow)及其贡献者奠定的
Agent harness、工具和前端基础。

# Local Provider 本机命令单次审批

> 状态：当前分支已实现；修复后的 Lead 与 `general-purpose` 子代理拒绝路径浏览器验收、
> 自动化门禁已完成，active-goal 的真实模型 live 边界见“当前验证状态”。
> 适用范围：交互式 owner-private Run 与 `LocalSandboxProvider`。
> 产品边界：界面只提供“允许本次命令”和“拒绝”，不提供会话级、Thread 级、
> 定时或永久授权。

## 1. 结论

`LocalSandboxProvider` 不是安全沙箱。它把 Bash 命令交给 Worker 所在 OS
namespace 中的 Shell；native macOS 开发部署中，这通常等于直接使用运行 Worker
的 Mac 账号执行。审批只是一次显式授权，不会因为用户点击“允许”而把命令迁入
容器、虚拟机或其他隔离边界。

在 Compose 部署中，“宿主”指 `deer-flow-worker` 容器的 OS namespace，而不是
浏览器或 Gateway；它通常不等于 Docker 主机本身，但仍可访问 Worker 容器的凭据、
网络和 bind mount，且没有为每条 Agent 命令创建独立隔离环境。

启用 `approval_required` 后，Lead Agent、`general-purpose` 子代理和 `bash`
子代理发起的每条 Local Bash 调用都先冻结并进入审批。用户允许后，Worker 从
PostgreSQL 领取这条冻结命令并执行一次；模型不能在批准后替换或重新生成命令。

这里的“一次”指一次顶层 Shell 启动尝试。获批字符串可以包含管道、重定向、
here-document、`;`、`&&`、子 Shell、后台任务，因而一次 Shell 启动仍可能创建多个
子进程并产生多个副作用。审批不是命令语义分析器，也不提供回滚。

## 2. Provider 行为

| Provider / 配置                                              | Bash 行为                                    | 是否显示审批 |
| ------------------------------------------------------------ | -------------------------------------------- | ------------ |
| 明确受信任的内置 AIO、BoxLite、E2B provider                  | 在所选隔离 provider 内直接执行               | 否           |
| Local + `allow_host_bash: false` + `mode: disabled`          | 拒绝宿主 Bash                                | 否           |
| Local + `allow_host_bash: false` + `mode: approval_required` | 冻结每条命令，单次审批后由 Worker 在宿主执行 | 是           |
| Local + `allow_host_bash: true`                              | 兼容模式，直接在宿主执行                     | 否           |

`allow_host_bash: true` 与 `mode: approval_required` 互斥，配置加载会拒绝同时开启。
兼容直通模式只适合完全可信的单用户环境，不具备本方案的单次消费和结果回执。

只有明确受信任的内置隔离 Provider 类获得直通权限；同一内置类的重导出仍可识别，
未知 Provider 和自定义隔离 Provider 子类默认 fail closed。Local Provider 的重导出或
子类仍按 Local 处理，不能通过更换类路径绕过审批。特别是使用
`AioSandboxProvider` + Apple Container 时，命令在 AIO 容器内执行；AIO 创建、连接
或执行失败会让该操作失败，不会回退为 `LocalSandboxProvider` 或宿主 Bash。

## 3. 哪些操作会审批

审批边界是“是否通过 Local Provider 启动 Bash”，而不是文件扩展名或 Agent 类型：

- Lead Agent 直接调用 `bash`：审批；
- `general-purpose` 子代理调用 `bash`：审批；
- `bash` 子代理中的 Bash 调用：审批；
- `python script.py`、`python -c ...`：审批，因为 Python 由 Bash 启动；
- 管道、重定向、here-document，以及通过 Bash 写 Python 文件：审批；
- 独立的 `write_file`、`read_file`、`ls` 等文件工具：不因本功能审批；
- 明确受信任的内置 AIO/BoxLite/E2B provider 中的同一条 Bash：在其 provider 内直接执行，不审批。

因此“先用 `write_file` 写脚本，再运行脚本”会在写文件时放行、在运行时审批；
“用 `cat > script.py <<'PY' ...` 写脚本”本身就是 Bash 命令，需要审批。

审批只覆盖 ActWeave 已接入的 Bash 工具路径。Worker 自身启动服务、数据库驱动、
Sandbox provider 控制进程等平台内部运维调用不属于模型发起的审批请求。

## 4. 配置

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
  allow_host_bash: false
  host_execution_approval:
    mode: approval_required
    request_ttl_seconds: 300
    max_timeout_seconds: 600
    execution_domain_id: mac-primary-worker
    execution_domain_label: My local Worker
  bash_command_timeout: 600
```

- `mode` 只允许 `disabled` 或 `approval_required`；默认 `disabled`。
- `request_ttl_seconds` 是待决定请求的有效期，范围 `30..3600` 秒。
- `max_timeout_seconds` 是一次获批启动的审批侧上限，范围 `1..3600` 秒。
- `execution_domain_id` 在 Local `approval_required` 下必填，由运维稳定配置，长度
  `3..128`，只允许 ASCII 字母、数字、点、下划线、冒号和连字符。它参与私有执行域
  affinity，但不会返回浏览器、审计或日志。每个预期不同的 Worker OS namespace 应使用
  不同 ID；复制 ID 本身不会把两台机器变成同一执行域，因为 Worker 还会绑定设备、
  OS namespace、进程用户、运行根目录和净化后的基础环境指纹。
- `execution_domain_label` 是审批卡上显示的安全名称，长度 `1..64`；禁止控制符和
  Unicode 双向文本控制符。不要填主机名、用户名、路径或秘密。修改 label 不会让已批准
  Job 改变执行域。
- 实际命令超时取审批上限与 `bash_command_timeout` 中较小者。
- `allow_once` 落库后使用当前实现固定的 60 秒 continuation claim 窗口；它不是
  会话授权，也不是 `request_ttl_seconds` 的延长。
- 这些字段属于部署配置，修改后应重启 Gateway 和所有 Worker。
- Worker 启动时只在 Local 审批模式捕获执行域。macOS 使用 `IOPlatformUUID` 的摘要；
  Linux 使用严格 machine-id、boot-id 与 `/proc/self/ns/*` identity 的摘要；原始值不落库。
  Linux 重启或容器 namespace 重建会产生新 affinity，使旧 continuation 不能被新域领取。
  macOS 的边界是同一物理 Mac：系统重启后仍视为同一设备，但配置 ID、进程 uid/gid、
  runtime base dir 和净化环境仍必须一致。身份无法读取时 Worker fail closed，审批模式
  不启动。
- continuation Job 写入不可逆 affinity，Job claim SQL 只允许匹配域的 Worker 领取。
  错误域不会 lease、更不会 spawn；批准窗口到期时，仍排队的 continuation Job 与 Run
  在同一事务中取消并释放并发 Run 配额，避免永久占用 Thread 或队列。
- Local `approval_required` 会在启动时严格验证自定义 mount：`host_path` 必须是当时
  已存在的绝对路径，`container_path` 必须是绝对路径，并且不能落在
  `/mnt/acp-workspace`、`/mnt/user-data` 或当前 `skills.container_path` 下。这样待审批期间
  不能通过“稍后创建目录/重启”让同一逻辑路径静默指向新的宿主位置。

在 AIO 配置中可以保留默认的 `host_execution_approval.mode: disabled`；它不控制容器
内 Bash：

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0
  allow_host_bash: false
  host_execution_approval:
    mode: disabled
```

## 5. 权限与交互边界

审批状态是 PostgreSQL 中的 owner-private 数据。Gateway 的读取和决策都绑定当前
认证用户、项目、Thread 和来源 Run；执行前还会重新验证项目成员关系、
`private_work.create`、`private_work.approve_host_execution`、Run/Job lease 及部署策略。
浏览器提交的命令文本、owner、项目或执行上下文都不是 authority。

当前项目角色映射中，只有 Project Admin 包含
`private_work.approve_host_execution`；Editor、Runner、Viewer 和 Channel Guest
不能批准或消费本机执行。

决策请求包含当前版本和幂等键。并发点击、网络重试或重复提交只能收敛到同一决定；
相同幂等键配不同请求、旧版本、过期请求或已关闭请求会冲突，而不会再消费一次。

当前只允许交互式 Private Run 使用审批。Automation、Webhook、IM 等标记为
non-interactive 的执行在 Local 审批模式下 fail closed，不会等待一个不存在的界面操作。

## 6. 生命周期

### 6.1 冻结与展示

1. Agent 生成一个 Bash tool call。
2. Local 审批路径校验命令长度、路径边界和秘密明文，解析 Shell、超时、逻辑命令、
   环境变量名及 Agent 路径。
3. Worker 在来源 Run 的有效 lease 内写入 `staged` 请求。服务端保存 owner-private
   完整计划；checkpoint 中只留下最小 approval artifact，不包含新的执行 authority。
4. Worker 先正常提交来源 Run 已创建或修改的私有文件。若本轮有尚未展示的
   `outputs/*` 文件，结算事务会从数据库中的 ready 文件重建精确候选集合，并为当前
   approval 固化 server-owned `any_one` 交付义务；浏览器 metadata 和模型消息不参与授权。
5. checkpoint-safe 暂停确认后，Worker 在发布公共成功终态前先把与当前 Run、Job attempt、
   approval 完全匹配的 server-only suspension marker 持久化。若此后 Worker 崩溃或终态 ACK
   丢失，接管者从该 marker 恢复成功终态，不重新进入 Agent graph；不同 attempt、旧 Run 或
   浏览器 metadata 都不能伪造或覆盖该证明。
6. 来源 Run 只有携带同一个 typed suspension id 且上述 marker 精确匹配时，才会在结算事务
   中成功并把 `staged` 变为 `pending`。marker 提交前的失败会取消 staged 请求；marker 提交
   后的 publish 异常或并发清理必须先收敛这个成功暂停，不能把已证明的暂停反写成失败。
   旧 Run/旧 Thread 的 approval artifact 不能暂停或激活当前 Run。
7. 来源 Run 成功结算、请求从内部 `staged` 转为真正的 `pending` 后，前端才通过
   active API 取得审批卡片，展示准确的逻辑命令、工作目录、来源 Agent、超时、
   “在本机执行”警告以及可立即使用的决定按钮。

命令摘要绑定的是可迁移的逻辑计划：用户请求的原始命令、Shell、超时、环境变量名
和 Agent 路径。来源 Run 的临时宿主目录或临时 Skill mount 不作为可复用 authority；
continuation 必须在自己的私有 Local sandbox 中重新映射逻辑路径，并核对摘要与冻结的
资产闭包。闭包比较包含 Agent/Skill 资产版本、MCP grant snapshot 和 Skill Credential
binding snapshot；任一不一致都取消旧批准。

同一 owner-private envelope 还保存无秘密的 provider policy snapshot，包括 provider
类路径、解析后的执行模式、`allow_host_bash`、两个 timeout 和请求 TTL。Gateway 决策
与 Worker claim 都要求当前 snapshot 完全相同；切换到 AIO、Local disabled、legacy
direct 或修改相关上限会让旧批准 fail closed，而不是按新策略执行。

### 6.2 允许本次命令：approval-first

允许流程先持久化决定，再准入执行，避免出现“Run 已经创建但批准还未提交”的窗口：

1. Gateway 在锁定 owner-private Thread 和审批行的事务中，以 CAS 将 `pending` 改为
   `approved`，保存 `allow_once`、决定摘要、用户和幂等证据。
2. Gateway 以服务端上下文准入确定性的 continuation Run；该 Run 与 approval id 和
   决定摘要绑定。若存在待交付义务，同一 admission 事务还把它从 `deferred` 原子绑定到
   该 continuation Run/Job。普通浏览器或模型请求不能伪造这种 continuation。
3. Worker 在构建 Agent graph、调用模型或启动进程之前，从数据库原子 claim 冻结计划，
   将状态改为 `claimed`，并创建只属于本次 host spawn 的副作用围栏。
4. Worker 重新验证当前仍为 Local + `approval_required`、权限/lease、来源与 continuation
   的冻结资产闭包和逻辑命令摘要，再在 continuation 的临时目录中重新映射路径。
5. 在实际创建进程前的最后一个事务中，Worker 对审批行做一次 CAS 并写入
   `spawn_authorized_at`；只有精确的 `claimed` attempt 可以成功一次。工具 staging 接口不再有
   inline `approved` 执行分支，所有获批命令都只能经过 frozen continuation runner。
6. Worker 启动一次精确的顶层 Shell，保存结构化 exit code、stdout、stderr 和有界结果。
7. 持久化 `finished` 或 `launch_failed` receipt 后，Worker 才把一个隐藏的、服务端拥有的
   结果输入交给 Lead graph。若存在交付义务，该输入还包含服务端恢复的精确候选路径，
   要求 Lead 调用 `present_files`；这不授权重放命令。
8. `present_files` 先持久化 intent，随后 file finalizer 在创建或复用当前 continuation 的
   Artifact 时，于同一事务将义务改为 `delivered`。continuation 的终态事务会再次读取
   数据库：只有 `delivered`（或根本没有义务）才允许成功，不能依赖 Worker 内存中的标志。

`any_one` 延续原有“至少展示一个本轮输出”的产品语义：展示任一精确来源候选即可；若获批
命令修改了同一路径，则只接受由该 continuation 创建的后继文件。错误路径、其他 Run、其他
Thread 或仅来自模型文本的路径不能满足义务。

当前 intent 是一个持久化单槽：第一次 `present_files` 必须一次提交全部准备展示的规范化
路径；同一路径集合的重放（即使 tool call id 不同）是幂等成功，任何不同的第二组路径都会
fail closed。这样 Worker 接管后只从数据库恢复一份确定集合，不会出现“工具已返回成功、
但第二批仅存在进程内存”的伪成功。

如果第 1 步已提交而 admission 响应丢失，请求会暂时表现为未关联 continuation 的
`approved`。使用同一幂等键重试会继续确定性的 admission/link，不会再创建一份授权。

即使原命令来自子代理，获批执行也由 Worker 的冻结 runner 完成；`agent_path` 保留来源
归属，结果回到 Lead graph。系统不会重新启动子代理让它猜测原命令。
旧 continuation 被消费后，模型若再提出 Bash，会写入新的审批请求；旧 `allow_once`
不会变成后续命令的授权。

### 6.3 拒绝

`deny` 将请求原子改为 `denied`，同时把未完成交付义务改为 `cancelled`，不创建执行
continuation，也不启动进程。来源 Run 已提交但未展示的文件仍是该 Thread 的私有文件，
不会被伪装成已交付 Artifact。拒绝本身不授权模型执行替代宿主命令；用户可以在对话中
继续要求不需要本机执行的方案。

## 7. 状态与前端恢复

| 状态            | 含义                                                   | active API 是否返回 |
| --------------- | ------------------------------------------------------ | ------------------- |
| `staged`        | 来源 Run 内部已冻结，等待来源 Run 结算                 | 否                  |
| `pending`       | 等待用户允许或拒绝                                     | 是                  |
| `approved`      | `allow_once` 已持久化，continuation 正在准入或等待领取 | 是                  |
| `claimed`       | Worker 已消费授权，进程可能即将或已经启动              | 是                  |
| `finished`      | 有完成 receipt 和权威 exit code；非零退出也属于此状态  | 否                  |
| `launch_failed` | 确认未创建进程，并有失败 receipt                       | 否                  |
| `unknown`       | 可能已产生副作用，但无法证明最终结果                   | 否                  |
| `denied`        | 用户拒绝                                               | 否                  |
| `expired`       | 未在决定有效期内处理，或允许后未在 claim 窗口内领取    | 否                  |
| `cancelled`     | 来源 Run、策略或批准后的领取条件失效                   | 否                  |

输出交付使用独立的内部状态机，不暴露为浏览器可提交字段：

| 状态              | 含义                                                     |
| ----------------- | -------------------------------------------------------- |
| `deferred`        | 来源候选已固化，等待审批决定                             |
| `assigned`        | 已原子绑定精确 continuation Run/Job                      |
| `intent_recorded` | continuation 已持久化一次候选 `present_files` intent     |
| `delivered`       | 当前 continuation Artifact 已在同一事务满足义务          |
| `cancelled`       | deny、过期、取消或 interruption 已关闭义务               |
| `blocked_unknown` | 命令副作用状态未知；不得重放命令或声称文件已经交付       |
| `failed`          | continuation 错误或超时，义务未完成                      |

`staged` 只用于 Worker 与数据库内部协调。即使 ToolMessage 已经携带最小 approval
artifact，active 与 by-id API 在该阶段也都返回 `approval: null`，不能把它伪装成
`pending + can_decide: false`。客户端可以保留 approval id 并继续轮询或在来源 Run
结算后重新读取；只有服务端已经进入真正 `pending` 时才渲染审批卡，因此卡片首次出现时
决定按钮必须已经可用。

前端不能在 active API 变为 `null` 后丢失的是恢复锚点，而不是审批卡片。ToolMessage
中持久化的 approval artifact 用于在刷新后恢复 approval id；客户端随后使用 by-id API
继续收敛状态和关联 continuation。只有真正的 `pending` 渲染决定卡片；决定请求提交成功、
服务端返回 `approved` 或 `denied` 后，卡片立即卸载。`approved`、`claimed` 和终态投影只
保留在内部恢复链路中，不继续显示成只读审批卡。

`pending`、`approved`、`claimed` 期间，当前 Thread 的发送框被阻止，但草稿保留；其他
Thread 不受影响。Gateway 的普通 Run admission 也会拒绝同一 Thread 的新 Run；只有与
approval id 和决定摘要匹配的 server-owned continuation 可以穿过该门禁。

界面只显示命令和有界状态摘要，不把完整 stdout/stderr receipt 当作公共 API 返回。

## 8. 批量 tool call 屏障与审批暂停

LangGraph 的 ToolNode 默认可能并行启动同一模型消息中的多个工具。如果一个兄弟工具
已开始写文件或委派任务，另一个 Bash 才暂停等待审批，用户看到的就不再是清晰的
“批准前无后续副作用”边界。

Local `approval_required` 模式安装了 pre-ToolNode batch barrier：当一个模型批次包含
`bash` 或 `task` 时，只保留原始第一个 tool call，后续调用由下一次模型步骤根据真实
结果重新规划。这避免兄弟 handler 在审批前并发启动。它不是一个隐藏的批量授权：
后续每条实际 Bash 仍产生自己的新审批。

该 barrier 不安装在明确受信任的内置隔离 Provider、Local disabled 或
`allow_host_bash: true` 兼容模式中。

工具返回的 `Command(goto=END)` 本身不能覆盖 `create_agent` 固定的 tools-to-model
边；如果只依赖它，ToolMessage 后仍可能再进入一次模型节点。Local
`approval_required` 因此还安装 checkpoint-safe pause middleware：审批 ToolMessage
以同步 durability 完成 checkpoint 后，下一次模型调用前通过受支持的 `jump_to=end`
退出当前 Agent 图。Worker 同时按精确 `source_run_id` 识别该 artifact，跳过 active-goal
evaluator 和隐藏续跑；只订阅 custom 流时会在自然结束后从物化 checkpoint 复核。旧 Run
的 artifact 不能暂停新 Run。

同步 checkpoint 只用于 Local `approval_required` Run。AIO、Local disabled 和 legacy
direct 不安装 pause middleware，也保持默认 durability，避免把本机审批的维护成本扩散到
隔离执行路径。Lead 与子代理使用同一 gate；子代理审批冒泡到父 task 后，Lead 也必须在
下一模型调用前停止。

## 9. 崩溃恢复与“不自动重试副作用”

本功能把“批准”与“已安全执行”分开：

- claim 前失败：没有启动进程，授权不应被模型绕过；
- 能确定进程未创建：写 `launch_failed` receipt；
- 进程启动后抛错、结果无法确认或 completion 无法可靠落库：收敛为 `unknown`，不自动
  再执行；
- `finished` 或 `launch_failed` receipt 已落库但 Worker 在输出前崩溃：重试只读取 receipt
  并重新投递隐藏结果，不访问 provider、不 spawn；
- `present_files` intent 已提交但 checkpoint 或 Worker 内存丢失：新 Worker 从数据库恢复
  精确路径；Artifact 与 `delivered` 已提交但 Job 尚未结算时，重试复用已有 Artifact，不重复
  创建，也不重跑命令；
- 已 `claimed` 但没有 receipt，且原执行 lease 已失效：收敛为 `unknown`，不会重新消费
  `allow_once`，未完成交付义务同时收敛为 `blocked_unknown`。

`unknown` 不创建可重放 receipt。active/by-id 读取会惰性收敛已失 lease 的 claimed
请求；缺失、畸形或 scope 不匹配的 receipt 在投影和 replay 两端都 fail closed。

receipt 与 approval、continuation Job、Job attempt 通过数据库复合约束绑定。完成事务同时
只解除该次 host spawn 对应的精确围栏；只有不存在其他未解决副作用围栏时，Job 才恢复为
可安全重放。receipt 之前已有的 MCP、文件写入或其他副作用围栏，以及 receipt 之后新产生的
围栏，都不能被这条 receipt 全局清除。
continuation 成功结算还会在同一事务重新检查持久交付状态，因此模型输出、旧 checkpoint
或进程内缓存都不能绕过文件交付门禁。

这只能防止 ActWeave 在不确定状态下自动重放同一启动。它不能撤销命令已经写入的文件、
已发送的网络请求、已修改的数据库，也不能保证 Shell 启动的后台进程已经停止。

## 10. 安全限制

- Local 执行是宿主 RCE。命令可访问 Worker OS 账号能够访问的文件、进程、网络和本机
  服务；不要把它用于多租户或不可信输入。
- Bash audit 先施加 10,000 字符输入上限；冻结审批合同另有 65,536 UTF-8 bytes
  上限，tool call id 上限是 128 UTF-8 bytes。超过任一上限都不会生成审批。
- 路径校验、配置 guardrail、空值/超长/空字节检查和秘密明文扫描发生在审批前；
  审批不能覆盖这些 hard deny。`SandboxAudit` 对语法有效的高风险命令只做分类并让其进入
  Local 审批，确保用户能看到准确原文；风险标签本身不是隔离或安全许可。
- 命令中的 Unicode 双向文本控制字符在生成审批前直接拒绝，避免界面中看到的字符顺序
  与 Shell 实际收到的字符顺序不一致。
- 冻结计划如果引用活动环境变量/Credential，仅保存变量名还不足以证明 continuation
  使用的是同一 Credential 版本。当前实现对此 fail closed，不会按名字重新取值；后续只有
  在持久化 secret-free Credential binding closure 后才能安全支持。
- 输出会被截断并屏蔽已知秘密/本机路径；这只是减少意外泄露，不是 DLP。
- 一个合法命令的未知副作用无法由审批系统判断。用户应优先批准可重复、作用域窄、
  不带后台进程的命令，并在 `unknown` 后人工检查宿主状态。
- 当前没有“本次会话都允许”、通配命令、命令前缀授权或第二密码。每条新的 Bash 调用都
  必须重新审批。

需要执行不可信 Agent 代码时，应使用明确受信任的内置 AIO + Apple Container、
BoxLite、E2B 或受控 Kubernetes Provisioner，并只暴露必要的只读/读写 mount。

## 11. 持久化与接口

该能力使用：

- `execution_approval_requests`：owner-private 冻结计划、决定、claim 与终态；
- `execution_approval_result_receipts`：与精确 execution Job/attempt 绑定的有界结果；
- `execution_approval_output_delivery_obligations`：approval 与 continuation 的持久交付状态；
- `execution_approval_output_delivery_candidates`：来源 Run 的精确文件版本与摘要候选；
- owner-private active/by-id/decision API；
- checkpoint ToolMessage 中的最小 `host_execution_approval` artifact。

新空库通过 `make setup-db` 直接记录当前链头
`model_catalog_simplify`。已在受支持链上的 `initial_schema` 或
`approval_output_delivery` 数据库必须先备份，再在维护窗口使用 `make upgrade-db` 显式
升级。使用预发布
`full_schema` / `execution_approvals` marker 的数据库仍须换空目标重建，不能手工
stamp 或原地修补；运行时始终不会自动创建、升级或修补这些表。

## 12. 当前验证状态

当前 checkout 已包含后端审批 API/生命周期、schema、Worker continuation runner、
Local/AIO 分流、子代理上下文、batch barrier，以及前端 schema、轮询、恢复和卡片的聚焦
自动化测试。自动化测试能够验证确定性状态转换和“receipt replay 不再次 spawn”等合同，
但不能替代真实环境验收。

后端 PostgreSQL 生命周期测试还固定了公开可见性边界：内部 `staged` 请求通过 active
与 by-id 读取都返回 `approval: null`；来源 Run 成功结算成真正 `pending` 后，两条读取
路径才返回同一份可立即决定的审批投影。来源 Job 在 staged 期间终止时，请求先保持隐藏，
再以 `cancelled` 终态通过 by-id 可见。

自动化还覆盖了本轮暂停语义：Lead 与子代理在 Local `approval_required` 下安装同一
checkpoint-safe pause，Worker 在当前 Run 的 approval artifact 存在时跳过 active-goal
evaluator 与隐藏续跑，且同步 durability 只作用于 Local 审批模式。前端测试固定了
`staged` 不生成决定卡片、只有 `pending` 进入决定表面、决定后卸载卡片而恢复锚点继续在
内部收敛的合同。

持久交付义务的自动化覆盖来源输出固化、无义务路径、allow 原子绑定、deny/过期/取消/
unknown 收敛、精确 candidate 或同路径 continuation 后继的交付、intent 与 Artifact 的
崩溃重放，以及 continuation 终态数据库门禁。测试同时固定旧 Run/旧 Thread 的审批不能
暂停当前 Run，且 receipt replay 只恢复交付要求，不会再次 spawn 已消费的命令。

2026-08-15 本轮针对当前 checkout、真实模型和 native macOS Local 模式完成了 Lead
拒绝与 `allow_once` 浏览器验收：

- `staged` 阶段没有显示半成品卡片；审批卡第一次可见时，“拒绝”和“允许一次”已经在
  同一观测帧出现且可用；
- 卡片可见时没有同时显示 `Thinking` 或 `RunActivity`；Worker 日志显示，从审批 staged
  到用户决定之间没有额外模型请求；
- 拒绝后的卡片约 `293 ms` 卸载，且没有启动命令；允许后的卡片约 `711 ms` 卸载，随后
  只产生一次 continuation 和一条执行结果；
- 允许后的下一次模型请求发生在用户决定之后，用于消费权威执行结果，而不是在等待审批时
  继续思考或替换冻结命令。
- `general-purpose` 子代理拒绝路径也完成真实模型 live。标记
  `approval-subagent-live-20260815-2352` 的卡片首次观察于 `15:53:13.991`，两枚按钮同帧
  可用且没有 `RunActivity`；`15:53:28.921` 拒绝后约 `419 ms` 卸载。Worker 日志显示来源
  Run 在 stage、成功结算与用户决定之间没有额外模型请求；数据库记录的
  `agent_path=[lead, subagent:general-purpose]`、状态 `denied`、continuation 为空且 receipt
  数量为 0，证明拒绝没有准入执行。
- 修复子任务状态投影后，标记 `approval-subagent-wait-live-20260816-0010` 再次完成
  `general-purpose` 拒绝 live：pending 时子任务胶囊显示“等待审批”，子任务运行中、
  `Thinking` 与“正在运行”计数均为 0，决定按钮同帧可用；拒绝后约 `440 ms` 卡片卸载，
  胶囊变为“已拒绝”、运行中仍为 0，输入框恢复可用。

本轮真实模型环境没有暴露 `create_goal`，因此 active-goal 路径没有完成 live 验收；当前
结论只来自 Worker 聚焦自动化。`general-purpose` 子代理本轮完成的是拒绝 live，未把该证据
扩展为子代理 `allow_once` live；Lead/子代理中间件装配和父图暂停另由自动化覆盖。真实
Worker `SIGKILL`、数据库锁暂停等故障注入和 AIO runtime 也未在本轮重跑。未来发布仍必须
针对当时 checkout、数据库和目标 provider 重跑相应门禁，并只报告实际运行过的结果。

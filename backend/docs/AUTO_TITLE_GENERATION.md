# Thread Title 自动生成

`TitleMiddleware` 在首轮用户消息得到 Agent 完整回复后生成 Thread title。标题属于 Agent state，并随项目 scoped PostgreSQL checkpointer 持久化；首轮 Run 成功结束后，Worker 将标题同步到项目私有 Thread 服务，供 Web 会话列表读取。

## 生成规则

1. `title.enabled=false` 时不生成。
2. state 已有非空 title 时不覆盖。
3. 只在首个完整 user/assistant exchange 成功结束后运行一次；中断、取消或失败的 Run 不生成也不同步自动标题。
4. `title.model_name` 为空时使用系统默认模型生成标题。Run 准入会把当时的默认模型冻结进快照；Worker 按该快照调用。没有可用默认模型或模型调用失败、返回空标题时，回退到从首条用户消息截取的本地标题。
5. 配置 `title.model_name` 后，middleware 调用指定模型；同样在失败或空标题时回退到本地标题。
6. 结构化 message content 会先归一化为文本，reasoning `<think>` 内容不会进入标题。
7. 会话被手动重命名后，Worker 的自动标题同步不会覆盖该名称；第二轮及后续轮次不再触发自动标题。

在授权被撤销时，标题模型调用与其他 side-effect boundary 一样 fail closed，不会吞掉 `AuthorizationRevoked`。

## 配置

标题策略由平台管理员在 `/admin/settings/system` 的对话体验页维护，权威来源是 PostgreSQL，不是 `config.yaml`。

- `enabled`：是否启用自动标题。
- `max_words`：提供给标题 prompt 的目标词数。
- `max_chars`：最终标题长度上限。
- `model_name`：可选逻辑模型名；`null` 表示使用系统默认模型。
- `prompt_template`：部署侧自定义 prompt，必须兼容当前配置模型要求。

配置变更边界以 [`CONFIGURATION.md`](./CONFIGURATION.md) 和 `config.example.yaml` 为准。

## 代码与测试

- Middleware：`packages/harness/deerflow/agents/middlewares/title_middleware.py`
- 配置：`packages/harness/deerflow/config/title_config.py`
- 组合与失败边界：`tests/test_create_deerflow_agent.py`、`tests/test_run_worker_rollback.py`
- Project Thread/Run 集成行为：Replay E2E、private-work router 与 PostgreSQL 门禁

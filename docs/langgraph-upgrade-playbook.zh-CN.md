# LangChain / LangGraph 升级 Playbook

> 配套 U5（升级防线）。`backend/packages/harness/pyproject.toml` 中
> `langchain` / `langchain-core` / `langgraph` 带 minor 上界，本文是解除或抬高
> 上界时的操作手册。原则：**锁文件钉精确版本，上界防手滑，契约测试当金丝雀，
> checkpoint 补丁逐个核对后才允许跟着新版本走。**

## 1. 适用范围与原则

- 三个受门禁包：`langchain`、`langchain-core`、`langgraph`（上界见
  `backend/packages/harness/pyproject.toml`，当前 `<1.4` / `<1.5` / `<1.3`）。
  周边包（`langgraph-api`、`langchain-openai` 等）无上界，但升级后同样要过
  金丝雀清单。
- ActWeave 深度依赖上游**未成文行为**（`after_model` 逆序派发、wrap 组合方向、
  checkpoint 内部结构），这些行为由 `backend/tests/test_langchain_contract.py`
  显式钉住。升级的本质是：先让契约测试告诉你上游改了什么，再决定代码怎么跟。
- 任何升级 PR 必须同时包含：新 lock、（如需）新上界、金丝雀结果、
  `checkpoint_patches.py` 复核结论。缺一不合并。

## 2. 升级步骤

1. **升级前基线**：在当前版本跑一遍金丝雀清单（第 3 节），确认全绿。
2. **单独升级目标包**：

   ```bash
   cd backend
   # 按需抬高 packages/harness/pyproject.toml 中的上界，然后：
   uv lock --upgrade-package langgraph  # 或 langchain / langchain-core
   uv sync
   ```

   不要裸跑 `uv lock --upgrade` 整体升级——上界只防 minor 级手滑，
   周边包一起动会让金丝雀失败无法归因。
3. **先跑契约测试**：`uv run pytest tests/test_langchain_contract.py -q`。
   失败即上游行为变化，逐条对照第 5 节决定"改代码适配"还是"补丁站岗/退役"。
4. **复核 `checkpoint_patches.py`**（第 4 节）：两枚补丁各自核对上游源码与
   探针结果，更新 `_PATCH_VALIDATED_LANGGRAPH_VERSION` 或删除补丁。
5. **跑完整金丝雀清单**（第 3 节顺序），随后跑 backend 全量
   `POSTGRES_TEST_URL=... make test`。
6. **full/delta 双模式矩阵**（第 6 节）逐格核对，真 PostgreSQL 用例必跑。
7. 升级 PR 里写清楚：上游 changelog 相关条目、契约测试的差异与处置、
   补丁复核结论。

## 3. 金丝雀清单（按顺序）

| 顺序 | 测试 | 看住什么 |
|---|---|---|
| 1 | `tests/test_langchain_contract.py` | `after_model` 逆序派发、`wrap_model_call`/`wrap_tool_call` 组合方向与短路、`create_agent` 参数形态、checkpoint 补丁依赖的上游内部结构、版本上界自检 |
| 2 | `tests/test_agent_assembly_golden.py` | 三条装配链的精确中间件序列与共享脊椎投影 |
| 3 | `tests/test_create_deerflow_agent.py` | SDK factory 全部参数形态与特性开关 |
| 4 | `tests/test_clarification_middleware.py` | clarification 中断/恢复（依赖上游 interrupt 语义） |
| 5 | `tests/test_client.py` | 嵌入客户端复用 lead 链 + DeltaChannel 行为 |
| 6 | `tests/test_worker_service.py` | Worker 生命周期与 subagent detach 清理 |
| 7 | `tests/test_task_tool_event_wakeup.py` | subagent 事件驱动等待（跨线程 loop 语义） |
| 8 | `tests/test_memory_archive_receipt_postgres.py`（真 PG） | 真 StateGraph 阈值压缩、checkpoint 回执激活、full/delta 双模式 |

全绿后再跑 backend 全量与 frontend `pnpm check && pnpm test`。

## 4. `checkpoint_patches.py` 补丁台账

补丁位于 `backend/packages/harness/deerflow/checkpoint_patches.py`，随
`deerflow.agents.thread_state` 导入在每个进程生效。**每次升级都要逐枚复核。**

### 4.1 `ensure_inmemory_delta_history_patch`

- **上游缺陷**：`InMemorySaver.get_delta_channel_history` 的单遍融合实现，
  在通道 blob 版本由祖先 checkpoint 携带前进（full → delta 迁移后的第一个
  superstep 正是这种形态）时，把终止 checkpoint 自己的 pending writes 误判为
  "已被 blob 吸收"而丢弃——迁移后追加的第一条消息从物化状态里消失。
- **补丁行为**：把 InMemorySaver 的 sync/async 两个方法委托回
  `BaseCheckpointSaver` 的正确实现（先收集终止 checkpoint 的 writes，再把
  blob 当种子）。
- **站岗条件**：上游 override 仍存在（`_upstream_override_present()` 为真）。
  验证版本 `_PATCH_VALIDATED_LANGGRAPH_VERSION = 1.2.9`，更新版本超过它时
  启动日志出 warning，提示重新核对。
- **何时可删**：上游修正或移除该 override（补丁自动 stand down 后观察一个
  版本周期）；删除时同步删 `test_langchain_contract.py` 里对应的结构断言。

### 4.2 `ensure_binop_overwrite_first_write_patch`

- **上游缺陷**（#4380）：`BinaryOperatorAggregate.update` 对空通道
  （Union 类型无可构造默认值，起始 MISSING）直接存入 `values[0]`，不做
  `Overwrite` 解包——线程分支或 `/state` 替换式写入会把 `Overwrite` 包装对象
  原样写进 checkpoint，下一个读者崩 `TypeError: 'Overwrite' object is not
  subscriptable`。
- **补丁行为**：仅拦截"空通道 + 首值为 Overwrite"一种形态，解包后镜像上游
  同批次语义（后续普通值跳过、第二个 Overwrite 抛 `InvalidUpdateError`），
  其余全部委托原实现。
- **站岗条件**：行为探针 `_binop_first_write_stores_overwrite_wrapper()` 为真
  （用 Union 通道实测上游是否仍存包装对象）。探针为假时补丁自动不装。
- **何时可删**：新版本上探针返回 False（上游自己解包了）；确认
  `DeltaChannel.update` 与 `BinaryOperatorAggregate.update` 行为已一致后删除，
  并同步删除契约测试中的探针断言。

## 5. 契约测试失败的处置矩阵

| 失败组 | 含义 | 处置 |
|---|---|---|
| `after_model` 派发顺序 | 上游改了 hook 派发方向 | Safety/Loop/Subagent 等依赖逆序的注册位全部重审（见 `lead_agent/agent.py` 注释），改完必须让 golden 测试同步变更 |
| wrap 组合方向/短路 | 中间件包裹层级反转 | 审 InputSanitization 最外层、ToolProgress 包住 ToolErrorHandling 两条不变量 |
| `create_agent` 参数形态 | 工厂签名变化 | 改 `factory.py` / `make_lead_agent` 调用点，同步 SDK 文档 |
| checkpoint 内部结构 | 补丁依赖的私有结构变了 | 走第 4 节台账，逐枚决定跟进或退役 |
| 版本上界自检 | lock 里的版本越过上界 | 说明有人绕过流程升级，回到第 2 节 |

## 6. full / delta 双模式验证矩阵

`database.checkpoint_channel_mode` 两个值都是受支持的生产形态，升级后逐格核对：

| 行为 | full | delta | 覆盖用例 |
|---|---|---|---|
| 新线程首轮写入 | ✅ | ✅ | `test_client.py`、`test_create_deerflow_agent.py` |
| 断点续跑 / 重放 | ✅ | ✅ | `test_memory_archive_receipt_postgres.py`（真 PG） |
| full → delta 迁移后首条消息 | — | ✅ | `test_langchain_contract.py` 补丁结构断言 + 4.1 补丁自身逻辑 |
| 线程分支 / 替换式状态写入 | ✅ | ✅ | 4.2 补丁探针 + `test_human_input_response_promotion.py` |
| SNIP 压缩回执激活 | ✅ | ✅ | `test_memory_archive_receipt_postgres.py` |
| 嵌入客户端（InMemorySaver） | ✅ | ✅ | `test_client.py` |

矩阵中任何一格失败，升级 PR 不得合并；delta 列失败而 full 列通过时，允许
临时把部署钉在 full 模式发布，但必须开跟踪 issue 在下个周期解决。

# RAG Knowledge Package M6（Agent Tool + Citation）Implementation Plan

## 目标

让 Lead Agent 调用 M5 SearchService，并把结构化 Knowledge Citation 保存到消息中。

## 文件范围

```text
backend/app/knowledge/run_tool.py
backend/app/reliability/run_execution/executor.py
backend/app/worker/app.py
backend/packages/harness/deerflow/client.py
backend/tests/knowledge/test_agent_tool.py
backend/tests/test_client.py
frontend/src/core/threads/message-projection.ts
frontend/tests/unit/core/threads/
```

## Task 1：工具工厂

在 `RunAgentPrivateExecutor` 的普通 chat Run 组装：

```python
def create_knowledge_search_tool(module, project_id): ...
```

- `project_id` 使用 `execution.context.project_id`；
- Run 使用现有 `shared_assets.execute` 能力，不新增 Knowledge 专用能力；
- feature disabled 时不注入工具；
- Package 不 import Harness；
- Harness 不 import Knowledge Package。

M4 在 Worker 生命周期中创建的 `KnowledgeModule` 注入 `RunAgentPrivateExecutor`。executor 只在普通 chat Run 且 feature enabled 时创建当前 Project 绑定的 factory wrapper。

`run_tool.py` 的 wrapper `private_runtime_factory` 显式保留原 factory 的完整 keyword-only 签名，只覆盖 `trusted_extension=TrustedLeadAgentExtension(extra_tools=(tool,))`，再委托给现有 `_make_lead_agent_with_private_runtime`。不修改 SDK `create_deerflow_agent`，Skill Builder Run 也不注入 Knowledge 工具。

## Task 2：工具契约

模型可见签名：

```text
knowledge_search(query: str, top_k: int = 4)
```

工具调用 `KnowledgeModule.search()`，不复制检索逻辑。实现使用隐藏的 `InjectedToolCallId`，并返回 `Command(update={"messages": [ToolMessage(...)]})`；模型可见参数仍只有 query 和 top-k。

返回给模型的每项包含 Base name、Document name、snippet、score 和 source position。没有命中时返回空 items。

`score` 是 M5 Reranker 返回的 `relevance_score`，不是 cosine 候选分数。工具不向模型暴露 `score_threshold`，内部使用与 HTTP 检索相同的包内默认阈值；全部候选低于阈值时返回空 items。

## Task 3：Citation 消息

每次工具成功后，ToolMessage 的 `content` 保存给模型读取的 items JSON，`additional_kwargs["knowledge_citations"]` 保存完整 `KnowledgeCitation`：

```json
{
  "items": [
    {
      "knowledge_base_id": "...",
      "knowledge_base_name": "...",
      "document_id": "...",
      "document_name": "...",
      "segment_id": "...",
      "segment_position": 1,
      "snippet": "...",
      "score": 0.83,
      "source_position": {"page": 2}
    }
  ]
}
```

生产 Worker live messages、values 与 RunJournal replay 已保留 `additional_kwargs`。补齐 `DeerFlowClient._tool_message_event()`，使其 normalized `messages-tuple` 路径与这些路径一致。

`projectThreadMessages()` 在 `mergeMessages()` 得到最终、已带 Run id 的消息序列后执行 Citation 投影：

1. 按消息顺序收集同一 Run 中成功的 `knowledge_search` ToolMessage；
2. 按首次出现顺序合并 `items`，以 `segment_id` 去重；
3. 把完整 `knowledge_citations` 附到该 Run 最后一条可见 AI 文本消息；
4. ToolMessage 缺少或具有非法的 `additional_kwargs.knowledge_citations`，或该 Run 没有最终可见、非空文本 AI Message 时，不生成 Citation。

实时消息和历史 replay 都调用这一投影，Citation 渲染组件只读取最终 AI 消息。

## Task 4：错误行为

- 无命中：成功空列表；
- query 非法：`KNOWLEDGE_INVALID_REQUEST`；
- 模型失败：`KNOWLEDGE_MODEL_UNAVAILABLE` 或 `KNOWLEDGE_EMBEDDING_FAILED`；
- Reranker 失败：`KNOWLEDGE_RERANK_FAILED`；
- 数据库失败：`KNOWLEDGE_SEARCH_FAILED`。

Agent 可以根据错误继续回答，但不能把错误转换为虚假 Citation。

## Task 5：端到端测试

- feature enabled/disabled 的工具注入；
- 工具使用当前 Run Project；
- 工具与 HTTP 检索结果一致；
- 无命中；
- 全部候选低于阈值时返回空 items 且不产生 Citation；
- Embedding、Reranker 和数据库错误；
- `_tool_message_event()` 保留 `additional_kwargs.knowledge_citations`；
- 同一 Run 多次工具调用按 `segment_id` 稳定去重并附到最终 AI 消息；
- ToolMessage 持久化后 replay；
- 刷新前后 Citation 相同。

## 放行门

- 真实 Worker Agent Run 能调用工具；
- 工具只依赖 app Adapter 和 Package Interface；
- Citation 可持久化和刷新恢复；
- M0–M5 回归通过。

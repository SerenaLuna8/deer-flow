# RAG 检索模块技术设计（MVP）

## 1. 目标

对一个 Project 当前启用的 Knowledge Base 执行两阶段检索，并返回可直接展示的 Knowledge Citation：

```text
query embedding
    -> pgvector exact cosine 召回候选
    -> Reranker 重排候选
    -> 返回全局 top-k
```

向量相似度只用于扩大候选范围，Reranker 的 `relevance_score` 决定最终顺序。MVP 不建设向量索引、关键词融合和混合检索。

## 2. 接口

```python
@dataclass
class KnowledgeSearchRequest:
    project_id: UUID
    query: str
    knowledge_base_ids: tuple[UUID, ...] | None = None
    top_k: int | None = None
    score_threshold: float | None = None

@dataclass
class KnowledgeCitation:
    knowledge_base_id: UUID
    knowledge_base_name: str
    document_id: UUID
    document_name: str
    segment_id: UUID
    segment_position: int
    snippet: str
    score: float
    source_position: dict[str, object]

@dataclass
class KnowledgeSearchResult:
    citations: tuple[KnowledgeCitation, ...]
```

`project_id` 由宿主上下文提供。HTTP body 和 Agent 参数只提交 query、可选 Base ids 和 top-k。

`KnowledgeCitation.score` 是 Reranker 返回的 `relevance_score`，不是 cosine score。

## 3. 请求规则

- query 执行普通 `strip()` 后不能为空，且不超过 2000 字符；
- `top_k` 范围 1..20；
- `score_threshold` 范围 0..1，未提供时使用包内常量 `DEFAULT_SCORE_THRESHOLD = 0.2`（M2 对真实 Provider 联调时校准一次），0 表示不过滤；
- 未提供 Base ids 时选择 Project 全部 active Base；
- 提供 Base ids 时忽略 disabled/deleting Base；
- 没有可搜索 Base 时返回空结果。

## 4. 检索流程

1. 读取目标 Project 的 active Knowledge Base。
2. 按 `model_configuration_id` 分组。
3. 每组使用配置中的 `embedding_model` 生成一次 query embedding。
4. 校验 query embedding 的维度、有限数值且不为全零。
5. 对该组 Base 执行 pgvector exact cosine 查询，每组召回：

   ```text
   candidate_k = min(100, max(20, top_k * 5))
   ```

6. 候选为空时直接跳过该组，不调用 Reranker。
7. 使用同一配置中的 `reranker_model`，以原始 query 和候选 Segment 全文按 `reranker_max_batch` 分批调用 `/rerank`。
8. 跨批合并后按 `relevance_score` 排序，丢弃低于 `score_threshold` 的候选，保留该组前 `top_k` 条。
9. 合并各组结果，按 Reranker score 降序返回全局前 `top_k` 条；全部候选低于阈值时返回空 citations。

默认初始化只有一条 Knowledge Model Configuration，因此常规检索只调用一组模型。以后存在多条配置时，各配置组分别重排；不同 Reranker 的 score 不保证已经校准，这是多配置场景的已知限制。

Reranker 失败时整次检索失败，不允许静默退化成 cosine-only 结果。

## 5. 候选召回 SQL

embedding 直接保存在 `knowledge_segments.embedding`：

```sql
SELECT
    s.id,
    s.position,
    s.content,
    s.source_position,
    d.id AS document_id,
    d.name AS document_name,
    b.id AS knowledge_base_id,
    b.name AS knowledge_base_name,
    1 - (s.embedding <=> CAST(:query_embedding AS vector)) AS vector_score
FROM knowledge_segments AS s
JOIN knowledge_documents AS d ON d.id = s.knowledge_document_id
JOIN knowledge_bases AS b ON b.id = d.knowledge_base_id
WHERE b.project_id = :project_id
  AND b.id = ANY(:knowledge_base_ids)
  AND b.status = 'active'
  AND d.status = 'ready'
  AND s.document_version = d.version
  AND b.model_configuration_id = :model_configuration_id
ORDER BY
    vector_score DESC,
    b.id ASC,
    d.id ASC,
    s.position ASC,
    s.id ASC
LIMIT :candidate_limit;
```

对不同 dimension 的配置分别执行 SQL，避免把不同维度向量放入同一次距离计算。

`vector_score` 仅用于候选召回和 Reranker 同分时的稳定排序，不直接作为 Citation score 返回。

## 6. Reranker 调用

请求示例：

```json
{
  "model": "Qwen/Qwen3-VL-Reranker-8B",
  "query": "用户问题",
  "documents": ["候选段落一", "候选段落二"],
  "top_n": 4,
  "return_documents": false
}
```

调用 `POST {base_url}/rerank`，复用 Knowledge Model Configuration 的 API key 和请求超时。候选超过 `reranker_max_batch` 时分批调用，每批 `top_n = min(top_k, 批内候选数)`，返回 index 按批内偏移映射回原候选，跨批直接按 `relevance_score` 合并。

返回结果必须满足：

- `results` 存在且为数组；
- 每项的 `index` 指向本次提交的候选；
- `index` 不重复、不越界；
- `relevance_score` 是有限数值；
- 有效返回数量不超过请求的 `top_n`。

Provider 返回非法结果、超时或非成功状态时返回 `KNOWLEDGE_RERANK_FAILED`。

## 7. 排序与数量

内部稳定排序键：

```text
rerank_score DESC,
vector_score DESC,
knowledge_base_id ASC,
document_id ASC,
segment_position ASC,
segment_id ASC
```

- 排序前丢弃 `relevance_score` 低于 `score_threshold` 的候选；全部低于阈值时返回空结果；
- 请求未提供 `top_k` 时使用 4，最大为 20；
- 相同 Segment 只返回一次；
- snippet 默认取 Segment 全文，最多展示前 320 字符；
- Citation 的 `score` 写入 `rerank_score`。

## 8. SearchService

```python
class SearchService:
    async def search(self, request: KnowledgeSearchRequest) -> KnowledgeSearchResult:
        bases = await self.store.list_searchable_bases(...)
        grouped = group_by_model_configuration(bases)
        ranked = []

        for configuration, group in grouped:
            query_vector = await self.models.embed_one(configuration, request.query)
            candidates = await self.store.cosine_search(
                group,
                query_vector,
                candidate_limit=calculate_candidate_k(request.top_k),
            )
            if not candidates:
                continue

            ranked.extend(
                await self.models.rerank(
                    configuration,
                    request.query,
                    candidates,
                    top_n=request.top_k,
                )
            )

        return assemble_result(ranked, request.top_k, request.score_threshold)
```

Project HTTP 检索和 Agent 工具必须复用同一个 `SearchService.search()`。Embedding 与 Reranker 的 Provider 细节都留在 Knowledge 软件包内部。

## 9. Agent 工具

工具签名：

```text
knowledge_search(query, top_k=4)
```

工具不向模型暴露 `score_threshold`，内部使用与 HTTP 检索一致的包内默认阈值。

工具返回：

```json
{
  "items": [
    {
      "knowledge_base_name": "产品手册",
      "document_name": "安装指南.pdf",
      "snippet": "...",
      "score": 0.91,
      "source_position": {"page": 12}
    }
  ]
}
```

宿主同时保存完整 `KnowledgeCitation`，前端据此渲染来源。没有候选时返回 `{"items": []}`。

## 10. 错误

```text
KNOWLEDGE_INVALID_REQUEST
KNOWLEDGE_MODEL_UNAVAILABLE
KNOWLEDGE_EMBEDDING_FAILED
KNOWLEDGE_RERANK_FAILED
KNOWLEDGE_SEARCH_FAILED
```

模型超时、Provider 非法响应、数据库错误和维度不匹配返回相应业务错误，不伪装成零命中。

## 11. 测试

- 单 Base 先召回候选再返回 top-k；
- Reranker 能改变 cosine 候选的原始顺序；
- Citation score 等于 `relevance_score`；
- Reranker 失败时返回 `KNOWLEDGE_RERANK_FAILED`，不得返回 cosine-only 结果；
- 无候选时不调用 Reranker；
- `candidate_k` 的默认值、下限和上限正确；
- `score_threshold` 默认值、请求覆盖、0 不过滤和全部低于阈值返回空；
- query 超过 2000 字符返回 `KNOWLEDGE_INVALID_REQUEST`；
- 候选超过 `reranker_max_batch` 时分批 rerank 与跨批合并；
- 多 Base 合并；
- 多模型配置分组调用；
- disabled/deleting Base 不参与；
- 非 ready Document 和旧 Document version 不参与；
- 相同 rerank score 的稳定排序；
- 无 Base 和无命中；
- embedding/数据库失败；
- query embedding 全零；
- HTTP 检索与 Agent 工具结果一致；
- Citation 的 Base、Document、Segment 和来源位置正确。

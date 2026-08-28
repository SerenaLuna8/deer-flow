# RAG Knowledge Package M5（Semantic Retrieval）Implementation Plan

## 目标

实现 Project 两阶段检索测试和可复用 SearchService：pgvector exact cosine 负责候选召回，Reranker 负责最终排序。

## 文件范围

```text
backend/packages/knowledge/actweave_knowledge/retrieval/
backend/packages/knowledge/actweave_knowledge/contracts.py
backend/app/knowledge/gateway.py
backend/tests/knowledge/test_retrieval.py
```

## Task 1：搜索契约

```python
KnowledgeSearchRequest(project_id, query, knowledge_base_ids=None, top_k=None, score_threshold=None)
KnowledgeSearchResult(citations)
KnowledgeCitation(...)
```

- query strip 后非空且不超过 2000 字符；
- top-k 未提供时默认 4，范围 1..20；
- `score_threshold` 范围 0..1，未提供时使用包内常量 0.2，0 表示不过滤；
- Base ids 可选；
- 没有 active Base 或没有命中时返回空 citations。

## Task 2：候选 Base

- 只选当前 Project 的 active Base；
- 指定 Base ids 时取其 active 子集；
- 只检索 ready Document；
- Segment 的 `document_version` 必须等于 Document 当前 version。

## Task 3：query embedding

- 按 `model_configuration_id` 对 Base 分组；
- 每个配置生成一次 query embedding；
- 检查配置 active、返回 dimension、有限数值和非零向量；
- 一组失败时整次请求返回模型错误。

## Task 4：exact cosine 候选召回

- 对每个模型组执行 `<=>`；
- 每组取 `candidate_k = min(100, max(20, top_k * 5))` 个候选；
- 计算 `vector_score = 1 - cosine_distance`；
- `vector_score` 只用于候选召回和 Reranker 同分时的稳定排序。

SQL 以《RAG检索模块技术设计文档》第 5 节为准。

## Task 5：Reranker 精排

- 候选不为空时，使用同一 Knowledge Model Configuration 的 Reranker；
- 发送原始 query 和候选 Segment 全文；
- 候选按 `reranker_max_batch` 分批调用 `/rerank`，每批 `top_n = min(top_k, 批内候选数)`，跨批按 `relevance_score` 合并；每组丢弃低于 `score_threshold` 的候选后最多保留 `top_k` 条；各组结果合并后再保留全局 `top_k`；
- 按 `relevance_score`、`vector_score`、Base/Document/position/Segment id 排序；
- Citation 的 `score` 使用 `relevance_score`；
- 候选为空时不调用 Reranker；
- 全部候选低于阈值时返回空 citations；
- Reranker 失败或返回非法 index/score 时整次检索返回 `KNOWLEDGE_RERANK_FAILED`，不得静默退化成 cosine-only。

## Task 6：检索测试 API

```text
POST /api/projects/{project_id}/knowledge/search
```

请求：query、可选 Base ids、可选 top-k。响应：`KnowledgeSearchResult` 对象，其中 `citations` 是 Citation 列表。

Gateway 只负责解析宿主 Project 上下文，业务检索全部调用 `KnowledgeModule.search()`。
该 API 使用现有 `shared_assets.read` 能力。

## 测试

- 单 Base、多 Base；
- 不同模型配置分组；
- disabled/deleting Base；
- 非 ready 和旧 version Document；
- top-k 和稳定排序；
- Reranker 改变 cosine 候选顺序；
- Citation score 等于 `relevance_score`；
- Reranker 失败不返回 cosine-only 结果；
- 无候选时不调用 Reranker；
- 阈值默认值、请求覆盖、0 不过滤和全部低于阈值返回空；
- query 超长返回 `KNOWLEDGE_INVALID_REQUEST`；
- 候选跨批 rerank 与合并；
- 无 Base、无命中；
- 模型失败和数据库失败；
- query embedding 全零；
- Citation 内容和来源位置；
- HTTP 与 Package 结果一致。

## 放行门

- HTTP 集成测试调用 API 可返回经过 Reranker 精排的检索结果；
- 只读取当前可搜索数据；
- 多模型维度不进入同一次向量距离计算；
- M0–M4 回归通过。

# RAG Knowledge Package M2（Embedding + Reranker）Implementation Plan

## 目标

完成一组同时绑定 SiliconFlow Embedding 与 Reranker 的检索模型配置、双接口连接测试、模型调用和管理员 API。

## 文件范围

```text
backend/packages/knowledge/actweave_knowledge/models/
backend/packages/knowledge/actweave_knowledge/contracts.py
backend/app/knowledge/secret_adapter.py
backend/app/knowledge/gateway.py
backend/app/gateway/app.py
backend/tests/knowledge/test_models.py
```

## Task 1：模型契约

实现：

```python
KnowledgeModelConfigurationCreate
KnowledgeModelConfigurationUpdate
KnowledgeModelConfigurationView
KnowledgeModelConnectionResult
```

配置字段：`display_name`、`base_url`、`embedding_model`、`embedding_dimension`、`embedding_max_batch`、`reranker_model`、`reranker_max_batch`、`request_timeout_seconds`、`status`；View 另含派生布尔值 `in_use`。一个配置只保存一份两种模型共用的当前加密 API Key。`KnowledgeModelConnectionResult` 只有 `ok` 和可展示 `message`。

普通校验：必填字符串非空；base URL 是可解析的 HTTP/HTTPS URL；dimension、batch 和 timeout 在 SQL 允许范围内。

## Task 2：Secret Adapter

实现：

```python
protect_api_key(configuration_id, api_key) -> KnowledgeProtectedSecret
materialize_api_key(configuration_id, protected_secret) -> str
```

生产实现复用宿主已有 `SecretKey`/`SecretEnvelope`；`knowledge_model_configurations` 行保存当前 nonce/ciphertext。测试使用内存 fake，不增加 Secret 表或历史版本。

## Task 3：Provider client

实现内部 `KnowledgeModelClient`：

- 调用 `{base_url}/embeddings`；
- 按 `embedding_max_batch` 分批；
- 使用配置 timeout；
- 网络错误、429 和 5xx 最多重试一次；
- 恢复 Provider data index 顺序；
- 校验结果数量、dimension、有限数值和非零向量。
- 调用 `{base_url}/rerank`，发送原始 query、候选文本、`top_n` 和 `return_documents=false`；
- Rerank 候选按 `reranker_max_batch` 分批，每批 `top_n = min(top_n, 批内候选数)`，index 按批内偏移映射回原候选，跨批按 `relevance_score` 合并；
- 校验 Reranker 的 index 不重复且不越界，`relevance_score` 是有限数值，并按 index 映射回原候选。

连接不可用返回 `KNOWLEDGE_MODEL_UNAVAILABLE`；实际调用分别返回 `KNOWLEDGE_EMBEDDING_FAILED` 或 `KNOWLEDGE_RERANK_FAILED`。

## Task 4：配置服务

- create：依次测试 `/embeddings` 和 `/rerank`，都成功后加密 API Key，并在一个事务中写入配置和当前 nonce/ciphertext；
- update：始终允许名称、Embedding/Reranker batch、timeout、API Key；未被 Base 引用时还允许 status、base URL、embedding model、reranker model 和 dimension；API Key 或模型字段变化时重新测试两个接口，成功后才保存；
- 被 Base 引用时不允许停用，语义字段也不可修改；
- delete：只有无 Base 引用时允许；
- test：使用一条固定短文本测试 Embedding，再使用一个固定 query 和两条候选文本测试 Reranker，不创建业务数据。

## Task 5：admin API

```text
GET/POST  /api/admin/knowledge/models
PATCH     /api/admin/knowledge/models/{configuration_id}
DELETE    /api/admin/knowledge/models/{configuration_id}
POST      /api/admin/knowledge/models/{configuration_id}/test
GET       /api/projects/{project_id}/knowledge/model-options
```

Gateway 复用现有管理员和 Project 路由规则。Admin 路由继续使用 system admin 判断；Project `model-options` 使用 `shared_assets.read`。Admin 响应返回配置字段和 `in_use`；Project model-options 返回 active 配置的 id、display name、embedding model、embedding dimension 和 reranker model。

## 测试

- CRUD、`in_use`、启停和被引用配置限制；
- API Key 创建、更新和删除；
- `/embeddings` 与 `/rerank` 双接口连接测试成功/失败；
- 单条、批量和跨 batch 顺序；
- Reranker index 映射、score、top_n、空候选不调用及非法返回；
- Rerank 跨批分批、批内偏移映射和跨批合并顺序；
- timeout、429、5xx 和 4xx；
- 数量、dimension、NaN/Infinity 和全零向量；
- API Key 加密或配置事务失败时不留下配置；
- admin API 与 DTO。

## 放行门

- mock SiliconFlow Provider 的 Embedding 与 Reranker 测试全部通过；
- 管理员可以创建配置并同时测试两个模型接口；
- Package 仍不 import 宿主代码；
- M0+M1 回归通过。

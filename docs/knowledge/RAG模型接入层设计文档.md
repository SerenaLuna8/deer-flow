# RAG 模型接入层设计（MVP）

## 1. 范围

MVP 固定实现 SiliconFlow 的文本 Embedding 与 Reranker：

```text
POST {base_url}/embeddings
POST {base_url}/rerank
```

一个 Knowledge Model Configuration 同时绑定两个模型，共用同一个 Base URL、API Key 和请求超时。Knowledge Model Configuration 与 Agent 执行使用的 System Model Configuration 是两个对象。

本模块提供：

- 模型配置 CRUD；
- 首次数据库初始化 seed；
- API Key 保存接入；
- Embedding + Reranker 双接口连接测试；
- 单条和批量 Embedding；
- 候选 Rerank；
- 基础错误转换。

模型名称虽然包含 `VL`，但当前摄取和检索只传文本。图片、视频和 OCR 不因选用该模型而进入 MVP。

## 2. 数据模型

`knowledge_model_configurations`：

| 字段 | 说明 |
| --- | --- |
| `id` | 配置 id |
| `display_name` | 管理页面名称 |
| `status` | `active` 或 `disabled` |
| `base_url` | SiliconFlow API Base URL |
| `embedding_model` | Embedding model 名称 |
| `embedding_dimension` | 返回向量维度 |
| `embedding_max_batch` | 单次 Embedding 请求文本数量 |
| `reranker_model` | Reranker model 名称 |
| `reranker_max_batch` | 单次 Rerank 请求最多候选数量 |
| `request_timeout_seconds` | 两个接口共用的请求超时 |
| `created_at/updated_at` | 时间 |

同一行还保存当前共享 API Key 的 `api_key_nonce` 和 `api_key_ciphertext`。二者由宿主 `KnowledgeSecretPort` 生成和读取，不进入公开配置 DTO。

`KnowledgeModelConfigurationView.in_use` 由 Knowledge Base 引用查询派生，不增加数据库列。管理员页面用它禁用停用和删除操作；服务端仍在更新/删除时重新检查引用。

## 3. 首次数据库初始化

首次空库 `make setup-db` 和显式 `make reset-db` 初始化一条确定性配置：

```text
display_name         = SiliconFlow Qwen3-VL Retrieval
base_url             = https://api.siliconflow.cn/v1
embedding_model      = Qwen/Qwen3-VL-Embedding-8B
embedding_dimension  = 4096
embedding_max_batch  = 64
reranker_model       = Qwen/Qwen3-VL-Reranker-8B
reranker_max_batch   = 32
request_timeout      = 30 seconds
status               = active
```

初始化规则：

1. `backend/app/knowledge/bootstrap.py` 在 DDL 或 destructive reset 之前读取 installation-only `ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY`。
2. 使用现有 `ACT_WEAVE_SECRET_KEY` 创建 `SecretKey`，再按确定性配置 id 使用 `SecretEnvelope` 生成 nonce/ciphertext；bootstrap material 不保留明文的可打印表示。
3. Schema staged 后，Package bootstrap interface 在一个事务中确认模型目录为空并插入上述一行。
4. seed 成功后才继续发布 `schema_v1` marker；失败时沿用现有 staged-schema 失败处理，不伪装成已就绪数据库。
5. 初始化不发起外网连接测试，也不把 API Key 写入 SQL、日志或 Runtime 配置。
6. `ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY` 加入 `backend/scripts/run_runtime.py` 的 installation-only 过滤集合，正常 Gateway/Worker/Scheduler 不继承它。
7. 已完成 Schema V1 上的 `setup-db` 只读验证，不补写或覆盖配置。

这里初始化的是一条包含两个模型标识的检索配置，不创建两种模型行、全局 Reranker 指针或配对表。

官方资料确认 `Qwen/Qwen3-VL-Embedding-8B` 最大 Embedding dimension 为 4096，SiliconFlow 提供 `/embeddings` 和 `/rerank` 接口并支持上述两个模型：

- [SiliconFlow Embedding API](https://api-docs.siliconflow.cn/docs/api/embeddings-post)
- [SiliconFlow Rerank API](https://api-docs.siliconflow.cn/docs/api/rerank-post)
- [Qwen3-VL Embedding/Reranker 官方规格](https://github.com/QwenLM/Qwen3-VL-Embedding)

## 4. 配置操作

### 创建

请求字段：

```text
display_name
base_url
embedding_model
embedding_dimension
embedding_max_batch
reranker_model
reranker_max_batch
request_timeout_seconds
api_key
```

流程：

1. 校验普通字段；
2. 使用提交的 API Key 执行双接口连接测试；
3. 为配置生成 id，并由宿主 Secret Adapter 加密 API Key；
4. 在一个事务中创建模型配置并保存加密结果；
5. 返回配置视图。

任一接口测试、加密或数据库写入失败时不创建配置。

### 更新

- 可以更新 display name、Embedding/Reranker batch、timeout 和 API Key。
- API Key 更新后立即测试 `/embeddings` 与 `/rerank`；两者成功才保存。
- 被 Knowledge Base 引用时，不允许停用，也不允许修改 Base URL、Embedding model、Reranker model 和 dimension。
- 未被引用时可以编辑这些字段，但保存前必须重新测试两个接口。

MVP 只维护这一配置行的当前共享 API Key；更新当前值即可。

### 删除

只有未被任何 Knowledge Base 引用的配置可以删除。删除配置行会同时删除当前加密 API Key。

## 5. Secret Port

```python
class KnowledgeSecretPort(Protocol):
    def protect_api_key(
        self, configuration_id: UUID, api_key: str
    ) -> KnowledgeProtectedSecret: ...

    def materialize_api_key(
        self, configuration_id: UUID, secret: KnowledgeProtectedSecret
    ) -> str: ...
```

`KnowledgeProtectedSecret` 只有 `nonce: bytes` 和 `ciphertext: bytes`。生产 Adapter 复用宿主现有 `SecretKey`/`SecretEnvelope`，以配置 id 构造 recipient；Package 测试使用内存实现。两个模型共用该配置拥有的同一个 Secret，不增加 Secret 表或历史版本。

## 6. Provider Client

Package 内部 client 提供两个方法：

```python
class KnowledgeModelClient:
    async def embed(self, configuration, texts: list[str]) -> list[list[float]]: ...
    async def rerank(
        self,
        configuration,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[RerankScore]: ...
```

Embedding 请求：

```http
POST {base_url}/embeddings
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "model": "Qwen/Qwen3-VL-Embedding-8B",
  "input": ["text 1", "text 2"],
  "dimensions": 4096,
  "encoding_format": "float"
}
```

Reranker 请求：

```http
POST {base_url}/rerank
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "model": "Qwen/Qwen3-VL-Reranker-8B",
  "query": "user query",
  "documents": ["candidate 1", "candidate 2"],
  "top_n": 2,
  "return_documents": false
}
```

客户端行为：

- Embedding 按 `embedding_max_batch` 切分输入；
- Rerank 候选按 `reranker_max_batch` 分批调用，每批 `top_n = min(top_n, 批内候选数)`；rerank 对每个 query-候选对独立打分，跨批按 `relevance_score` 合并不改变语义；
- 两个接口使用配置的请求超时；
- 对网络错误、429 和 5xx 最多重试 1 次；
- 其他 4xx 不重试；
- Embedding 恢复 Provider data index 对应的输入顺序；
- Reranker 使用返回 index 映射原候选，不信任返回 document 文本。

不实现 Provider 插件注册表；实现类直接面向 SiliconFlow 的上述两个契约。

## 7. 返回校验

Embedding 每次调用校验：

1. response 中 data 数量等于 input 数量；
2. index 可以恢复为输入顺序；
3. 每个 embedding 是数值数组；
4. 每个 embedding 长度等于配置 dimension；
5. 所有值都是有限数；
6. 向量至少有一个非零值。

Reranker 每次调用校验：

1. 每批的 `results` 是数组且数量不超过该批 `top_n`；
2. 每个 index 是该批候选范围内的唯一整数，并按批内偏移映射回原候选；
3. 每个 `relevance_score` 是有限数；
4. 返回顺序与 score 降序一致；若 Provider 同分，Package 使用候选的 cosine score 和稳定 id 完成排序。

Embedding 校验失败返回 `KNOWLEDGE_EMBEDDING_FAILED`；Reranker 校验失败返回 `KNOWLEDGE_RERANK_FAILED`。

MVP 直接保存 Provider 返回向量，不额外归一化；cosine distance 由 pgvector 计算。

## 8. 连接测试

连接测试执行两个调用：

1. `/embeddings` 固定发送 `ActWeave knowledge embedding connection test`，确认返回一条指定 dimension 的合法向量。
2. `/rerank` 固定发送一个 query 和两条文本，确认返回合法且可映射的 index/score。

两次都成功才返回 `ok=true`。连接测试不创建 Knowledge Base、Document 或 Segment。

## 9. 错误映射

| 情形 | Error code |
| --- | --- |
| 配置不存在或 disabled | `KNOWLEDGE_MODEL_UNAVAILABLE` |
| 连接或请求超时 | `KNOWLEDGE_MODEL_UNAVAILABLE` |
| Embedding Provider 4xx/5xx | `KNOWLEDGE_EMBEDDING_FAILED` |
| Embedding 数量/维度/数值错误 | `KNOWLEDGE_EMBEDDING_FAILED` |
| Reranker Provider 4xx/5xx | `KNOWLEDGE_RERANK_FAILED` |
| Reranker index/score/顺序错误 | `KNOWLEDGE_RERANK_FAILED` |

## 10. 测试

- 首次空库确定性 seed、密文非明文、重复 setup 只读验证；
- setup/reset 在 DDL 或删除前完成 bootstrap Key 与 SecretKey 预检；
- Runtime 环境剥离 Knowledge bootstrap Key；
- 创建、更新、启停、删除；
- 配置被 Base 引用时不可停用且不可修改检索语义字段；
- `in_use` 派生值；
- 更新当前共享 API Key；
- 双接口连接测试成功、超时和错误响应；
- Embedding 单条、批量和跨 batch 顺序；
- Reranker index 映射、score 排序、top_n 和空候选不调用；
- Rerank 候选跨批分批、批内 index 偏移映射和跨批 score 合并；
- 429/5xx 一次重试和非重试 4xx；
- Embedding 数量、index、dimension、NaN/Infinity 和全零向量错误；
- Reranker 重复/越界 index、NaN/Infinity score 和缺失结果；
- API Key 加密失败时不留下配置。

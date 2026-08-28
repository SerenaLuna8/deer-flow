# RAG Knowledge Package M0+M1 Implementation Plan

## 目标

建立独立 `actweave-knowledge` Package，并把五张 Knowledge 表并入现有 Schema V1。本阶段不实现模型调用、上传或检索。

## 文件范围

```text
backend/packages/knowledge/pyproject.toml
backend/packages/knowledge/actweave_knowledge/__init__.py
backend/packages/knowledge/actweave_knowledge/module.py
backend/packages/knowledge/actweave_knowledge/contracts.py
backend/packages/knowledge/actweave_knowledge/persistence/
backend/pyproject.toml
backend/uv.lock
backend/app/knowledge/config.py
backend/app/knowledge/composition.py
backend/app/knowledge/bootstrap.py
backend/app/knowledge/secret_adapter.py
backend/scripts/setup_postgres.py
backend/scripts/reset_postgres.py
backend/scripts/run_runtime.py
backend/packages/harness/deerflow/persistence/full_schema.sql
backend/tests/knowledge/
config.example.yaml
README.md
Install.md
docker/docker-compose.yaml
docker/docker-compose-dev.yaml
```

## M0：Package 骨架

### Task 1：创建 workspace package

- distribution：`actweave-knowledge`；
- import：`actweave_knowledge`；
- Python 版本与 backend workspace 一致；
- 直接声明 SQLAlchemy、asyncpg、httpx、pgvector、官方 `minio` client 和 parser 依赖，不借用 harness 传递依赖。
- 把 `packages/knowledge` 加入 backend uv workspace，并把 `actweave-knowledge` 加入根应用依赖和 workspace source，更新 `uv.lock`。

测试：在 clean venv 构建 wheel、安装、import 并执行 `pip check`。

### Task 2：定义公开接口

根包导出：

```python
create_knowledge_module
KnowledgeModule
KnowledgeSettings
KnowledgeSecretPort
KnowledgeProtectedSecret
KnowledgeError
KnowledgeModelConfigurationCreate
KnowledgeModelConfigurationUpdate
KnowledgeModelConfigurationView
KnowledgeModelOption
KnowledgeModelConnectionResult
KnowledgeBaseCreate
KnowledgeBaseUpdate
KnowledgeBaseView
KnowledgeDocumentUpload
KnowledgeDocumentView
KnowledgeSegmentView
KnowledgeSearchRequest
KnowledgeSearchResult
KnowledgeCitation
KnowledgeHealth
```

`KnowledgeModule` 定义直观方法：模型配置 CRUD/测试和 active options、Base CRUD、Document 上传/列表/Segment 预览/重试/删除、search、purge_project、run_worker、health、aclose。

`create_knowledge_module(settings=..., session_factory=..., secret_port=...)` 复用宿主 SQLAlchemy session factory；`MinioObjectStore` 和单一内部 `KnowledgeModelClient` 由 Package 创建。

根包不导出 ORM、Repository、FileStore 或 Provider client。

### Task 3：依赖方向

- Package 不 import `app.*`、`deerflow.*`；
- harness 不 import `actweave_knowledge`；
- `backend/app/knowledge/composition.py` 负责组装；
- 增加静态 import 测试。

### Task 4：KnowledgeSettings 与根配置

默认关闭配置：

```yaml
knowledge:
  enabled: false
```

启用时配置：

```yaml
knowledge:
  enabled: true
  worker_concurrency: 2
  task_timeout_seconds: 900
  upload_max_bytes: 52428800
  max_knowledge_bases_per_project: 20
  max_documents_per_knowledge_base: 500
  max_segments_per_document: 5000
  minio:
    endpoint: $ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT
    bucket: actweave-knowledge
    access_key: $ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY
    secret_key: $ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY
    secure: false
```

`backend/app/knowledge/config.py` 从 `AppConfig.model_extra` 读取可选的 `knowledge` 映射，再使用 Package 导出的 `KnowledgeSettings` 校验。配置块缺失等同于 `enabled=false`；默认关闭配置不包含 MinIO 环境变量，启用配置才写入并要求 endpoint、bucket、access key 和 secret key。三个配额字段可省略，默认 20/500/5000，由 `KnowledgeSettings` 校验。

这些是根 `config.yaml` 的启动期配置，不进入 System Runtime Settings。Gateway 与 Worker 使用同一份配置并一起重启。实现阶段在 `config.example.yaml` 保留默认 `enabled=false`，把启用所需的 MinIO 环境变量写成注释示例，并同步 `README.md`、`Install.md`、`docker/docker-compose.yaml` 和 `docker/docker-compose-dev.yaml` 的显式环境变量传递；两个 Compose 都不创建 MinIO 服务，endpoint 与 bucket 作为部署前提准备，Runtime 只检查可访问性。缺少 `knowledge` 配置仍保持功能关闭，因此不为这一可选顶层配置提升现有 config version；启用说明由 `Install.md` 提供。

## M1：Schema V1

### Task 5：实现五张表和 ORM

```text
knowledge_model_configurations
knowledge_bases
knowledge_documents
knowledge_segments
knowledge_tasks
```

以 `docs/knowledge/RAG知识库MVP建表.sql` 为设计输入，实现 Package 内 ORM 和 Repository。所有表使用现有 `public` Schema，不创建独立 Knowledge Schema。

### Task 6：并入唯一 Schema V1

- 把 SQL 合并到 `full_schema.sql`；
- 同步 catalog digest；
- 同步 required relations/columns；
- 同步 setup/check、catalog 校验和 PostgreSQL fixtures；reset 继续只重建现有 `public` Schema；
- Runtime 不执行 DDL。

### Task 7：pgvector

- 安装流程由管理员准备 `public.vector`；
- setup 前检查 type 存在；
- app role 不负责创建 extension；
- Segment 的 `embedding` 使用无固定 typmod 的 vector，dimension 在应用写入前校验。

### Task 8：Repository 测试

- 模型配置 CRUD；
- Project Base 名称唯一；
- Document 状态和 version；
- Segment 级联删除；
- Task partial unique 和 claim 查询；
- Project 有 Base 时数据库拒绝直接删除，`purge_project` 清理后才允许删除；
- ORM、SQL 和 catalog 字段一致。

### Task 9：初始化默认检索模型配置

- `full_schema.sql` 保持纯 DDL，不插入模型记录、占位密文或明文 API Key；
- `backend/app/knowledge/bootstrap.py` 准备一条确定性 Knowledge Model Configuration：

  ```text
  display_name             = SiliconFlow Qwen3-VL Retrieval
  base_url                 = https://api.siliconflow.cn/v1
  embedding_model          = Qwen/Qwen3-VL-Embedding-8B
  embedding_dimension      = 4096
  embedding_max_batch      = 64
  reranker_model           = Qwen/Qwen3-VL-Reranker-8B
  reranker_max_batch       = 32
  request_timeout_seconds  = 30
  status                   = active
  ```

  两个模型共用当前加密 API Key；
- `setup_postgres.py` 在 Knowledge 表已 staged、Schema V1 marker 尚未发布时写入该配置；初始化不调用外部 Provider；
- 空库安装必须从安装期变量 `ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY` 取得 API Key，并在执行 DDL 前使用现有 `SecretKey`/`SecretEnvelope` 完成保护材料预检；
- `reset_postgres.py` 必须在删除现有 Schema 前完成同样预检；
- `run_runtime.py` 必须剥离安装期明文变量，Gateway/Worker 不继承它；
- 已完成的 Schema V1 再次运行 `setup-db` 仍保持只读，不把它改成数据升级命令。

测试：缺少主密钥或 Knowledge bootstrap API Key 时在 DDL/删除前失败；成功安装只产生一条固定配置；SQL 和日志不包含明文；配置写入失败时不发布 Schema V1 marker。

## 放行门

- Package wheel 安装/import 通过；
- 依赖方向测试通过；
- 临时空库 `setup-db` 与 `check-db` 通过；
- 默认 Embedding + Reranker 配置已由 bootstrap 写入并可被 Base 选择；
- 五张表、约束、索引和注释存在；
- 原 backend 聚焦测试不回归。

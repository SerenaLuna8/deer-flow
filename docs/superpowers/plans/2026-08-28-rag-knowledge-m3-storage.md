# RAG Knowledge Package M3（Base + Storage + Upload）Implementation Plan

## 目标

完成 Knowledge Base CRUD、MinIO 对象存储、Knowledge Document 上传和状态查询。摄取由 M4 实现。

## 文件范围

```text
backend/packages/knowledge/actweave_knowledge/storage/
backend/packages/knowledge/actweave_knowledge/bases/
backend/packages/knowledge/actweave_knowledge/documents/
backend/app/knowledge/composition.py
backend/app/knowledge/gateway.py
backend/tests/knowledge/test_bases.py
backend/tests/knowledge/test_storage.py
backend/tests/knowledge/test_upload.py
```

## Task 1：MinioObjectStore

本机当前已确认：

```text
S3 API endpoint: 127.0.0.1:9000
Console URL:     http://127.0.0.1:9001
```

程序必须连接 9000，不能把 9001 Console 当作对象存储 endpoint。`actweave-knowledge` bucket 由管理员预先创建，Runtime 只检查 endpoint 与 bucket 可访问性，不自动建 bucket。Gateway/Worker 在宿主机运行时可使用 `127.0.0.1:9000`；运行在 Compose 容器内时必须改用两个进程都能访问的 S3 API 地址。

Package 内部实现：

```python
MinioObjectStore.upload_from(key, source_path)
MinioObjectStore.download_to(key, target_path)
MinioObjectStore.delete(key)
```

- endpoint、bucket 和连接参数来自已校验的 `KnowledgeSettings`；
- 官方 MinIO client 的 endpoint 使用 `host:port`，不带 URL path；是否启用 TLS 由 `secure` 单独决定；
- Gateway 与 Worker 必须使用同一 endpoint 和 bucket；
- `upload_from` 使用官方 MinIO client 的 `fput_object`，完成后对象才可下载；
- 每个合法文件强制单 PUT：part_size 取文件大小与 5 MiB 的较大值、并行数为 1；
  `upload_max_bytes` 默认值和硬上限均为 50 MiB，每个 `MinioObjectStore` 只允许一个并发
  `fput_object`，约束 SDK 整 part 内存并避免不可见的 incomplete multipart；
- `download_to` 把对象写入调用方提供的任务临时 Path；
- `delete` 在对象不存在时仍返回成功；
- bucket 必须关闭 versioning/Object Lock；`check_bucket` 和所有删除操作先调用
  `GetBucketVersioning`，Enabled、Suspended 或权限不足都失败关闭，避免只写 delete marker；
- `upload_from` 获取单槽 PUT 许可后也必须重新调用 `GetBucketVersioning`，运行期漂移时不得
  调用 `fput_object`；
- `download_to` 和 `delete` 分别使用 `fget_object` 和 `remove_object`；所有同步 MinIO client 调用通过基于 `asyncio.to_thread` 的 cancellation-settling adapter 执行，不阻塞事件循环，且取消后等待已启动调用结束。
- Gateway 的请求临时目录创建、文件写入和清理使用异步文件操作或 `asyncio.to_thread`。

文件 key 使用服务端生成的 Document id 和原始扩展名：

```text
projects/{project_id}/knowledge/{base_id}/{document_id}.{ext}
```

## Task 2：Knowledge Base CRUD

实现：

```text
GET/POST  /api/projects/{project_id}/knowledge/bases
GET/PATCH /api/projects/{project_id}/knowledge/bases/{base_id}
```

- 创建时选择 active 模型配置；
- Project 内 Base 数量超过 `max_knowledge_bases_per_project` 时返回 `KNOWLEDGE_QUOTA_EXCEEDED`；
- 同一 Project 名称唯一；
- 更新只允许 name、description 和 active/disabled status，模型配置不可更换；
- View 返回基本字段、`document_count` 和可选 `delete_error`；
- 列表按 `updated_at DESC,id DESC`，详情严格限制在当前 Project。

Base 删除端点和执行逻辑在 M4 实现。

读取、列表、详情和 health 使用 `shared_assets.read`；创建、更新和上传使用 `shared_assets.edit`。

## Task 3：上传服务

上传请求包含：file、display name、chunk size、chunk overlap。

流程：

1. 校验 Base 处于 active、Document 数量未达 `max_documents_per_knowledge_base` 和扩展名；disabled/deleting Base 拒绝上传；
2. Gateway 把请求写入单次请求临时 Path，同时校验文件大小；
3. Package 创建 `uploading` Document 和 object key；
4. `MinioObjectStore.upload_from` 上传临时文件；
5. 同一事务更新 Document 为 `queued` 并创建 ingest Task；
6. 失败时删除已写对象和 `uploading` Document，并返回上传错误；
7. 无论成功、失败或取消，Gateway 都删除单次请求临时文件。

默认支持 50 MiB，六种扩展名由系统需求冻结。切分参数在上传时一次性固定，之后不可修改。

## Task 4：Document 查询 API

```text
POST /api/projects/{project_id}/knowledge/bases/{base_id}/documents
GET  /api/projects/{project_id}/knowledge/bases/{base_id}/documents
GET  /api/projects/{project_id}/knowledge/documents/{document_id}
GET  /api/projects/{project_id}/knowledge/documents/{document_id}/download
```

列表使用 `page/page_size`，按 `created_at DESC,id DESC` 排序。

Document view 包含名称、原始文件名、媒体类型、大小、状态、version、切分参数、Segment 数和错误信息。

下载端点使用 `shared_assets.read`：Gateway 提供单次请求临时 Path，调用 `download_document` 从 MinIO 读回后按原始文件名和媒体类型返回，响应结束后删除临时文件。仅 `queued|processing|ready|failed` 可下载，`uploading|deleting` 返回 `KNOWLEDGE_INVALID_REQUEST`，对象缺失返回 `KNOWLEDGE_STORAGE_UNAVAILABLE`。

## Task 5：宿主 composition

- 宿主把已校验的 `KnowledgeSettings` 传入 `create_knowledge_module`，由 Package 内部创建 `MinioObjectStore`；
- Gateway 和 Worker 共享同一个 KnowledgeModule 配置；
- shutdown 调用 `KnowledgeModule.aclose()`；
- `GET /api/projects/{project_id}/knowledge/health` 返回 database、MinIO 和 enabled 状态；MinIO 检查必须使用配置凭据验证目标 bucket 可访问，不能只调用进程级 `/minio/health/live`。

## 测试

- 临时 MinIO bucket 的 upload/download/delete，并从下载 Path 读回相同字节；
- Gateway 写入后 Worker 可从同一 bucket 下载；
- 六种允许扩展名和不支持扩展名；
- Base CRUD、名称唯一和模型配置不可更换；
- 0 byte、正常文件和超过上限；
- 上传成功后 Document+Task 状态；
- MinIO 写入失败和数据库写入失败后的对象清理；
- startup 后 bucket versioning 漂移时上传在 `fput_object` 前失败；
- 配置超过 50 MiB 时拒绝，同一 `MinioObjectStore` 的并发 PUT 峰值为 1；
- 删除不存在对象的幂等行为；
- 请求临时文件在成功、失败和取消后均清理；
- blocking-I/O 静态门确认事件循环内没有直接同步文件或 MinIO I/O；
- 列表分页和详情；
- 原文下载字节一致；uploading/deleting 拒绝下载；对象缺失返回存储错误；
- disabled Base 拒绝上传；Base 数量与 Document 数量配额生效；
- Package 与 HTTP 集成。
- health 使用配置凭据检查目标 bucket；无权限或 bucket 不存在时返回不可用。

## 放行门

- 上传后可以从 MinIO 下载并读回相同字节；
- 下载 API 返回与上传一致的字节和原始文件名；
- 可以创建 Base 并从同一 Base 查询 Document；
- 只有 MinIO 对象写入成功才创建摄取 Task；
- Gateway 与 Worker 可对同一 object key 完成上传和下载；
- 连接的是 S3 API endpoint，且预先创建的 bucket 可访问；
- M0–M2 回归通过。

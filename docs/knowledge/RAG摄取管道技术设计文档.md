# RAG 摄取管道技术设计（MVP）

> 现状：本文为 MVP 基线存档。M8 后切分改为递归分隔符策略并支持预处理规则
> （压缩空白、删 URL/邮箱）与父子分块，新增 Gateway 同步分块预览，
> 文件格式扩展 html/htm/pptx/epub。以《RAG知识库MVP执行计划》M8 小节为准。

## 1. 目标

把用户上传的文件稳定转换为可检索的 Knowledge Segment 和 embedding，并把处理状态显示在 Knowledge Document 上。

完整链路：

```text
upload -> extract -> clean -> split -> embed -> publish
```

## 2. 模块

```text
actweave_knowledge/
├── documents/service.py
├── ingestion/extractors.py
├── ingestion/cleaner.py
├── ingestion/splitter.py
├── ingestion/service.py
├── storage/minio.py
├── tasks/repository.py
└── tasks/worker.py
```

Package 内部接口：

```python
class MinioObjectStore:
    async def upload_from(self, key: str, source: Path) -> None: ...
    async def download_to(self, key: str, target: Path) -> None: ...
    async def delete(self, key: str) -> None: ...

class Extractor(Protocol):
    def extract(self, file_path: Path) -> ExtractedDocument: ...

class KnowledgeModelClient(Protocol):
    async def embed_many(self, configuration, texts: list[str]) -> list[list[float]]: ...
```

`MinioObjectStore` 直接包装官方同步 MinIO client：`upload_from`、`download_to` 和 `delete` 分别调用 `fput_object`、`fget_object` 和 `remove_object`，并统一通过可等待同步调用真正结束的 blocking adapter 执行；取消或超时不会让后台线程与任务重试重叠。上传获取对象存储实例唯一 PUT 槽后重新校验 bucket 仍未启用 versioning，再把 `part_size` 设为文件大小与 5 MiB 的较大值、并行数设为 1；所有合法文件均为单 PUT，`upload_max_bytes` 默认值和硬上限均为 50 MiB，以限制 MinIO SDK 的整 part 内存并避免 crash 遗留普通对象列表不可见的 incomplete multipart。Gateway/Worker 的临时目录创建、文件写入和清理也使用异步文件操作或同一类 blocking adapter。

## 3. 上传

支持：

| 扩展名 | 提取器 |
| --- | --- |
| `.pdf` | pypdf |
| `.docx` | python-docx |
| `.txt` | 文本解码器 |
| `.md` | 文本解码器 |
| `.csv` | Python csv |
| `.xlsx` | openpyxl |
| `.html` / `.htm` | HTML parser |
| `.pptx` | python-pptx |
| `.epub` | EPUB parser |

上传流程：

1. Gateway 把请求内容写入单次请求临时 `Path`，同时校验扩展名和大小上限。
2. Package 生成 Document id 和 MinIO object key `storage_key`。
3. 校验 Base 处于 active 且 Document 数量未达 `max_documents_per_knowledge_base` 后，插入 `status='uploading'` 的 Knowledge Document。
4. `MinioObjectStore.upload_from` 把临时文件上传到配置的 MinIO bucket。
5. 在一个事务中锁定 Document；只有它仍属于同一 Project、保持初始 version 且状态仍为 `uploading` 时，才更新为 `queued` 并插入 `ingest_document` Task。并发删除通过递增 version 和改为 `deleting` 获胜，上传方清理刚写入的对象且不得复活 Document。
6. 上传失败或被并发删除时先删除已写对象，再删除残留 Document。若对象删除失败，写入携带精确 `storage_key` 的独立 `delete_document_object` Task；Base 尚存时保留或重建 `deleting` tombstone，使三次失败后的 `delete_error` 可见并支持普通删除入口重试。
7. 无论成功、失败或取消，Gateway 都删除单次请求临时文件。

默认文件上限和配置硬上限均为 50 MiB。每次上传创建独立 Document，不做内容去重。

MinIO 是 MVP 唯一持久文件存储实现。请求和 Worker 临时 `Path` 只服务于一次操作；bucket 由根配置提供，Document 只保存 object key，不保存 provider 或 bucket 字段。

## 4. 文本提取

`ExtractedDocument`：

```python
@dataclass
class ExtractedBlock:
    text: str
    source_position: dict[str, object]

@dataclass
class ExtractedDocument:
    blocks: list[ExtractedBlock]
```

来源位置：

- PDF：`{"page": 1}`；
- XLSX：`{"sheet": "Sheet1", "row_start": 1, "row_end": 5}`；
- CSV：`{"row_start": 1, "row_end": 5}`；
- PPTX：slide；EPUB：chapter；HTML/DOCX/TXT/Markdown：段落或空位置。

TXT/Markdown 解码顺序：

1. 有 UTF-16 BOM 时按 BOM 解码；
2. 尝试 UTF-8；
3. 尝试 GB18030；
4. 都失败则处理失败。

空文件或只包含空白字符的文件处理失败。

Worker 为一次处理创建临时目录，把对象下载到临时 `Path` 后调用 Extractor，并在处理结束后删除临时目录。MinIO I/O 和上述同步 parser 均通过 cancellation-settling blocking adapter 执行，不直接阻塞 Worker 事件循环；Worker 只有在已启动的同步调用真正结束后才释放 claim 或安排重试。

## 5. 清洗与切分

基础清洗只做：

- 统一换行符；
- 去除每行首尾空白；
- 连续三个及以上空行压缩为两个；
- 删除首尾空白。

不做语义改写、URL 删除、邮箱删除或语言识别。

切分参数保存在 Knowledge Document：

```text
chunk_size       默认 1000，范围 200..4000
chunk_overlap    默认 100，范围 0..500 且小于 chunk_size
```

Splitter 优先在段落和换行边界切分；没有合适边界时按字符数切分。输出 position 从 1 连续递增。`max_segments_per_document` 是单文档向量条目预算，默认值和可配置硬上限均为 5000：general 模式按 Segment 数量计，parent-child 模式按携带向量的 Knowledge Segment Child 数量计。计数超限时必须在任何 Embedding 调用前失败，Document 进入 `failed` 并说明超限。

## 6. Embedding

1. 读取 Base 绑定的 Knowledge Model Configuration。
2. 使用模型接入层的 `KnowledgeModelClient.embed_many()`，按配置 `embedding_max_batch` 分批调用 Embedding。
3. 返回数量必须等于输入文本数量。
4. 每个向量的维度必须等于配置 `embedding_dimension`。
5. 向量元素必须是有限数值。
6. 向量不能全部为零。

任一批失败则本次 Document 不发布新 Segment。

Reranker 只参与查询时的候选重排，不参与文档摄取。

## 7. Task 执行

Task 使用单表字段：

```text
id
project_id
kind
resource_id
target_version
storage_key              # 仅 delete_document_object 使用
status
attempt_count
max_attempts
available_at
claim_token
lease_until
error_message
created_at / updated_at / finished_at
```

claim 流程：

1. 使用 `FOR UPDATE SKIP LOCKED` 选择一条到期的 `queued|retry_wait` Task。
2. 设置 `running`、随机 `claim_token`、`lease_until`，并递增 `attempt_count`。
3. 长任务周期性延长 `lease_until`。
4. 只有仍持有该 token 的 Worker 可以提交成功或失败。
5. lease 过期且仍有剩余次数的 `running` Task 回到 `retry_wait`；已用完 3 次则进入 `failed`。

自动执行最多 3 次。前两次失败进入 `retry_wait`；第三次失败进入 `failed`。只有 `ingest_document` 最终失败时才把匹配 version 的 Document 置为 `failed`；删除 Task 失败时，仍存在的 Base/Document/tombstone 保持 `deleting`。没有 tombstone 的 object-only Task 仍保留为 Project purge 的失败关闭证据。

## 8. 发布事务

Worker 完成所有 embedding 后开启短事务：

1. 锁定 Task 和 Knowledge Document；
2. 检查 Task 的 `claim_token`；
3. 检查 Document 的 `version == target_version`；
4. 检查 Document 仍是 `processing`；
5. 删除该 Document 的旧 Segment；
6. 插入新 Segment，每行直接保存 embedding；
7. 更新 Document 为 `ready` 并写入 `segment_count`；
8. 更新 Task 为 `succeeded`。

以上步骤一次提交。Document 不存在、已 `deleting` 或 version 不匹配时，不发布结果，并把仍由当前 claim token 持有的 Task 更新为 `succeeded` no-op，避免重复执行。

## 9. 重试

用户重试失败 Document 时：

1. 锁定 Document；
2. `version += 1`；
3. 状态改为 `queued`，清空错误；
4. 创建新 `ingest_document` Task，`target_version` 为新 version。

同一 Document version 只允许一条未完成的摄取 Task。重试只接受 failed Document，要求所属 Base 处于 active，并沿用 Document 上保存的切分参数。

## 10. 删除

### Document

1. 把 Document 标记为 `deleting`、递增 version 并清空 `error_message`。
2. 创建 `delete_document` Task。
3. Worker 删除 MinIO 对象。
4. Worker 删除 Document 行；Segment 由外键级联删除；删除 Task 更新为成功。
5. 若并发上传的晚到 put 在原删除 Task 仍 running 时完成且即时清理失败，创建可并存的
   `delete_document_object` Task；handler 先验证 exact key 属于可信 Project/Document，
   再删除对象和可选 tombstone。最终失败纳入 Document `delete_error`。

### Knowledge Base

1. 把 Base 标记为 `deleting`。
2. 创建 `delete_knowledge_base` Task。
3. Worker 删除 Base 下所有 Document 对象和 Document 行。
4. 删除 Base 行，并把删除 Task 更新为成功。

删除动作可重复调用。MinIO 对象已经不存在时视为删除成功。没有 open delete Task 时，Document/Base View 才从最近失败 Task 派生 `delete_error`；再次删除会创建新 Task，并在该 Task 未完成期间返回 `delete_error=null`。

### Project

Project retention 通过独立 purger（不是 `knowledge_tasks` kind）清理。近期 `uploading`
使本轮直接返回未完成且不得触碰对象/关系行/prefix；超过一天的遗留上传本轮只转为
`deleting` 并写 exact-key Task，下一轮才清理。判断使用 PostgreSQL 时钟，一天显著长于
MinIO 正常单请求传输/重试且显著短于 Project 固定 30 天 retention。最终仍对可信
Project prefix 做兜底 sweep。MinIO-backed 删除在删除对象及其关系行前必须确认 bucket
versioning 为未配置/`Off`；`Enabled`、`Suspended`、Object Lock 或缺少
`GetBucketVersioning` 权限均失败关闭。凭据还必须允许列举 Project prefix 和删除其中对象。
功能关闭后独立 purger 仍保留；若 MinIO 配置已移除，只要还有 Document 行或状态不是
`succeeded` 的 object-only Task，本次 Project purge 就返回未完成。只有无上述存储证据的
纯元数据状态可以不访问 MinIO 直接清理。

## 11. 测试

- 九种格式各一个正常 fixture；
- UTF-8、UTF-16、GB18030 文本；
- 空文件和损坏文件；
- chunk size/overlap 边界；
- Segment 数量超限进入 failed；
- disabled Base 拒绝上传和重试；
- embedding 数量、维度、非法数值和全零向量；
- 自动重试和 lease 过期恢复；
- retry 后旧 Worker 迟到发布；
- delete 与正在执行的摄取并发；
- 普通删除仍 running 时可并存 exact-key object-only Task，失败通过 tombstone 展示并可重试；
- 近期/超过一天的 `uploading` Project purge 分支、无 MinIO 失败关闭和 versioning 拒绝；
- Document/Base 删除后的 MinIO 对象与数据库清理；
- 上传请求临时文件和摄取临时文件在成功、失败、取消后均清理；
- Worker 重启后继续处理 queued Task。

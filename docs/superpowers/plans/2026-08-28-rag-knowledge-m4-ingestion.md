# RAG Knowledge Package M4（Ingestion + Tasks）Implementation Plan

## 目标

把 queued Knowledge Document 处理为 ready Segment，并完成失败重试、Document/Base 删除和 Project purge。

## 文件范围

```text
backend/packages/knowledge/actweave_knowledge/ingestion/
backend/packages/knowledge/actweave_knowledge/tasks/
backend/packages/knowledge/actweave_knowledge/documents/
backend/app/knowledge/worker.py
backend/app/knowledge/gateway.py
backend/app/worker/app.py
backend/app/worker/retention.py
backend/tests/knowledge/test_ingestion.py
backend/tests/knowledge/test_tasks.py
backend/tests/knowledge/test_worker.py
```

## Task 1：Extractor

实现：

- PDF：pypdf；
- DOCX：python-docx；
- TXT/Markdown：UTF-16 BOM、UTF-8、GB18030；
- CSV：stdlib csv；
- XLSX：openpyxl read-only。

输出统一为 `ExtractedBlock(text, source_position)`。空文本和解析异常返回 `KNOWLEDGE_PARSE_FAILED`。

## Task 2：Cleaner 与 Splitter

- 统一换行；
- trim 行和全文；
- 压缩多余空行；
- 优先按段落/换行边界切分；
- 没有边界时按字符数切分；
- position 从 1 连续递增；
- 保留所在 block 的来源位置。

测试默认 1000/100、最小/最大 chunk、0 overlap 和跨边界文本。

## Task 3：Task Repository

使用 `public` Schema 中的 `knowledge_tasks` 实现单表任务：

```text
queued/retry_wait -> running -> succeeded|retry_wait|failed
```

- `FOR UPDATE SKIP LOCKED` claim；
- claim 生成 token 和 lease；
- heartbeat 延长 lease；
- 过期 running 在仍有剩余次数时回到 retry_wait；已用完三次则进入 failed；
- 自动执行最多三次；
- 同一 Document version 最多一条 open ingest Task。
- `delete_document_object` 携带精确 storage key，并有独立 open 唯一约束，因此可与同一
  Document 仍 running 的普通删除 Task 并存；handler 校验 key 的 Project/Document 结构。

## Task 4：Ingestion handler

顺序：

1. claim 后把匹配 version 的 Document 置为 `processing`；
2. 通过 cancellation-settling blocking adapter 创建任务临时目录，把 MinIO 对象下载到临时 Path；
3. 通过同一 adapter 执行同步提取，再清洗和切分；切分数量超过 `max_segments_per_document` 时按 `KNOWLEDGE_QUOTA_EXCEEDED` 失败；
4. 调用 Base 的 Knowledge Model Configuration 中 Embedding 部分生成向量；Reranker 仅在 M5 查询阶段使用；
5. 校验数量、dimension、有限数值和非零向量；
6. 执行最终发布事务；
7. 无论成功、失败或取消都通过该 adapter 清理任务临时目录；取消等待已启动调用真正结束。

发布事务同时：

- 检查 Task claim token；
- 检查 Document version 和 processing 状态；
- 删除旧 Segment；
- 插入新 Segment 与 embedding；
- Document 置为 ready；
- Task 置为 succeeded。

Document 不存在、deleting 或 version 不匹配时不发布迟到结果，并把当前 claim 的 Task 结算为 succeeded no-op。

## Task 5：用户重试

`POST /api/projects/{project_id}/knowledge/documents/{document_id}/retry`

- 使用 `shared_assets.edit`；
- 只允许 failed Document，且所属 Base 处于 active；沿用原切分参数；
- version 加一；
- 清空错误并置 queued；
- 创建新 ingest Task；
- 以上在同一事务完成。

## Task 6：Segment 预览

实现：

```text
GET /api/projects/{project_id}/knowledge/documents/{document_id}/segments
```

只返回当前 Project、当前 Document version 已发布的 Segment，按 position、id 排序并使用 `page/page_size`。响应为 `KnowledgeSegmentView`，不返回 embedding。

该读取端点使用 `shared_assets.read`。

## Task 7：删除

Document 删除：

- 标记 deleting、递增 version 并清空 error_message；
- 创建 delete_document Task；
- Worker 删除 MinIO 对象后删除 Document 行，并把删除 Task 更新为成功。
- 删除最终失败时 Document 保持 deleting；再次调用删除且没有 open delete Task 时创建新 Task，存在 open Task 时 View 的 `delete_error=null`。
- 上传删除竞态的即时对象清理失败时，创建 exact-key `delete_document_object` Task；
  Base 尚存则恢复 deleting tombstone，最终失败纳入同一 delete_error 派生并支持普通重删。

Base 删除：

- 标记 deleting；
- 创建 delete_knowledge_base Task；
- Worker 删除所有 Document 的 MinIO 对象和 Document 行后删除 Base 行，并把删除 Task 更新为成功。
- 删除最终失败时 Base 保持 deleting；再次调用删除且没有 open delete Task 时创建新 Task，存在 open Task 时 View 的 `delete_error=null`。

Gateway 提供：

```text
DELETE /api/projects/{project_id}/knowledge/bases/{base_id}
DELETE /api/projects/{project_id}/knowledge/documents/{document_id}
```

两个删除端点使用 `shared_assets.edit`。

`purge_project(project_id)` 复用 Base 删除对象/行的处理函数并保持幂等；没有 Base 时返回 complete。
Project 进入 `pending_deletion` 后，Task claim 在同一事务通过宿主 Project share-lock callback
退回 `retry_wait`，60 秒后重试且不消耗 attempt；Project restore 后自动恢复执行。最终 purge
先恢复过期 lease 并锁定该 Project 全部 open Knowledge Task：仍有 `running` 时本轮返回
incomplete，`queued|retry_wait` 在对象或关系行删除前移除。

Worker composition 把同一个 `KnowledgeModule` 传给 `RetentionPurgeJobHandler`。Project retention job 在进入最终 `physically_purge` 数据库事务前调用 `purge_project(project_id)`；Knowledge 清理完成才继续，清理失败则使用现有 retention job 重试，不能先完成 Project purge。

## Task 8：Worker 接入

- `backend/app/knowledge/worker.py` 调用 `module.run_worker(stop_event)`；
- Knowledge TaskWorker 在现有 `app.worker` 进程内运行，与主 Worker 使用同一个 stop event；
- 主 Worker 或 Knowledge TaskWorker 任一循环异常时停止另一循环并退出进程，由现有进程重启策略恢复；
- 不新增独立 Worker 服务；
- stop 后不再 claim 新 Task，当前 handler 在超时时间内结束；
- Worker 重启后恢复过期 lease。
- pending-deletion Project 的 claim 不启动 handler、不消耗重试预算；restore 后继续；最终
  Project purge 不越过仍在运行的 Knowledge Task。

## 测试

- 六种格式 fixture；
- 解码、空文件和损坏文件；
- 分段边界；
- Task claim/heartbeat/expiry/三次失败；
- publish 单事务回滚；
- retry 与迟到旧 Worker；
- Segment 数量超限失败与错误信息；disabled Base 拒绝重试；
- delete 与正在处理的 Document；
- Base 多 Document 删除；
- 当前 version Segment 预览及分页；
- Project purge；
- retention purge 只有在 Knowledge 对象和 Base 清理后才继续；
- Project pending-deletion claim 暂停且 restore 后自动继续；running 与 queued Task
  并存时 purge 删除 queued 但不越过 running；
- MinIO 下载失败进入现有 PostgreSQL Task 重试；
- 成功、失败和取消后任务临时文件都被清理；
- blocking-I/O 静态门确认事件循环内没有直接同步文件、MinIO 或 parser I/O；
- 主 Worker 或 Knowledge TaskWorker 任一循环异常时停止另一循环并完成统一 shutdown；Worker restart 后恢复过期 lease 和 queued Task。

## 放行门

- 上传的六种文件都能进入 ready；
- 失败和重试在页面 DTO 中可解释；
- 迟到任务不能复活或覆盖 Document；
- 删除后 MinIO 对象与关系数据均消失；
- M0–M3 回归通过。

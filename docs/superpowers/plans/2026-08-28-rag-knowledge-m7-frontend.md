# RAG Knowledge Package M7（Frontend）Implementation Plan

## 目标

完成用户可以直接操作的 Knowledge 页面、管理员模型页面和聊天 Citation 展示。

## 文件范围

```text
frontend/src/app/projects/[project_slug]/knowledge/page.tsx
frontend/src/app/admin/settings/knowledge/page.tsx
frontend/src/components/projects/knowledge/
frontend/src/components/admin/settings/
frontend/src/components/admin/operations/admin-operations-shell.tsx
frontend/src/components/projects/project-nav.tsx
frontend/src/core/knowledge/
frontend/src/core/private-work/scope-registry.ts
frontend/src/core/threads/message-projection.ts
frontend/src/core/i18n/locales/
frontend/tests/unit/
frontend/tests/e2e/
```

## Task 1：API 类型

实现：

```text
KnowledgeModelConfigurationView
KnowledgeBaseView
KnowledgeDocumentView
KnowledgeCitation
KnowledgeSearchResult
KnowledgeHealth
Page<T>
```

列表请求统一使用 `page` 和 `page_size`。Project 页面通过 `useCurrentProject()` 取得 Project UUID 调用 API。Knowledge query key 固定包含 account UUID、Project UUID 和资源 id，并把 Knowledge query root 纳入现有 `transitionPrivateWorkScope` 清理。

## Task 2：Project 导航与 Base 页面

- feature disabled 时隐藏 Knowledge 导航；
- 页面和读取入口要求 `shared_assets.read`；创建、编辑、上传、重试和删除控件要求 `shared_assets.edit`；
- Base 列表显示名称、状态、Document 数和更新时间；
- 创建表单调用 Project `model-options`，选择模型配置并填写名称/描述；
- 编辑名称、描述和状态；
- 删除前确认，提交后轮询 deleting 状态直到消失。
- deleting 且 `delete_error` 非空时停止轮询，显示错误和“再次删除”；再次提交后恢复轮询。

## Task 3：Document 页面

- 单文件上传；批量选择时逐文件请求并显示各自结果；
- 表单允许 display name、chunk size 和 overlap，并说明切分参数上传后不可修改、重试沿用原参数；
- 列表显示文件名、大小、状态、Segment 数和错误；
- `uploading|queued|processing|deleting` 时每 2 秒刷新；
- failed 显示重试和删除；
- deleting 且 `delete_error` 非空时停止轮询，显示错误和“再次删除”；再次提交后 `delete_error` 为空并恢复轮询；
- ready 支持查看 Segment 预览和删除。
- 非 `uploading|deleting` 状态提供原文下载入口。

Segment 预览调用：

```text
GET /api/projects/{project_id}/knowledge/documents/{document_id}/segments
```

## Task 4：检索测试

- query 输入；
- 可选 Base 多选；
- top-k 1..20；
- 可选 `score_threshold`（0..1，默认 0.2）；
- 展示 snippet、score、Base、Document 和来源位置；
- score 文案表示 Reranker 相关度，不显示为“向量相似度”；
- 空结果显示“未找到相关内容”；
- 模型或搜索错误显示后端 message。

## Task 5：admin 模型设置

- 使用 `/admin/settings/knowledge` 和现有 Admin settings shell；
- 模型配置列表；
- 新建与编辑表单；
- 当前 API Key 输入；
- 连接测试；
- active/disabled 切换；
- 删除未使用配置。

`in_use=true` 时禁用停用和删除操作，并展示原因。

表单字段与 M2 DTO 一致，不增加前端 URL 规范化库。

表单明确包含 Base URL、Embedding model、Embedding dimension、Embedding max batch、Reranker model、Reranker max batch、timeout 和当前 API Key；连接测试只有 `/embeddings` 与 `/rerank` 都成功才显示通过。

## Task 6：Citation 渲染

- 从最终 Agent 消息的 `additional_kwargs.knowledge_citations` 读取 Citation；
- 在 Agent 消息下显示来源卡片；
- 卡片显示 Base、Document、snippet、score 和页码/行号；
- 同一 Segment id 在一条消息内只展示一次；
- 刷新和历史消息使用同一渲染组件。

## Task 7：浏览器验收

Playwright 场景：

1. 管理员创建模型配置并同时测试 Embedding 与 Reranker；
2. 用户创建 Base；
3. 上传文档并观察 queued/processing/ready；
4. 检索测试返回结果；
5. Agent 调用工具并显示 Citation；
6. 刷新页面后 Citation 仍存在；
7. 损坏文件失败后用户删除；
8. mock Embedding Provider 连续失败至三次耗尽，恢复后用户重试成功；
9. 删除 Document；
10. 删除 Base；
11. 被 Base 使用的模型配置不能停用；
12. mock Reranker 改变 cosine 候选顺序，页面与 Citation 显示重排后的顺序和 score；
13. Reranker 失败时检索显示错误，不展示 cosine-only 结果；
14. feature disabled 时导航与工具都不存在；
15. 下载已上传文档并核对文件名与内容；
16. 无关 query 的检索测试显示“未找到相关内容”，Agent 回答不产生引用。

Mock E2E 和真实 backend E2E 分开报告。真实 backend 测试使用临时 PostgreSQL、临时 MinIO bucket 和同时实现 `/embeddings`、`/rerank` 的 mock Provider；删除 Document/Base 后同时确认对应对象已不存在。

## 放行门

- `pnpm check`、unit tests 和 Knowledge Playwright 通过；
- 页面覆盖创建、处理、检索、重试和删除；
- Citation 刷新可恢复；
- M0–M6 backend 门通过。

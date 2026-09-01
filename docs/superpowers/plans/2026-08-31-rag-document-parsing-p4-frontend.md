# P4 前端接入与最终验收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有知识库界面中交付动态格式、表头选择、真实 Token 单位、Markdown/图片和可验证的处理结果。

**Architecture:** 前端只消费服务器能力与安全 DTO，不自己选择解析器或持有存储权限。预览图使用本次响应产生的 Blob，已发布图通过受权 Gateway fetch；两条路径共用安全 Markdown 渲染。最终用 mock 浏览器、真实后端 replay、生产镜像离线门及检索对比验证整个改造。

**Tech Stack:** React/Next.js、TypeScript/Zod、现有 SafeMessageResponse/MarkdownContent、Rstest、Playwright、pytest、Docker。

**Spec:** [设计规格](../specs/2026-08-31-rag-document-parsing-design.md)；[总计划与 DTO](2026-08-31-rag-document-parsing.md)；前置 [P3](2026-08-31-rag-document-parsing-p3-ingestion.md)。

## Global Constraints

- 完整继承总计划约束，尤其服务器 Project authority、无存储 URL、Markdown 无 raw HTML、外部图片不自动加载。
- 预览前 10 父段、最多 20 张缩略图、每张 ≤128 KiB、合计 ≤2 MiB。Blob/响应字节不能写 localStorage 或持久查询缓存。
- 用户可选的是分段/表头参数，不是任意 parser 路径、模型凭据、attachment storage key。
- 新参数单位显示“知识库 Token”；历史 character profile 仍显示字符。字数统计仍是字符数。
- API revision/文件/参数/项目作用域变化都使预览过期；旧响应不能复活。
- 当前计划不含 OCR 或图片向量，不显示“图片已识别”或“图片可语义检索”等误导文案。
- 先完成 P3 的真实 DTO；不能只改 mock，使浏览器测试与后端脱节。
- 本计划不授权启动生产服务、reset数据库或推送；授权提交时只列该任务文件。

## P4-T1：严格 DTO、能力查询和附件请求

**Files**

- Modify: `frontend/src/core/knowledge/types.ts`、`api.ts`、`hooks.ts`、`query-keys.ts`。
- Create: `frontend/tests/unit/core/knowledge/parsing-contracts.test.ts`。
- Modify: `frontend/tests/e2e/project-knowledge.spec.ts` 的mock DTO。

**Interfaces**

- Consumes: 总计划 §3.4 的file-capabilities/preview/segment DTO与两条附件路由。
- Produces: `knowledgeFileCapabilitiesSchema`、`listKnowledgeFileCapabilities(projectId, signal?)`、`useKnowledgeFileCapabilities(scope)`；`knowledgeAttachmentURL(input: KnowledgeAttachmentRead): string`、`fetchKnowledgeAttachment(input, signal): Promise<Blob>`。

`KnowledgeAttachmentRead` 精确定义：projectId/documentId/segmentId/attachmentId为string，expectedDocumentVersion为number，expectedContentDigest为string，purpose为`management|citation`，baseId在citation中必填。使用判别联合，不用一组全optional参数。

- [ ] **1. 写schema和URL红测。**

```typescript
import { describe, expect, it } from '@rstest/core';
import { knowledgeFileCapabilitiesSchema } from '@/core/knowledge/types';
import { knowledgeAttachmentURL } from '@/core/knowledge/api';

describe('file capability contract', () => {
  it('rejects storage locators in an otherwise valid capability response', () => {
    const response = {
      effective_etl: 'dify', capability_revision: 'a'.repeat(64),
      formats: [{ extension: '.pdf', parser_id: 'dify.pdf', available: true, reason_code: null, embedded_images: true }],
      chunk_limits: { unit: 'token', tokenizer_profile_id: 'knowledge-cl100k-v1', parent_min: 200,
        parent_max: 4000, parent_max_chars: 4000, overlap_max: 500, child_min: 100, child_max: 2000 },
    };
    expect(knowledgeFileCapabilitiesSchema.safeParse(response).success).toBe(true);
    expect(knowledgeFileCapabilitiesSchema.safeParse({ ...response, storage_key: 'private/key' }).success).toBe(false);
  });
  it('separates management and citation paths', () => {
    const common = { projectId: 'p', documentId: 'd', segmentId: 's', attachmentId: 'a',
      expectedDocumentVersion: 3, expectedContentDigest: 'b'.repeat(64) };
    expect(knowledgeAttachmentURL({ ...common, purpose: 'management' })).not.toContain('/bases/');
    expect(knowledgeAttachmentURL({ ...common, purpose: 'citation', baseId: 'k' })).toContain('/bases/k/');
  });
});
```

- [ ] **2. 跑red。** 工作目录frontend：

```bash
pnpm exec rstest tests/unit/core/knowledge/parsing-contracts.test.ts
```

- [ ] **3. 按真实响应写strict Zod。** 每层`.strict()`，etls为明确枚举，SourceSpan与总计划字段相同，offset为非负整数、end≥start。安全 MIME 仅PNG/JPEG/WebP；base64字符串长度先限制再解码验证实际字节预算。缺少能力响应不能退回“全部格式可上传”。
- [ ] **4. 新增项目作用域查询和附件fetch。** 能力key绑定现有 ProjectClientScope 的 accountId/projectId；当前共享scope没有actorId/generation字段。请求generation由本功能生命周期token或现有作用域机制单独约束，不为此扩大全局scope类型；A→B→A后旧响应不得恢复能力状态，不跨账号/项目共享。附件URL核心：

```typescript
export function knowledgeAttachmentURL(input: KnowledgeAttachmentRead): string {
  const encode = encodeURIComponent;
  const base = input.purpose === 'citation' ? `/bases/${encode(input.baseId)}` : '';
  const path = `/api/projects/${encode(input.projectId)}/knowledge${base}/documents/${encode(input.documentId)}`
    + `/segments/${encode(input.segmentId)}/attachments/${encode(input.attachmentId)}`;
  const query = new URLSearchParams({ expected_document_version: String(input.expectedDocumentVersion),
    expected_content_digest: input.expectedContentDigest });
  return `${path}?${query.toString()}`;
}
```

fetch沿用现有API认证/错误映射；2xx才转Blob，检查response MIME，不把错误JSON当图片。401/403/404清除展示，409提示刷新内容。不要将Blob放入共享Query cache。
- [ ] **5. 同步所有受影响mock响应并跑green。**

```bash
pnpm exec rstest tests/unit/core/knowledge/parsing-contracts.test.ts
pnpm check
```

- [ ] **6. 交付A01/A17/A22/A25的前端部分。** 保存实际后端DTO样例供P4-T5真实浏览器复用。

## P4-T2：上传向导的动态格式、表头和预览身份

**Files**

- Modify: `frontend/src/components/projects/knowledge/knowledge-create-wizard.tsx`、`knowledge-documents-view.tsx`。
- Modify: `frontend/src/core/knowledge/preview-identity.ts`、`chunk-settings.ts`、`types.ts`、`api.ts`。
- Create: `frontend/src/components/projects/knowledge/knowledge-header-settings.tsx`。
- Modify: `frontend/tests/unit/core/knowledge/preview-identity.test.ts`、`chunk-settings.test.ts`、`frontend/tests/e2e/project-knowledge.spec.ts`。
- Modify: `frontend/src/core/i18n/locales/zh-CN.ts`、`en-US.ts`、`types.ts`。

**Interfaces**

- Consumes: capabilities DTO和preview effective_profile/fingerprint。
- Produces: 扩展 `KnowledgePreviewParams`：既有字段+`unit, tokenizer_profile_id, capability_revision, header_rules`。`previewParamsEqual`比较所有实际输入；现有sequence/scope reducer继续拥有响应顺序。

- [ ] **1. 写身份红测。** 在既有preview-identity测试的基准参数上增加新字段，证明只变能力revision、header_rules、单位之一就不相等：

```typescript
import { expect, it } from '@rstest/core';
import { previewParamsEqual, type KnowledgePreviewParams } from '@/core/knowledge/preview-identity';

it('invalidates a preview when the effective parser capability changes', () => {
  const params: KnowledgePreviewParams = {
    chunk_size: 1000, chunk_overlap: 100, chunk_separator: '\\n\\n', remove_extra_spaces: false,
    remove_urls_emails: false, chunking_mode: 'general', unit: 'token',
    tokenizer_profile_id: 'knowledge-cl100k-v1', capability_revision: 'a', header_rules: [],
  };
  expect(previewParamsEqual(params, { ...params, capability_revision: 'b' })).toBe(false);
  expect(previewParamsEqual(params, { ...params, header_rules: [{ sheet: null, mode: 'none', row: null }] })).toBe(false);
});
```

- [ ] **2. 跑red，扩展参数规范化。** header_rules按sheet稳定排序后序列化，避免对象键序影响；显式行号要求正整数且mode=explicit，其余row必须null。API只发送用户可配字段，不把effective_profile中的服务器parser版本原样当权限提交。
- [ ] **3. 接入能力列表。** `<input accept>`仅由available格式生成；已选文件因ETL变化变为不可用时保留文件和可见原因，禁止提交该文件，不静默删除。缺能力响应显示重试；无需重新创建已有知识库。
- [ ] **4. 表头控件只在CSV/Excel出现。** auto/none/显式行号；XLSX每sheet展示服务器预览发现的名称及候选行，参数回写header_rules。消费总计划的 `table_sources:[{sheet, header_mode, header_row, header_cells}]`；auto时明确显示这是候选，explicit显示用户选择行，none显示未选择表头。原始表头单元格完整展示，不能在浏览器独立解析Excel猜表头。
- [ ] **5. 用服务器fingerprint完成提交。** 保存最近成功预览的fingerprint与File对象身份；多文件仍逐文件提交且成功项不重传。换文件/参数后只有刷新预览才能提交新fingerprint；有些文件未预览可以按headless语义上传并显示实际profile，不能把另一文件fingerprint借给它。409保留参数/文件，标明预览过期。
- [ ] **6. 跑green与mock浏览器场景。** 包含A→B→A、同名File替换、能力revision变化、参数变化、请求取消后晚到响应；旧character文档重解析弹窗提示单位变化。

```bash
pnpm exec rstest tests/unit/core/knowledge/preview-identity.test.ts tests/unit/core/knowledge/chunk-settings.test.ts
pnpm exec playwright test tests/e2e/project-knowledge.spec.ts --project=chromium
```

- [ ] **7. 交付A03/A13/A29/A30。** 新schema字段必须同时在P3真实响应和mock出现；授权提交时只列本任务Files。

## P4-T3：统一安全 Markdown 与图片 Blob 生命周期

**Files**

- Create: `frontend/src/components/projects/knowledge/knowledge-markdown.tsx`、`knowledge-image.tsx`。
- Create: `frontend/src/core/knowledge/attachment-images.ts`、`markdown-images.ts`。
- Modify: `knowledge-create-wizard.tsx`、`knowledge-segments-browser.tsx`、`knowledge-search-panel.tsx`。
- Test: `frontend/tests/unit/core/knowledge/attachment-images.test.ts`、`markdown-images.test.ts`。
- Extend: `frontend/tests/e2e/project-knowledge.spec.ts`。

**Interfaces**

- Consumes: 已验证attachment DTO、scopeKey、既有MarkdownContent/SafeMessageResponse。
- Produces: `KnowledgeMarkdown({content, imageSources, scopeKey})`；`useKnowledgeImage(input: KnowledgeAttachmentRead | null, scopeKey: string)`；`createPreviewImageURLs(attachments): {urls:Map<string,string>, dispose():void}`。

`KnowledgeImageSource` 为 `{kind:'preview',url:string} | {kind:'protected',request:KnowledgeAttachmentRead}`，imageSources为按逻辑ref索引的ReadonlyMap。预览URL只能来自本任务的Blob工厂；protected分支调用P4-T1的typed fetch，不接受正文任意URL。hook返回 `{url:string|null,status:'idle'|'loading'|'ready'|'error'}`；组件在preview分支给hook传null并显示已有Blob，不进行额外请求。

- [ ] **1. 写Blob释放和外部图片红测。** 可先为纯函数写完整测试，不依赖新的UI测试库：

```typescript
import { afterEach, expect, it, rs } from '@rstest/core';
import { createPreviewImageURLs } from '@/core/knowledge/attachment-images';

afterEach(() => rs.restoreAllMocks());
it('releases every preview blob URL exactly once', () => {
  rs.spyOn(URL, 'createObjectURL').mockReturnValue('blob:preview-one');
  const revoke = rs.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
  const images = createPreviewImageURLs([{ ref: 'a'.repeat(64), media_type: 'image/png',
    data_base64: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1sAAAAASUVORK5CYII=' }]);
  expect(images.urls.get('a'.repeat(64))).toBe('blob:preview-one');
  images.dispose(); images.dispose();
  expect(revoke).toHaveBeenCalledTimes(1);
});
```

若当前Rstest环境未定义URL.createObjectURL，在test setup明确增加测试stub后spy并恢复；不能安装不必要的组件测试框架。
- [ ] **2. 跑red，实现幂等资源释放。**

```typescript
export function createPreviewImageURLs(attachments: PreviewAttachment[]) {
  const urls = new Map<string, string>();
  for (const item of attachments) {
    const bytes = Uint8Array.from(atob(item.data_base64), c => c.charCodeAt(0));
    const previous = urls.get(item.ref);
    if (previous) URL.revokeObjectURL(previous);
    urls.set(item.ref, URL.createObjectURL(new Blob([bytes], { type: item.media_type })));
  }
  let disposed = false;
  return { urls, dispose() { if (disposed) return; disposed = true;
    for (const url of urls.values()) URL.revokeObjectURL(url); urls.clear(); } };
}
```

生产函数先使用P4-T1 schema校验及预算验证，任一步创建失败也释放已经创建的URL。组件effect在换文件、换scope、退出登录、卸载时dispose。
- [ ] **3. 实现受控图片fetch的取消。** effect每次创建AbortController和active标志；await fetch/blob后都检查active，再createObjectURL；cleanup先active=false、abort、revoke已经创建的URL。409/403返回受控占位及刷新动作，不能读取旧blob缓存充当成功。
- [ ] **4. Markdown只解析真实image节点。** 用remark插件遍历mdast image节点，将合法`knowledge-attachment:<64hex>`映射为内部占位路径`/__knowledge-image/<ref>`，自定义img组件只从经过验证的imageSources中找图。代码块中的同样字面串不得被改写。其它图片节点显示“外部图片未加载”，不创建原始`<img src=https://...>`；不新增远程代理抓图。复用现有raw HTML禁用设置，链接允许安全用户主动跳转。
- [ ] **5. 接入三处知识库正文。** 预览用响应内缩略图映射；管理详情用management URL；检索详情用citation URL及当次version/digest。聊天模型自行写出的内部ref不能自动授权展示，只有服务器引用详情返回的attachment映射可以使用。
- [ ] **6. 测试安全和竞态green。** 浏览器拦截所有外部图域确认零请求；恶意SVG/HTML/javascript URL不执行；更换scope后旧请求resolve不创建新Blob；关闭详情后create/revoke数量平衡。

```bash
pnpm exec rstest tests/unit/core/knowledge/attachment-images.test.ts tests/unit/core/knowledge/markdown-images.test.ts
pnpm exec playwright test tests/e2e/project-knowledge.spec.ts --project=chromium
```

- [ ] **7. 交付A12/A17/A22。** 本任务不改变共享聊天Markdown的全局行为，只把可复用安全渲染能力应用到Knowledge正文。

## P4-T4：文档/分段治理、警告和旧参数显示

**Files**

- Modify: `frontend/src/components/projects/knowledge/knowledge-documents-view.tsx`、`knowledge-segments-browser.tsx`、`knowledge-search-panel.tsx`、`knowledge-base-detail.tsx`。
- Modify: `frontend/src/components/workspace/citations/knowledge-citations-panel.tsx`、`frontend/src/core/knowledge/source-position.ts`。
- Create: `frontend/src/core/knowledge/processing-profile.ts`。
- Modify: `frontend/src/core/i18n/locales/zh-CN.ts`、`en-US.ts`、`types.ts`。
- Modify: M11产出的 `frontend/src/components/admin/settings/admin-knowledge-settings-page.tsx`、`frontend/src/core/admin-settings/knowledge/types.ts`、`api.ts`。
- Test: `frontend/tests/unit/core/knowledge/source-position.test.ts`、新增`processing-profile.test.ts`；扩展`project-knowledge.spec.ts`。

**Interfaces**

- Consumes: Document.parsing_profile、warnings、真实task progress、Segment token_count/source_spans/attachments。
- Produces: 新旧单位可区分的维护视图，不另建前端OCR/任务状态机。

- [ ] **1. 写单位和来源投影红测。** 新增 `processingUnitLabel(profile): 'characters'|'knowledgeTokens'`，只读服务器unit；缺失旧profile按character兼容，不根据数字或文件类型猜测。source-position测试新增多来源列表、encoding不当作页号、source/context_prefix角色显示。

```typescript
import { expect, it } from '@rstest/core';
import { processingUnitLabel } from '@/core/knowledge/processing-profile';

it('does not reinterpret a legacy character profile', () => {
  expect(processingUnitLabel(null)).toBe('characters');
  expect(processingUnitLabel({ chunk: { unit: 'character' } })).toBe('characters');
  expect(processingUnitLabel({ chunk: { unit: 'token' } })).toBe('knowledgeTokens');
});
```

函数输入为 `Readonly<{chunk: Readonly<{unit:'character'|'token'}>}> | null`，不要求测试构造无关完整profile。
- [ ] **2. 实现警告与真实进度。** 显示“图片已保存，图片文字未识别”，单图失败数量、HEADER_INFERRED等安全说明；任务阶段来自后端枚举。总段数/Token/字符分别标注，不把省略缩略图当作图片丢失。
- [ ] **3. 编辑/重处理提示。** 手工新增/编辑Markdown提供当前文档附件选择器，禁止自由选择其他文档图片；reparse提示替换人工修改与附件绑定，reembed提示只重算向量。失败重处理显示旧published内容和其图片，不拼最新失败generation。
- [ ] **4. 系统设置只接两字段。** 在M11知识配置页增加etl_type和extraction_cache_enabled，沿用其保存/重启生效/权限规则；未完成M11页面时不另建独立YAML表单。OCR字段不偷偷加入。
- [ ] **5. 跑green与权限例。** 只读成员只能看内容，不能上传/编辑配置；禁用内容不出检索但管理可看；旧引用冲突提示刷新，不加载新世代图片替代。

```bash
pnpm exec rstest tests/unit/core/knowledge/source-position.test.ts tests/unit/core/knowledge/processing-profile.test.ts
pnpm check
pnpm exec playwright test tests/e2e/project-knowledge.spec.ts --project=chromium
```

- [ ] **6. 交付A18/A20/A21。** 更新frontend/AGENTS.md对应契约，避免称图片已具有OCR能力。

## P4-T5：真实后端浏览器与对象清理证据

**Files**

- Modify: `frontend/tests/e2e-real-backend/knowledge-real-backend.spec.ts`。
- Modify: `backend/tests/replay_knowledge.py`、`backend/scripts/run_replay_gateway.py`。
- Modify: `frontend/playwright.real-backend.config.ts`（仅必要的新探针配置，保留隔离端口）。
- Create: `frontend/tests/fixtures/knowledge-parsing/` 中确定的DOCX/PDF/CSV样例，由P1生成器导出。

**Interfaces**

- Consumes: 真实Gateway/Worker、随机PostgreSQL、独立MinIO测试bucket、deterministic replay模型。
- Produces: 端到端解析→预览→上传→检索→图片→reparse/reembed→删除证据，不依赖外部商业模型。

- [ ] **1. 扩展现有测试而非创建平行启动器。** 已有真实目录是`tests/e2e-real-backend/`，不能把路径写成`tests/e2e/knowledge-real-backend.spec.ts`。继续使用registerReplayProject与隔离3317/8117默认端口，不复用ambient开发服务。
- [ ] **2. 新增流程断言。** 在现有测试文件内部复用其private辅助函数，增加带图DOCX：预览前后Project对象列表相同；上传后原件+manifest+asset可见；详情图片200且MIME为安全图；调用reembed对象key集合不增；调用reparse改变参数后，已级联删除的旧Segment引用按防枚举契约返回404且不读对象（只有仍存续Segment的version/digest过期才返回409）；删除后Project前缀对象清空。

在该文件中新增断言助手：

```typescript
async function expectNoNewObjectsDuring(page: Page, project: ReplayProjectScope, action: () => Promise<void>) {
  const before = (await listProjectObjects(page.context(), project)).sort();
  await action();
  expect((await listProjectObjects(page.context(), project)).sort()).toEqual(before);
}
```

`listProjectObjects`已在同文件定义，禁止从它导入到另一测试文件。action对应真实UI选择文件并等待preview响应，不等待固定秒数。
- [ ] **3. 扩展失败探针。** 复用现有replay故障入口增加对象PUT失败/删除失败、模型batch阻塞和来源读取计数；只在test-only路由允许，生产注册不得开放。验证失败不提前暴露图，删除重试后对象与quota归零。
- [ ] **4. 先检查环境名，不打印值。** 当前真实套件缺MinIO变量会skip，skip不是通过。预先验证`ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY`和测试数据库配置可用，沿用replay创建随机库与bucket的生命周期。

```bash
../backend/.venv/bin/python - <<'PY'
import os
names = ('ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT', 'ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY',
         'ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY')
missing = [name for name in names if not os.environ.get(name, '').strip()]
if missing:
    raise SystemExit('Missing test environment names: ' + ', '.join(missing))
print('MinIO test environment names present; values suppressed')
PY
pnpm exec playwright test --config playwright.real-backend.config.ts tests/e2e-real-backend/knowledge-real-backend.spec.ts --project=chromium --workers=1
```

该命令须在已加载测试环境的frontend目录执行；replay读取根开发环境只用于创建独立测试资源，不能指向业务库执行DDL。若环境未就绪，记录未运行原因，不改test.skip绕过依赖。
- [ ] **5. 补跨账号/过期引用例并跑green。** 对来自另一Project的attachment_id、已停用引用、撤权后的图片请求均断言状态；管理浏览旧published图继续受read权限保护。
- [ ] **6. 交付A12–A22/A28/A30真实证据。** 保存无内容/密钥的对象计数与状态，不把MinIO真实key或图片字节写入共享诊断包。

## P4-T6：离线生产镜像、检索对比与最终交付

> 2026-09-01 范围更新：用户明确不使用 Docker 部署，因此本任务的 Dockerfile、dev-entrypoint 和生产镜像门从交付范围移除；保留本地/裸机安装入口、平台资源锁、OS 隔离 fail-closed 行为和其余质量门。

**Files**

- Modify: `backend/Dockerfile`、`README.md`、`Install.md`、`backend/AGENTS.md`、`frontend/AGENTS.md`、`CONTEXT.md`。
- Modify: `Makefile`、`backend/Makefile`、`scripts/serve.sh`、`docker/dev-entrypoint.sh`（仅安装入口保留必需解析 extra）；Create: `backend/tests/test_knowledge_dependency_install.py`。
- Modify: `backend/tests/knowledge/test_extraction_resources.py`（多平台资源锁的当前平台比较与保留其他平台条目回归）。
- Create: `backend/tests/knowledge/test_parsing_quality.py`、`fixtures/parsing_retrieval_cases.json`。
- Modify: `backend/tests/knowledge/eval_quality.py`、`eval_metrics.py`（复用M10评测基础，补现有尚无的reciprocal_rank_at_k）。
- Create: `backend/tests/knowledge/parsing_quality.py`（测试用的基线导出/双语料评测适配），不进入生产包。
- Create: `docs/knowledge/parsing-quality-eval-report.md`、`parsing-quality-eval-report.json`（执行后生成，不预填成功）。

**Interfaces**

- Consumes: P1–P3全部实现、M10/M11已有检索评测基础。
- Produces: A01–A30覆盖表、真实依赖/资源manifest、测试计数与环境限制，明确无生产迁移授权。

- [ ] **1. 定义固定质量集。** 至少包含表头缺失列、设备编号前导零、长表跨段、Word标题与步骤分离、Markdown泛型字面值、纯图片不误报可检索这6类；每类至少3条查询和人工确认来源。相同原件分别经基线字符解析和新解析，使用同一个模型/检索配置与top_k=5。
- [ ] **2. 写确定性质量测试再实现评测适配。** 当前 `eval_metrics.py` 已有 `recall_hit/mean_or_none`，没有MRR函数；在同文件补 `reciprocal_rank_at_k(target_ids:Sequence[str], retrieved_ids:Sequence[str], k:int=5)->float`，沿现有测试的手算样例验证第2位为1/2、超过第5位为0。不要在测试中定义第二套公式再测试自己。

```python
from eval_metrics import mean_or_none, recall_hit, reciprocal_rank_at_k

def test_quality_metric_fixture_is_unambiguous():
    ranked = ['wrong', 'correct']
    assert recall_hit(['correct'], ranked[:5]) is True
    assert reciprocal_rank_at_k(['correct'], ranked, k=5) == 0.5
    assert reciprocal_rank_at_k(['correct'], ['wrong'] * 5 + ['correct'], k=5) == 0.0
    assert mean_or_none([0.5, 0.0]) == 0.25
```

新增函数实现放在 `eval_metrics.py`，原M10的nDCG/Recall@10保持不变：

```python
from collections.abc import Sequence

def reciprocal_rank_at_k(target_ids: Sequence[str], retrieved_ids: Sequence[str], k: int = 5) -> float:
    targets = set(target_ids)
    for rank, item_id in enumerate(retrieved_ids[:k], start=1):
        if item_id in targets:
            return 1.0 / rank
    return 0.0
```

`parsing_quality.py` 定义 `run_parsing_quality_eval(postgres_database_url:str, *, baseline_path:Path, cases_path:Path, api_key:str|None)->dict`。提取基线来自固定 `b96581974b057c0ae4d853815130d99c0ed23823` 的隔离测试checkout，通过其真实 extractor/cleaner/splitter 导出原件SHA、content、source_position与参数；不得在新生产包保留旧解析器作为隐藏fallback。候选用新流程处理完全相同的原件。两份段在同一新随机库建立两个测试库，沿 `eval_quality.py` 的模型注册/客户端与production search服务评测，固定同一Embedding、Reranker、维度、检索参数与top_k=5；原M10的 `run_quality_eval` 入口和报告不改名、不覆盖。

fixture 的相关性标注使用 `source_id + 原始位置(page/paragraph/sheet/row等) + 必须出现的原文`；评测适配器按实际返回段的覆盖来源映射到这些标签，不能按变化后的Segment UUID或新旧chunk位置比较。没有精确位置的基线标记来源缺失，不编造映射。纯图片查询单独统计预期无可索引内容，不混入有答案查询的Hit/MRR均值。replay模式仅验证流程与关键来源断言，真实模型模式才产出质量比较结论。

`test_parsing_quality.py` 分为纯度量测试、随机PG+replay流程测试以及显式 `provider_integration` 真实模型测试。后者只有 `ACT_WEAVE_KNOWLEDGE_PARSING_QUALITY_EVAL=1` 才启用，沿现有 `resolve_provider_api_key` 获取凭据；启用但无凭据应失败，不悄悄skip。分别执行：

```bash
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_parsing_quality.py -q -m 'not provider_integration'
ACT_WEAVE_KNOWLEDGE_PARSING_QUALITY_EVAL=1 PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_parsing_quality.py -q -m provider_integration
```

第二条会调用已配置真实模型，仅在执行时已授权真实Provider验证且配置就绪后运行。报告记录模型配置ID/修订、语料摘要、输入profile、成功/失败数、每类Hit@5/MRR@5与变化；关键来源不得回退。未运行真实模型就将A27记为未验证。
- [ ] **3. 构建镜像并在无网络环境跑格式矩阵。**

先修改Dockerfile：builder固定安装knowledge的 `extraction-local` extra（现有可选UV_EXTRAS仍保留），在显式构建阶段运行P1资源准备和P3Tokenizer准备；runtime必须带相同版本的libmagic与bubblewrap及已准备的Pandoc/NLP资源。不得只在builder安装动态库后遗漏runtime，也不把系统隔离缺失变成裸跑。两个阶段的必要系统包版本在构建记录中固定并由runtime probe比对。核心Python准备命令在 `/app/backend` 执行：

```bash
uv sync --all-packages --extra extraction-local
.venv/bin/python scripts/build_extraction_resources.py --output packages/knowledge/actweave_knowledge/extraction/resources.lock.json
.venv/bin/python scripts/prepare_knowledge_tokenizer.py --output packages/knowledge/actweave_knowledge/ingestion/tokenizer_data
```

本地安装和开发容器启动也必须保留 `--all-packages --extra extraction-local`。现有 `make install`、`scripts/serve.sh` 和 `docker/dev-entrypoint.sh` 会执行精确 `uv sync`，若未显式选择该 extra，启动时会卸载已经准备的长尾解析/NLP 依赖；不能只修生产 Dockerfile。仅在这些安装入口加入必需 extra，保留既有可选 `UV_EXTRAS` 检测/校验，不把 PostgreSQL ETL 设置搬回 YAML。用开发入口现有 `--print-extras` 和隔离的命令捕获测试确认必需与可选 extra 同时保留；不启动或改动用户正在运行的服务。系统资源安装和资源锁的生成仍是明确环境准备步骤，不从解析请求触发。

加入 Linux 资源锁条目时同时收敛 P1-T7 审阅保留的小问题：`test_build_manifest_is_reproducible` 不能把一次本机生成的单平台 map 与完整多平台 lock map 比较。比较当前平台的条目，并另测更新 lock 时保留其他平台条目；不因跨平台新增而误报资源不一致。

随后从仓库根目录构建并验证。下面的运行不添加privileged或宽泛capability；若Docker默认策略不允许P1的隔离，则记录部署前置失败，先确定并验证最小隔离部署方式，不能擅自扩大容器权限来让测试变绿。

```bash
docker build -f backend/Dockerfile -t actweave-rag-plan:local .
docker run --rm --network none --read-only --tmpfs /tmp:rw,size=536870912 --workdir /app/backend --entrypoint /app/backend/.venv/bin/python actweave-rag-plan:local -m pytest tests/knowledge/test_dify_text_extractors.py tests/knowledge/test_dify_tabular_extractors.py tests/knowledge/test_dify_office_pdf.py tests/knowledge/test_local_unstructured.py tests/knowledge/test_extraction_runtime.py tests/knowledge/test_extraction_offline_matrix.py tests/knowledge/test_knowledge_tokenizer.py -q -p no:cacheprovider
```

先确认P1已创建上述测试；容器依赖缺失或资源下载尝试必须失败，不能跳过格式。生产镜像不包含Dify源码目录依赖、不依赖用户缓存。分别验证实际目标CPU架构的二进制wheel与系统资源，无法运行的架构只报告未验证。
- [ ] **4. 运行模块与整体门禁。**

```bash
cd backend
make format
make lint
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/ -q -m 'not provider_integration'
make test
uv run python scripts/generate_schema_comments.py --check
```

```bash
cd frontend
pnpm check
pnpm test
pnpm exec playwright test tests/e2e/project-knowledge.spec.ts --project=chromium
pnpm exec playwright test --config playwright.real-backend.config.ts tests/e2e-real-backend/knowledge-real-backend.spec.ts --project=chromium --workers=1
```

两个代码块分别从仓库根目录开始；不要在backend目录继续执行`cd frontend`。 全后端门包含真实 MinIO 测试；core_gate_plugin 仅加载 DATABASE_URL，不加载其余 .env。运行前在测试子进程环境中仅注入已配置的 ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY 三个测试变量（优先保留调用者值），不整份source .env、不输出值；这些测试只创建并清理随机临时bucket。缺少或不可连接时明确记门禁未通过，不能跳过后称全量通过。记录各门实际计数和跳过原因，数据库schema安装只在fixtures创建的新空库发生。
- [ ] **5. 更新文档与术语。** CONTEXT新增Knowledge Extraction/Attachment含义；指南和README更新本地ETL格式、Token口径、图片仍无OCR、预览不持久化、配额和重处理语义。保留原Schema V1无在线升级约束，Install说明构建时资源准备和启动探测。
- [ ] **6. 完成最终核对。** 将A01–A30映射到任务和测试node ID，实际报告中零未说明失败；完整原件、图片、缓存和临时对象清理可复现。现有开发工作区无关修改未被纳入。用户授权提交后按变更归属逐文件提交；不自动push或部署。

## 执行完成后的报告边界

必须分别陈述：代码/纯解析测试、真实PostgreSQL与MinIO、浏览器、离线生产镜像、真实OCR（本期无）、真实Embedding/Reranker质量是否验证。只有P1–P4和全部必要门禁完成，才可称本规格已实现。

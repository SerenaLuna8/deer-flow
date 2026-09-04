# RAG 用户提示与旧版本编辑错误交互 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 三个分段配置入口准确说明 Knowledge Token、预览能力和 overlap 边界，并证明旧 token parent-child 分段新增/修改失败时保留未保存输入、不自动重新解析。

**Architecture:** 复用现有 `wizard.knowledgeTokenUnit`、`documents.chunkOverlapHint` 与当前实际渲染的字段，不新增面板、模型字段或请求参数。编辑错误继续由现有 `knowledgeErrorMessage`、Mutation 状态和 Sheet 本地输入缓冲呈现；本计划只为已经满足的错误行为增加回归，不重写治理流程。

**Tech Stack:** Next.js、React、TypeScript、现有 i18n、TanStack Query、`@rstest/core`、Playwright；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-09-04-rag-quality-optimization-design.md`，§8 A4、§9.2、A13/A16；同时阅读仓库根与 `frontend/AGENTS.md`。

## Global Constraints

完整继承总入口 `docs/superpowers/plans/2026-09-04-rag-quality-optimization.md` 的十项 Global Constraints；以下是本子计划的重点和附加约束。

- 本计划是用户要求的实施计划产物，不授权现在修改业务代码、提交、部署、数据库操作或外部模型调用。实施须另获授权；提交步骤以审查交付 checkpoint 代替。
- 保留用户未提交修改。当前 Knowledge UI 正在抽取共享组件；不接管、不回退、不顺带完成该重构。
- token profile 父段默认 1000、overlap 默认 100；父子模式子块默认 500。父段范围 200..4000、overlap 0..500 且小于父段、子块范围 100..2000 且小于父段，不变；历史 character 值不套用这一 Token 口径。
- token profile 的父段和 Child 分别满足显示 Markdown Token、`index_text` Token 及 16000 字符上限；标题、表头、分隔符和保护字符均参与相关预算。overlap 不是额外赠送的预算。
- 每文档父分段最多 5000，父子模式的累计 Child 向量条目另限 5000；两者不是合计 5000。沿用当前其余配额检查，不增加上限。
- Child 在各自父段内零重叠。父段重叠后，不同父段的 Children 可能覆盖相同原文；不新增跨父段全局去重。
- 不改变默认参数、请求身份或现有预览交互；纯文案不改变 processing profile、capability revision、fingerprint 或处理版本。
- 不恢复每个文档的内部 parser/tokenizer/version 元数据行，不新增大量重复 warning，不新增模型容量字段或配置面板。
- 前端继续依赖服务端权限和错误响应；不得根据浏览器中的版本号自行判定执行权限，亦不得因错误自动提交 reparse。
- 旧 token parent-child 的二次派生由后端拒绝；前端保留本次未保存编辑。普通 Segment 编辑及 null/character 路径不得因本提示包额外被阻断。
- 模型超限风险仍然存在；A4 不是 B2 模型真实容量保护的实现。不得宣称 500/1000 Knowledge Tokens 对所选模型必然安全。
- README 与 owning guide 文案由总集成计划单一维护；本分计划不安排其他执行者并写这些文件。

## 当前落点与执行前核对

2026-09-04 当前源码检查结果：

- `knowledge-create-wizard.tsx` 已渲染 `wizard.knowledgeTokenUnit`；创建上传和已有库上传共用它。
- 实施前复核发现用户已完成新的接线：上传 wizard 实际调用 `KnowledgeChunkSettingsFields`；显式重新解析配置由独立 `KnowledgeDocumentChunkSettings` 页面承载，该页面也调用同一字段组件。
- `knowledge-documents-view.tsx` 的操作菜单通过 `documents.chunkSettings` 打开分段设置；`project-knowledge-page.tsx` 渲染 `data-testid="knowledge-document-chunk-settings"` 的页面，而不是 Dialog。该页尚未渲染 Knowledge Token 说明；进入页面自动预览当前参数，后续刷新使用 `wizard.previewRefresh`。
- overlap 提示应在 `knowledge-chunk-settings-fields.tsx` 添加一次；单位说明保留上传页已有位置，只补到文档分段设置页。`knowledge-chunk-preview-list.tsx` 仍属用户的现有重构，不在本计划修改范围。
- `documents.chunkOverlapHint` 目前是字符单位旧文案且没有 JSX 消费者；本计划把它改为 token overlap 语义，复用 key，不新增翻译类型字段。
- Add/Edit Segment Sheet 的 `content` 在本地 `useState` 中，Mutation 仅在 `onSuccess` 关闭；错误由 `<p role="alert">` 展示，现有代码已经保留失败输入。
- `ExtractionError.reason_code` 不在 HTTP envelope 中。真实响应是 HTTP 422、`detail.code = "KNOWLEDGE_PARSE_FAILED"`、服务端 `detail.message`；不要在 mock 或前端发明 `reason_code` 字段或 `PROCESSING_PROFILE_UNAVAILABLE` 顶层 HTTP code。

- [ ] **P0：执行前重新核对渲染所有者与工作区。**

从仓库根运行：

```bash
git status --short
rg -n 'KnowledgeChunkSettingsFields|knowledgeTokenUnit|chunkOverlapTokenLabel|chunkOverlapHint' frontend/src/components/projects/knowledge/knowledge-create-wizard.tsx frontend/src/components/projects/knowledge/knowledge-document-chunk-settings.tsx frontend/src/components/projects/knowledge/knowledge-chunk-settings-fields.tsx
rg -n 'onSuccess:.*onClose|role="alert"|useState\(segment.content\)' frontend/src/components/projects/knowledge/knowledge-segments-browser.tsx
```

预期：确认真实 JSX 调用而非仅导入；本计划已按当前共享字段和独立分段设置页更新落点。若接线再次变化，只更新受影响锚点，不把共享组件重构变成本优化的任务。不要恢复已删除的 Dialog，也不要同时往共享与内联路径添加重复提示。

---

## Task UI-1：三个入口的单位、预览限制与 overlap 提示

**Files:**

- Modify: `frontend/src/core/i18n/locales/zh-CN.ts` — `knowledge.wizard.knowledgeTokenUnit` 与 `knowledge.documents.chunkOverlapHint`。
- Modify: `frontend/src/core/i18n/locales/en-US.ts` — 相同两个 key。
- Modify: `frontend/src/components/projects/knowledge/knowledge-chunk-settings-fields.tsx` — 实际共享 overlap Input 与包含三个父段输入的 grid 后；不改变接线。
- Modify: `frontend/src/components/projects/knowledge/knowledge-document-chunk-settings.tsx` — `DocumentChunkSettingsForm` 的当前配置说明之后、重新解析警告之前补充单位说明。
- Read-only: `frontend/src/components/projects/knowledge/knowledge-create-wizard.tsx` — 保留已有单位说明；消费共享字段提示，无需重复添加。
- Read-only: `frontend/src/components/projects/knowledge/knowledge-documents-view.tsx`、`project-knowledge-page.tsx` — 保留分段设置页入口与权限，不恢复旧 Dialog。
- Read-only: `frontend/src/core/knowledge/chunk-settings.ts`、`frontend/src/core/knowledge/preview-identity.ts`、`frontend/src/core/knowledge/types.ts` — 参数与身份契约不变。
- Create test: `frontend/tests/unit/components/projects/knowledge/knowledge-guidance.test.ts`。
- Modify test: `frontend/tests/e2e/project-knowledge.spec.ts` — 复用现有 `mockKnowledgeRoutes`、`MockBase`、`MockDocument`、`json`、`knowledgeError`，测试代码写在同一文件以访问这些真实 helper。

**Interfaces:**

- Consumes: `useI18n().t.knowledge`；`wizard.knowledgeTokenUnit: string`；`documents.chunkOverlapHint: string`。
- Consumes: 当前 `KnowledgePreviewParams` 与 `KnowledgeReparseInput`；不增加字段，不修改构造函数、默认 state 或验证函数。
- Produces: 三入口可见的两项提示；overlap Input 的 `aria-describedby` 指向同表单唯一提示，不把提示放入 label 改变控件 accessible name。
- 消费现有 `KnowledgeChunkSettingsFields({ value, onChange, disabled, limits, radioName })`，签名不变。

- [ ] **Step 1：新增精确中英文文案单测。**

新建测试文件，完整内容：

```ts
import { expect, test } from "@rstest/core";

import { enUS, zhCN } from "@/core/i18n/locales";

test("RAG guidance states model capacity and bounded overlap in both locales", () => {
  expect(zhCN.knowledge.wizard.knowledgeTokenUnit).toBe(
    "“知识库 Token”是固定的本地分段单位，不等于所选 Embedding 模型的输入 Token。分段预览不校验模型输入上限；请结合所选模型和服务的限制配置分段。",
  );
  expect(enUS.knowledge.wizard.knowledgeTokenUnit).toBe(
    "Knowledge Tokens are a fixed local chunking unit, not the input tokens of the selected Embedding model. Chunk previews do not validate model input limits; configure chunking according to the selected model and service limits.",
  );
  expect(zhCN.knowledge.documents.chunkOverlapHint).toBe(
    "最多保留设定数量的正文 Token；不跨页面、标题或表格边界，实际重叠可能更少。",
  );
  expect(enUS.knowledge.documents.chunkOverlapHint).toBe(
    "Retains at most the configured number of body-text Knowledge Tokens. Overlap does not cross page, heading, or table boundaries and may be smaller.",
  );
});
```

- [ ] **Step 2：追加三入口 × 两语言浏览器断言。**

在 `project-knowledge.spec.ts` imports 增加：

```ts
import { enUS, zhCN } from "@/core/i18n/locales";
```

同文件末尾追加以下完整测试块；它使用已存在的 mock，不产生真实 Gateway/模型请求：

```ts
for (const [locale, copy] of [
  ["en-US", enUS.knowledge],
  ["zh-CN", zhCN.knowledge],
] as const) {
  for (const entry of ["new", "existing", "reparse"] as const) {
    test(`RAG guidance ${locale} ${entry} preserves defaults and preview payload`, async ({
      page,
      baseURL,
    }) => {
      if (!baseURL) throw new Error("Playwright baseURL is required");
      await page.context().addCookies([
        { name: "locale", value: locale, url: baseURL },
      ]);
      const baseId = "40000000-0000-4000-8000-000000000001";
      const documentId = "50000000-0000-4000-8000-000000000001";
      const base: MockBase = {
        id: baseId,
        name: "RAG guidance",
        description: "",
        status: "active",
        document_count: entry === "reparse" ? 1 : 0,
        delete_error: null,
        embedding_model_id: MODEL_ID,
      };
      const document: MockDocument = {
        id: documentId,
        knowledge_base_id: baseId,
        name: "guidance.txt",
        original_name: "guidance.txt",
        status: "ready",
        segment_count: 1,
        content_initialized: true,
        error_message: null,
        delete_error: null,
      };
      const state = await mockKnowledgeRoutes(page, {
        bases: entry === "new" ? [] : [base],
        documents: entry === "reparse" ? [document] : [],
      });
      await page.goto(
        entry === "new"
          ? "/projects/alpha/knowledge"
          : `/projects/alpha/knowledge?kb=${baseId}`,
      );

      if (entry === "reparse") {
        await page.getByRole("button", {
          name: copy.documents.actionsAria(document.name),
          exact: true,
        }).click();
        await page.getByRole("menuitem", {
          name: copy.documents.chunkSettings,
          exact: true,
        }).click();
      } else {
        await page.getByRole("button", {
          name: entry === "new"
            ? copy.wizard.uploadCreateTitle
            : copy.documents.uploadButton,
          exact: true,
        }).click();
        await page.getByLabel(copy.documents.fileLabel, { exact: true }).setInputFiles({
          name: "guidance.txt",
          mimeType: "text/plain",
          buffer: Buffer.from("RAG guidance fixture"),
        });
        await page.getByRole("button", {
          name: copy.wizard.next,
          exact: true,
        }).click();
      }

      const form = entry === "reparse"
        ? page.getByTestId("knowledge-document-chunk-settings")
        : page.getByTestId("knowledge-create-wizard");
      await expect(form.getByText(copy.wizard.knowledgeTokenUnit, { exact: true })).toBeVisible();
      await expect(form.getByText(copy.documents.chunkOverlapHint, { exact: true })).toBeVisible();
      await expect(form.getByLabel(copy.wizard.chunkSizeTokenLabel, { exact: true })).toHaveValue("1000");
      const overlap = form.getByLabel(copy.wizard.chunkOverlapTokenLabel, { exact: true });
      await expect(overlap).toHaveValue("100");
      await expect(overlap).toHaveAccessibleDescription(copy.documents.chunkOverlapHint);
      await expect(form.getByLabel(copy.documents.chunkSeparatorLabel, { exact: true })).toHaveValue("\\n\\n");

      if (entry === "reparse") {
        await expect(form).toContainText(copy.documents.reparseWarning);
        // The current settings page previews once on entry; do not trigger a second request.
        await expect.poll(() => state.reparsePreviewRequests.length).toBe(1);
        expect(state.reparsePreviewRequests[0]).toEqual({
          expected_version: 1,
          chunk_size: 1000,
          chunk_overlap: 100,
          chunk_separator: "\\n\\n",
          remove_extra_spaces: false,
          remove_urls_emails: false,
          chunking_mode: "general",
        });
        await form.getByRole("button", { name: copy.documents.reparseSubmit, exact: true }).click();
        await expect.poll(() => state.reparseRequests.length).toBe(1);
        expect(state.reparseRequests[0]).toEqual(state.reparsePreviewRequests[0]);
      } else {
        await expect.poll(() => state.previewProcessingRequests.length).toBe(1);
        const preview = state.previewProcessingRequests[0]!;
        expect(preview.profile).toEqual({
          unit: "token",
          mode: "general",
          size: 1000,
          overlap: 100,
          separator: "\\n\\n",
          child_size: 500,
          child_separator: "\\n",
          remove_extra_spaces: false,
          remove_urls_emails: false,
          header_rules: [],
        });
        await expect(form.getByTestId("chunk-preview-panel")).toContainText(
          copy.wizard.previewHint("guidance.txt"),
        );
        if (entry === "new") {
          await form.getByRole("combobox", { name: copy.bases.modelLabel, exact: true }).click();
          await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3", exact: true }).click();
        }
        await form.getByRole("button", {
          name: entry === "new" ? copy.wizard.saveAndProcess : copy.wizard.uploadAction,
          exact: true,
        }).click();
        await expect.poll(() => state.uploadProcessingRequests.length).toBe(1);
        expect(state.uploadProcessingRequests[0]).toEqual({
          fileName: "guidance.txt",
          profile: preview.profile,
          expectedFingerprint: preview.fingerprint,
        });
        if (entry === "existing") {
          expect(state.baseCreates).toEqual([]);
          expect(state.baseUpdates).toEqual([]);
        }
      }
    });
  }
}
```

此测试明确验证可见性、默认值、精确 payload 和已存在 fingerprint，不用“只数 key”代替请求契约。当前分段设置页仍使用兼容顶层 chunk 字段，测试照真实 `currentInput` 断言；进入页面自动发起预览，后续手动刷新使用 `wizard.previewRefresh`，不重新引入旧 Dialog 的按钮或行为。

- [ ] **Step 3：先运行并记录预期失败。**

工作目录 `frontend/`：

```bash
pnpm exec rstest run tests/unit/components/projects/knowledge/knowledge-guidance.test.ts
pnpm test:e2e tests/e2e/project-knowledge.spec.ts --grep 'RAG guidance' --reporter=line
```

预期：单测因原文案不匹配失败；浏览器测试至少在 overlap 提示/描述缺失处失败，reparse 还缺单位说明。若构建因用户正在进行的重构失败，记录真实阻碍，不把构建失败当成目标 RED；不修无关重构来凑绿。

- [ ] **Step 4：只改两个翻译 key 与实际表单提示。**

按 Step 1 的精确字符串替换 `zh-CN.ts`、`en-US.ts` 的两个现有值；无需修改 `locales/types.ts`。保留 wizard 已有的单位说明。

在 `knowledge-document-chunk-settings.tsx` 的 `DocumentChunkSettingsForm` 中，当前配置说明之后、重新解析警告之前增加：

```tsx
<p className="text-muted-foreground text-xs leading-5">
  {labels.wizard.knowledgeTokenUnit}
</p>
```

在 `KnowledgeChunkSettingsFields` 的 overlap Input 增加以下属性，并在父段参数 grid 后增加提示；上传和分段设置页自动复用，不并存重复 hint：

```tsx
aria-describedby={`${radioName}-overlap-hint`}
```

```tsx
<p id={`${radioName}-overlap-hint`} className="text-muted-foreground text-xs leading-5">
  {labels.documents.chunkOverlapHint}
</p>
```

这两段直接作用于当前已接线的共享字段，不改变调用关系。不得修改 state 默认值、submit handler、preview 重置 effect、请求组装、角色权限或 `knowledge-chunk-preview-list.tsx`。

- [ ] **Step 5：运行聚焦回归并检查父子模式已有覆盖。**

```bash
pnpm exec rstest run tests/unit/components/projects/knowledge/knowledge-guidance.test.ts tests/unit/core/knowledge/chunk-settings.test.ts tests/unit/core/knowledge/preview-identity.test.ts
pnpm test:e2e tests/e2e/project-knowledge.spec.ts --grep 'RAG guidance|wizard parent-child mode|chunk settings open as a page|a stale chunk-settings preview|cancelling chunk settings|freezes wizard controls|an existing configured base uploads' --reporter=line
```

预期：新增 6 个入口/语言用例通过；现有父子预览、上传冻结、过期预览、重新解析 CAS 用例保持原断言通过。它们是 mock Gateway 的浏览器验证，不证明实际解析器、模型或生产发布。

- [ ] **Step 6：UI-1 审查交付 checkpoint。**

记录精确命令、通过数量、失败/未跑原因，并用 `git diff --check` 和指定文件 diff 确认只增加提示/测试。新建文件也需直接读取审查，不能只看不包含 untracked 文件的 `git diff`。不提交；交给总集成人员确认用户重构未被覆盖。

## Task UI-2：旧 token parent-child 手工新增/修改的失败回归

**Files:**

- Modify test: `frontend/tests/e2e/project-knowledge.spec.ts`。
- Create test: `frontend/tests/unit/components/projects/knowledge/knowledge-error.test.ts`。
- Read-only production anchors: `frontend/src/components/projects/knowledge/knowledge-error.ts`、`knowledge-segments-browser.tsx` 的 `AddSegmentSheet`/`EditSegmentSheet`、`frontend/src/core/knowledge/api.ts`、`hooks.ts`。
- No planned production modification: 这些路径目前已经保留输入和显示服务端错误；不得为已有行为增加一层状态机或自动 reparse 分支。

**Interfaces:**

- Consumes: 后端 A1/A4 的旧 token splitter 拒绝行为；真实 wire envelope 为 `{ detail: { code: "KNOWLEDGE_PARSE_FAILED", message: "原解析配置已不可用，请显式重新解析", request_id: string } }`，HTTP 422。
- Consumes: `KnowledgeApiError(status, "REQUEST_FAILED", message, { knowledgeCode, serverMessage })` 与 `knowledgeErrorMessage(error, messages): string`。
- Produces: 可见 `role="alert"`、未关闭且可继续编辑的 Sheet、本地 buffer 原样保留、旧已发布内容不被前端替换、零 reparse/reparse-preview 请求。
- 错误正文遵守当前 server-message-first 契约；两语言界面均可读到服务器提供的明确提示，不通过匹配中文 message 猜测 reason 或自行翻译特定后端错误。

- [ ] **Step 1：新增真实错误映射单测。**

```ts
import { expect, test } from "@rstest/core";

import { knowledgeErrorMessage } from "@/components/projects/knowledge/knowledge-error";
import { enUS, zhCN } from "@/core/i18n/locales";
import { KnowledgeApiError } from "@/core/knowledge/api";

test("RAG legacy profile rejection keeps the explicit server reparse message", () => {
  const message = "原解析配置已不可用，请显式重新解析";
  const error = new KnowledgeApiError(422, "REQUEST_FAILED", message, {
    knowledgeCode: "KNOWLEDGE_PARSE_FAILED",
    serverMessage: message,
  });
  for (const copy of [enUS.knowledge, zhCN.knowledge]) {
    expect(knowledgeErrorMessage(error, copy.errors)).toBe(message);
  }
});
```

- [ ] **Step 2：追加新增/编辑 × 两语言的失败交互测试。**

以下完整代码追加到 UI-1 已导入 `enUS`/`zhCN` 的现有 E2E 文件，无需修改 mock helper 或导出生产内部 Sheet：

```ts
for (const [locale, copy] of [
  ["en-US", enUS.knowledge],
  ["zh-CN", zhCN.knowledge],
] as const) {
  for (const operation of ["add", "edit"] as const) {
    test(`RAG legacy edit ${locale} ${operation} keeps unsaved content without reparse`, async ({
      page,
      baseURL,
    }) => {
      if (!baseURL) throw new Error("Playwright baseURL is required");
      await page.context().addCookies([
        { name: "locale", value: locale, url: baseURL },
      ]);
      const baseId = "40000000-0000-4000-8000-000000000001";
      const documentId = "50000000-0000-4000-8000-000000000001";
      const segmentId = "60000000-0000-4000-8000-000000000001";
      const original = "原有父段内容";
      const message = "原解析配置已不可用，请显式重新解析";
      const state = await mockKnowledgeRoutes(page, {
        bases: [{
          id: baseId,
          name: "Legacy profile",
          description: "",
          status: "active",
          document_count: 1,
          delete_error: null,
          embedding_model_id: MODEL_ID,
        }],
        documents: [{
          id: documentId,
          knowledge_base_id: baseId,
          name: "legacy.txt",
          original_name: "legacy.txt",
          status: "ready",
          segment_count: 1,
          content_initialized: true,
          error_message: null,
          delete_error: null,
          chunk_size_unit: "token",
          chunking_mode: "parent_child",
          parsing_profile: {
            ...PROCESSING_PROFILE,
            chunk: {
              ...PROCESSING_PROFILE.chunk,
              unit: "token",
              mode: "parent_child",
              splitter_version: "splitter-v2",
            },
          },
        }],
        segments: {
          [documentId]: [{
            id: segmentId,
            position: 1,
            content: original,
            enabled: true,
            source_position: { page: 1 },
          }],
        },
      });
      const rejected: Array<Record<string, unknown>> = [];
      const path = operation === "add"
        ? `/api/projects/${PROJECT_ID}/knowledge/documents/${documentId}/segments`
        : `/api/projects/${PROJECT_ID}/knowledge/segments/${segmentId}`;
      await page.route(`**${path}`, async (route) => {
        const request = route.request();
        if (request.method() !== (operation === "add" ? "POST" : "PATCH")) {
          await route.fallback();
          return;
        }
        rejected.push(request.postDataJSON() as Record<string, unknown>);
        await knowledgeError(route, 422, "KNOWLEDGE_PARSE_FAILED", message);
      });
      await page.goto(`/projects/alpha/knowledge?kb=${baseId}&doc=${documentId}`);
      const browser = page.getByTestId("knowledge-segment-browser");
      const list = browser.getByTestId("knowledge-segment-list");
      await expect(list.getByText(original, { exact: true })).toBeVisible();
      if (operation === "add") {
        await browser.getByRole("button", { name: copy.segments.add, exact: true }).click();
      } else {
        await list.getByRole("listitem").filter({ hasText: original })
          .getByRole("button", { name: copy.segments.edit, exact: true }).click();
      }
      const sheet = page.getByRole("dialog");
      const buffer = "  未保存的正文\n第二行  ";
      await sheet.getByLabel(copy.segments.contentLabel, { exact: true }).fill(buffer);
      await sheet.getByRole("button", { name: copy.common.save, exact: true }).click();
      await expect(sheet.getByRole("alert")).toHaveText(message);
      await expect(sheet).toBeVisible();
      await expect(sheet.getByLabel(copy.segments.contentLabel, { exact: true })).toHaveValue(buffer);
      expect(rejected).toEqual([{ content: buffer.trim() }]);
      await expect(sheet.getByRole("button", { name: copy.common.save, exact: true })).toBeEnabled();
      await sheet.getByLabel(copy.segments.contentLabel, { exact: true }).fill(`${buffer}继续编辑`);
      await expect(sheet.getByLabel(copy.segments.contentLabel, { exact: true })).toHaveValue(`${buffer}继续编辑`);
      expect(state.reparseRequests).toEqual([]);
      expect(state.reparsePreviewRequests).toEqual([]);
      expect(state.segments.get(documentId)?.map((item) => item.content)).toEqual([original]);
      expect(state.documents[0]?.status).toBe("ready");
      await sheet.getByRole("button", { name: copy.common.cancel, exact: true }).click();
      await expect(sheet).toHaveCount(0);
      await expect(list.getByText(original, { exact: true })).toBeVisible();
    });
  }
}
```

- [ ] **Step 3：运行现有行为回归，不制造假 RED。**

```bash
pnpm exec rstest run tests/unit/components/projects/knowledge/knowledge-error.test.ts
pnpm test:e2e tests/e2e/project-knowledge.spec.ts --grep 'RAG legacy edit|segment browser edits' --reporter=line
```

预期：现有错误处理本来正确，因此本回归可立即通过；记录为新增已满足契约的覆盖，而不是声称修复了不存在的 UI bug。若失败来自新的用户重构，先精确诊断并更新计划锚点，不凭本计划改接口。若后端拒绝信息被改为其他文案，必须与总集成人员确认后端明确提示再更新 fixture，不将任意 422 都翻译为需重新解析。

- [ ] **Step 4：审查以下生产片段仍保持原样。**

两 Sheet 的成功处理继续是：

```tsx
{ onSuccess: () => onClose() }
```

Edit Sheet 继续保留本地输入并呈现已有错误：

```tsx
const [content, setContent] = useState(segment.content);
```

```tsx
{updateSegment.error ? (
  <p role="alert" className="text-destructive shrink-0 text-[13px]">
    {knowledgeErrorMessage(updateSegment.error, labels.errors)}
  </p>
) : null}
```

Add Sheet 同样保留已有 `useState("")` 和 `createSegment.error` 展示，不复制新的包装函数。没有 `onError` 清空输入、关闭 Sheet、覆盖 `parsing_profile` 或调用 reparse；失败也不能给原分段写乐观“成功”内容。这一步是读代码核验，不要求编辑已经正确的实现。

- [ ] **Step 5：UI-2 审查交付 checkpoint。**

提交测试证据给总集成人员，不执行 git commit。前端 mock 只证明错误可见且输入保留；后端必须另以 A13 测试证明旧 token profile 确实在模型调用、数据库写入前被拒绝，不能让此 mock 代替后端证明。

## 汇总门禁与文档交接

- [ ] **F1：执行本模块完整验证。** 工作目录 `frontend/`：

```bash
pnpm check
pnpm exec rstest run
pnpm test:e2e tests/e2e/project-knowledge.spec.ts --reporter=line
```

Playwright 使用 `playwright.config.ts` 的 production build 与页面 API mocks。需要已有可运行的前端依赖和浏览器；不要安装新模型/解析器来运行这些测试。记录 build/浏览器不可用、无关 dirty-worktree 错误为真实验证边界，不能当成功或目标缺陷证据。

- [ ] **F2：检查精确 diff 与新文件。** 仓库根：

```bash
git diff --check
git diff --stat
git status --short
```

预期：业务变化只在实际生效提示处及两个翻译 key；其余仅测试。不得恢复内部元数据行，不新增处理参数、数据库字段、自动重新解析动作。保留用户所有无关改动。

- [ ] **F3：单一集成人员更新文档。** 本计划提供以下短文案，不直接安排并行编辑：

README 建议：

> 知识库使用固定本地 Knowledge Token 分段；它不等于所选 Embedding 模型的输入 Token，预览不校验模型容量。overlap 是同一合法分组内最多保留的正文 Token，不跨页面、标题或表格边界，实际重叠可能更少。旧 token parent-child 文档在不支持的版本下新增或修改分段会保留未保存输入并提示显式重新解析；重新解析会覆盖原有人工分段编辑和附件绑定。

`frontend/AGENTS.md` 建议补入现有 Knowledge 约束一条：

> New-base upload, existing-base upload, and reparse reuse the same Knowledge Token and bounded-overlap guidance. A processing-profile rejection keeps the unsaved Segment editor open; never auto-reparse or reconstruct parser authority in the browser. Pure guidance changes do not alter processing defaults, payloads, or preview identity.

由总计划负责确认 README 最终文案与后端最终错误/版本行为一致，并合并文档 gate；不把指南写成逐次变更记录。

## 验收映射

| 规格 | 本计划证据 | 不由本计划证明 |
| --- | --- | --- |
| A16 三入口、中英文、单位/预览/overlap | UI-1 文案单测、6 个 mock 浏览器用例 | 实际模型容量安全 |
| 参数/payload/权限/预览不因提示改变 | 精确 preview/upload/reparse payload 断言，现有父子、过期预览及完整 project-knowledge 回归 | 真实后端解析一致性 |
| §9.2 保留旧 token parent-child 未保存编辑、不自动 reparse | UI-2 两语言 × 新增/编辑，真实 422 envelope | 后端零模型请求/零部分写入；由 A13 所有者验证 |
| 重解析明确说明破坏性影响 | 三入口测试断言已有 `reparseWarning` | 自动迁移或部署；本计划不授权 |
| 用户重构和现有优化不被覆盖 | P0 与 F2 的当前源码/dirty diff 审查 | 未执行的其他工作包 |

## 计划自审记录

- UI 工作与 splitter/Adapter/资源锁发布工作解耦；UI-2 消费后端拒绝契约但不假造 HTTP reason 字段。
- 当前共享组件“存在但未接线”的事实已明确，不把用户重构变成本包实施任务；执行前拓扑变化只更新锚点。
- 没有新的产品依赖、helper API、模型配置字段或内部 metadata 面板；测试 helper 均来自同一个现有 E2E 文件。
- 未保存 buffer 与提交时既有 `.trim()` 语义分别断言；不为了保存输入而改变 payload。
- 所有提交/部署均保留显式授权关口；本轮只有计划文件，不代表 UI 已实现或测试已执行通过。
- 编写计划时，将 5 个 TypeScript fenced blocks 在内存中组合到当前 E2E 源文件及两个虚拟单测文件，按当前 tsconfig 做 noEmit 检查：目标文件 0 条诊断、0 个业务/测试文件写入。未运行计划中的新增 rstest/Playwright 用例，此检查不证明浏览器定位或交互已经通过。

import { describe, expect, rs, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { KnowledgeDocumentChunkSettings } from "@/components/projects/knowledge/knowledge-document-chunk-settings";
import { I18nProvider } from "@/core/i18n/context";
import type {
  KnowledgeBaseItem,
  KnowledgeDocumentItem,
} from "@/core/knowledge/types";

const MODEL_ID = "30000000-0000-4000-8000-000000000001";
const RERANKER_ID = "30000000-0000-4000-8000-000000000002";

let documentRows: KnowledgeDocumentItem[] = [];

rs.mock("@/core/knowledge/hooks", () => ({
  useKnowledgeDocuments: () => ({
    data: { items: documentRows },
    error: null,
  }),
  useKnowledgeFileCapabilities: () => ({ data: undefined, error: null }),
  useKnowledgeModelOptions: () => ({
    isLoading: false,
    error: null,
    data: {
      embedding_models: [
        {
          id: MODEL_ID,
          provider_name: "SiliconFlow",
          model_name: "Qwen/Qwen3-VL-Embedding-8B",
          embedding_dimension: 1024,
        },
      ],
      reranker_models: [
        {
          id: RERANKER_ID,
          provider_name: "SiliconFlow",
          model_name: "Qwen/Qwen3-VL-Reranker-8B",
          embedding_dimension: null,
        },
      ],
      summary_model: null,
    },
  }),
  usePreviewKnowledgeDocumentReparse: () => ({ mutateAsync: rs.fn() }),
  useReparseKnowledgeDocument: () => ({
    isPending: false,
    error: null,
    mutate: rs.fn(),
  }),
}));

const base: KnowledgeBaseItem = {
  id: "40000000-0000-4000-8000-000000000001",
  project_id: "10000000-0000-4000-8000-000000000001",
  name: "测试用例文档",
  description: "",
  embedding_model_id: MODEL_ID,
  reranker_model_id: RERANKER_ID,
  retrieval_mode: "hybrid",
  summary_index_enabled: false,
  status: "active",
  document_count: 1,
  default_top_k: 5,
  default_score_threshold: 0,
  default_relative_cutoff: null,
  chunking_mode: "parent_child",
  delete_error: null,
  created_at: "2026-08-29T00:00:00Z",
  updated_at: "2026-08-29T00:00:00Z",
};

const document: KnowledgeDocumentItem = {
  parsing_profile: null,
  parse_warnings: [],
  chunk_size_unit: "token",
  tokenizer_profile_id: "knowledge-tokenizer-v1",
  content_initialized: true,
  id: "50000000-0000-4000-8000-000000000001",
  project_id: base.project_id,
  knowledge_base_id: base.id,
  name: "使用教程",
  original_name: "使用教程.docx",
  media_type: null,
  size_bytes: 2048,
  status: "ready",
  enabled: true,
  version: 3,
  chunk_size: 800,
  chunk_overlap: 80,
  chunk_separator: "\\n\\n",
  remove_extra_spaces: true,
  remove_urls_emails: false,
  chunking_mode: "parent_child",
  child_chunk_size: 300,
  child_chunk_separator: "\\n",
  segment_count: 12,
  word_count: 9000,
  hit_count: 0,
  doc_metadata: {},
  error_message: null,
  delete_error: null,
  task_progress: null,
  created_at: "2026-08-29T00:00:00Z",
  updated_at: "2026-08-29T00:00:00Z",
};

function render(rows: KnowledgeDocumentItem[], documentId = document.id) {
  documentRows = rows;
  return renderToStaticMarkup(
    <QueryClientProvider client={new QueryClient()}>
      <I18nProvider initialLocale="zh-CN">
        <KnowledgeDocumentChunkSettings
          scope={{ accountId: "account-1", projectId: base.project_id }}
          base={base}
          documentId={documentId}
          onExit={() => undefined}
        />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

describe("KnowledgeDocumentChunkSettings", () => {
  test("pre-fills the document's frozen chunk parameters beside the base's saved configuration and preview panel", () => {
    const html = render([document]);

    expect(html).toContain('aria-label="分段设置 · 使用教程"');
    expect(html).toContain("分段设置 · 使用教程");
    expect(html).toContain(">测试用例文档</button>");
    // Current parameters, not wizard defaults.
    expect(html).toContain('value="800"');
    expect(html).toContain('value="80"');
    expect(html).toContain('value="300"');
    expect(html).toMatch(
      /<input type="radio"[^>]*checked=""[^>]*value="parent_child"/u,
    );
    expect(html).not.toMatch(
      /<input type="radio"[^>]*checked=""[^>]*value="general"/u,
    );
    expect(html).toMatch(/<input type="checkbox"[^>]*checked=""/u);
    // The mode belongs to the base: shown as locked, never a per-document switch.
    expect(html).toContain("分段模式已锁定为「父子分段」");
    expect(html).toMatch(
      /<input type="radio"[^>]*disabled=""[^>]*value="general"/u,
    );
    expect(html).toMatch(
      /<input type="radio"[^>]*disabled=""[^>]*value="parent_child"/u,
    );
    expect(html).toContain("已填入该文档当前的分段参数");
    expect(html).toContain("全量替换分段");
    expect(html).not.toContain("历史字符单位");
    // The base's models are read-only context here.
    expect(html).toContain("沿用知识库的模型与检索配置");
    expect(html).toContain("SiliconFlow · Qwen/Qwen3-VL-Embedding-8B");
    expect(html).toContain("SiliconFlow · Qwen/Qwen3-VL-Reranker-8B");
    expect(html).toContain("混合检索");
    // Preview panel names the stored original file; nothing is fabricated
    // before the server responds.
    expect(html).toContain("分段预览");
    expect(html).toContain("预览文件：使用教程.docx");
    expect(html).not.toContain("Chunk-");
    expect(html).toContain(">确认重新解析</button>");
  });

  test("pre-fills the base's mode for a document still on the previous one", () => {
    // Admitted before a base-wide switch: confirming must bring it in line
    // with the base, not re-freeze the stale mode.
    const html = render([{ ...document, chunking_mode: "general" }]);

    expect(html).toContain("分段模式已锁定为「父子分段」");
    expect(html).toMatch(
      /<input type="radio"[^>]*checked=""[^>]*value="parent_child"/u,
    );
    expect(html).not.toMatch(
      /<input type="radio"[^>]*checked=""[^>]*value="general"/u,
    );
  });

  test("warns when a historical character-unit document will switch to Knowledge Tokens", () => {
    const html = render([{ ...document, chunk_size_unit: "character" }]);

    expect(html).toContain("历史字符单位");
  });

  test("reports an inaccessible document instead of rendering a cached form", () => {
    const html = render([], document.id);

    expect(html).toContain("该文档不存在或不可访问");
    expect(html).toContain(">返回文档列表</button>");
    expect(html).not.toContain('value="800"');
  });
});

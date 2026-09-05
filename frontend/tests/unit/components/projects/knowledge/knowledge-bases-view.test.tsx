import { describe, expect, rs, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { KnowledgeBasesView } from "@/components/projects/knowledge/knowledge-bases-view";
import { I18nProvider } from "@/core/i18n/context";
import type { KnowledgeBaseItem } from "@/core/knowledge/types";

let currentBase: KnowledgeBaseItem;

rs.mock("@/core/knowledge/hooks", () => ({
  useKnowledgeBases: () => ({ data: { items: [currentBase] } }),
  useCreateKnowledgeBase: () => ({ isPending: false }),
  useDeleteKnowledgeBase: () => ({ isPending: false }),
}));

const base: KnowledgeBaseItem = {
  id: "40000000-0000-4000-8000-000000000001",
  project_id: "10000000-0000-4000-8000-000000000001",
  name: "产品手册",
  description: "用于回答产品使用和配置问题。",
  embedding_model_id: "30000000-0000-4000-8000-000000000001",
  reranker_model_id: null,
  retrieval_mode: "hybrid",
  summary_index_enabled: false,
  status: "active",
  document_count: 12,
  default_top_k: 5,
  default_score_threshold: 0,
  default_relative_cutoff: null,
  chunking_mode: "general",
  delete_error: null,
  created_at: "2026-08-29T00:00:00Z",
  updated_at: new Date(Date.now() - 4 * 60 * 60 * 1000).toISOString(),
};

function render(item: KnowledgeBaseItem, canEdit = true) {
  currentBase = item;
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <KnowledgeBasesView
        scope={{ accountId: "account-1", projectId: base.project_id }}
        canEdit={canEdit}
        createOpen={false}
        onCreateOpenChange={() => undefined}
        onStartWizard={() => undefined}
        onOpenBase={() => undefined}
      />
    </I18nProvider>,
  );
}

describe("KnowledgeBasesView cards", () => {
  test("shows the saved retrieval mode, document count and relative update time", () => {
    const html = render(base);

    expect(html).toContain(base.name);
    expect(html).toContain(base.description);
    expect(html).toContain("混合检索");
    expect(html).toContain("12 篇文档");
    expect(html).toMatch(/更新于[^<]*4[^<]*小时[^<]*前/u);
    expect(html).toContain('aria-label="查看文档"');
    expect(html).toContain(">删除</button>");
  });

  test("keeps unconfigured and deletion-error states without inventing retrieval readiness", () => {
    const html = render(
      { ...base, embedding_model_id: null, delete_error: "对象存储暂不可用" },
      false,
    );

    expect(html).toContain("待配置");
    expect(html).toContain("删除失败：对象存储暂不可用");
    expect(html).not.toContain("混合检索");
    expect(html).not.toContain(">删除</button>");
    expect(html).toContain('aria-label="查看文档"');
  });
});

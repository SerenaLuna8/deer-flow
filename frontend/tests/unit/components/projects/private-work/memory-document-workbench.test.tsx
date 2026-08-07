import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  MemoryDocumentWorkbench,
  type MemoryDocumentWorkbenchProps,
} from "@/components/projects/private-work/memory/memory-document-workbench";
import { I18nProvider } from "@/core/i18n/context";

const TIMESTAMP = "2026-08-05T00:00:00Z";

function props(): MemoryDocumentWorkbenchProps {
  return {
    document: {
      data: {
        content: "# 关于我\n\n用户希望执行计划可直接落地。",
        version: 4,
        updatedAt: TIMESTAMP,
        pendingCount: 3,
        dreamRunning: false,
        injectionStatus: "ok",
      },
      isLoading: false,
      error: null,
      retry: () => undefined,
    },
    versions: {
      data: [
        {
          version: 4,
          trigger: "manual_dream",
          historyCount: 3,
          changed: true,
          needsReview: false,
          createdAt: TIMESTAMP,
        },
        {
          version: 3,
          trigger: "restore",
          historyCount: null,
          changed: false,
          needsReview: false,
          createdAt: TIMESTAMP,
        },
      ],
      isLoading: false,
      error: null,
      retry: () => undefined,
      page: 0,
      hasNext: false,
      previous: () => undefined,
      next: () => undefined,
    },
    detail: {
      selectedVersion: null,
      data: undefined,
      isLoading: false,
      error: null,
      retry: () => undefined,
      select: () => undefined,
    },
    episodes: {
      items: [
        {
          id: "44444444-4444-4444-8444-444444444444",
          threadId: "thread-1",
          origin: "snip",
          taggedText: "- [durable] 部署目标是 region-eu",
          occurredAt: TIMESTAMP,
          createdAt: TIMESTAMP,
        },
        {
          id: "55555555-5555-4555-8555-555555555555",
          threadId: "thread-2",
          origin: "tool",
          taggedText: "- [permanent] 用户偏好中文回复",
          occurredAt: TIMESTAMP,
          createdAt: TIMESTAMP,
        },
      ],
      isLoading: false,
      error: null,
      retry: () => undefined,
      searchInput: "",
      setSearchInput: () => undefined,
      submitSearch: () => undefined,
      activeQuery: null,
      tags: [],
      toggleTag: () => undefined,
      hasMore: false,
      loadMore: () => undefined,
      loadingMore: false,
    },
    pending: {
      items: [
        {
          sequence: 41,
          origin: "tool",
          taggedText: "- [durable] 部署目标是 region-eu",
          createdAt: TIMESTAMP,
        },
        {
          sequence: 42,
          origin: "snip",
          taggedText: "- [ephemeral] 正在排查导入抖动",
          createdAt: TIMESTAMP,
        },
      ],
      isLoading: false,
      error: null,
      retry: () => undefined,
    },
    actions: {
      canDream: true,
      canRestore: true,
      dreaming: false,
      restoringVersion: null,
      dream: async () => undefined,
      restore: async () => undefined,
    },
  };
}

function render(transform?: (value: MemoryDocumentWorkbenchProps) => void) {
  const value = props();
  transform?.(value);
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <MemoryDocumentWorkbench {...value} />
    </I18nProvider>,
  );
}

describe("Memory document workbench", () => {
  test("shows only the current document, pending work and real version history", () => {
    const html = render();

    expect(html).toContain("用户希望执行计划可直接落地");
    expect(html).toContain("待整理");
    expect(html).toContain("3 条");
    expect(html).toContain("立即整理");
    expect(html).toContain("整理记录");
    expect(html).toContain("手动整理");
    expect(html).toContain("版本恢复");
    expect(html).toContain("有内容变化");
    expect(html).toContain("内容未变化");
  });

  test("renders a clear first-use document state", () => {
    const html = render((value) => {
      value.document.data = {
        content: "",
        version: 0,
        updatedAt: null,
        pendingCount: 0,
        dreamRunning: false,
        injectionStatus: "ok",
      };
      value.versions.data = [];
    });

    expect(html).toContain("还没有长期记忆");
    expect(html).toContain("还没有整理记录");
    expect(html).toMatch(/<button[^>]*disabled[^>]*>[^<]*<svg[\s\S]*立即整理/u);
  });

  test("shows active Dream state without offering a second admission", () => {
    const html = render((value) => {
      value.document.data!.dreamRunning = true;
    });

    expect(html).toContain("已有整理任务运行中");
    expect(html).toMatch(/<button[^>]*disabled/u);
  });

  test("keeps Dream hidden behind server-issued capabilities", () => {
    const html = render((value) => {
      value.actions.canDream = false;
    });

    expect(html).toContain("你当前没有运行整理任务的权限");
    expect(html).toMatch(/<button[^>]*disabled/u);
  });

  test("keeps pagination recoverable on a later empty page", () => {
    const html = render((value) => {
      value.versions.data = [];
      value.versions.page = 1;
    });

    expect(html).toContain("上一页");
  });

  test("offers the archive tab beside organization history", () => {
    const html = render();

    expect(html).toContain("历史归档");
    expect(html).toContain("整理记录");
  });

  test("renders searchable archived episodes with origin and tags", () => {
    const html = render((value) => {
      value.initialTab = "archive";
    });

    expect(html).toContain("搜索归档记忆");
    expect(html).toContain("部署目标是 region-eu");
    expect(html).toContain("自动摘要");
    expect(html).toContain("主动记忆");
    expect(html).toContain("永久");
    expect(html).toContain("持久");
    expect(html).toContain("短期");
    expect(html).toContain("更正");
    // Browse mode with a full page keeps loading bounded via an explicit action.
    expect(html).not.toContain("加载更多");
  });

  test("surfaces the pending backlog with origins before the tabs", () => {
    const html = render();

    expect(html).toContain("待整理内容");
    expect(html).toContain("正在排查导入抖动");
    expect(html).toContain("主动记忆");
    expect(html).toContain("自动摘要");
    expect(html).toContain('id="memory-pending"');
  });

  test("hides the pending backlog when it is empty", () => {
    const html = render((value) => {
      value.pending.items = [];
    });

    expect(html).not.toContain("待整理内容");
    expect(html).not.toContain('id="memory-pending"');
  });

  test("keeps the pending backlog recoverable after a load failure", () => {
    const html = render((value) => {
      value.pending.items = [];
      value.pending.error = new Error("boom");
    });

    expect(html).toContain("无法加载待整理内容");
  });

  test("degrades over-budget documents to a banner with a compression action", () => {
    const html = render((value) => {
      value.document.data!.injectionStatus = "skipped_over_budget";
      value.document.data!.pendingCount = 0;
    });

    expect(html).toContain("记忆文档超出注入预算");
    expect(html).toContain("立即压缩文档");
    // Both Dream entry points must stay enabled for the budget rescue even
    // with zero pending items. Static markup renders a disabled button as an
    // actual `disabled=""` attribute (class names also contain "disabled:").
    expect(html).not.toMatch(
      /<button[^>]*\sdisabled=""[^>]*>(?:(?!<\/button>)[\s\S])*?(?:立即压缩文档|立即整理)/u,
    );
  });

  test("hides the over-budget banner when injection is healthy", () => {
    const html = render();

    expect(html).not.toContain("记忆文档超出注入预算");
  });

  test("marks large-deletion versions with a review badge", () => {
    const html = render((value) => {
      value.versions.data = [
        {
          version: 5,
          trigger: "budget_rewrite",
          historyCount: null,
          changed: true,
          needsReview: true,
          createdAt: TIMESTAMP,
        },
      ];
    });

    expect(html).toContain("预算压缩");
    expect(html).toContain("建议复核");
  });

  test("distinguishes archive empty state from a search without matches", () => {
    const empty = render((value) => {
      value.initialTab = "archive";
      value.episodes.items = [];
    });
    const noMatch = render((value) => {
      value.initialTab = "archive";
      value.episodes.items = [];
      value.episodes.activeQuery = "deployment";
    });
    const more = render((value) => {
      value.initialTab = "archive";
      value.episodes.hasMore = true;
    });

    expect(empty).toContain("还没有归档的记忆条目");
    expect(noMatch).toContain("没有找到匹配的归档记忆");
    expect(more).toContain("加载更多");
  });
});

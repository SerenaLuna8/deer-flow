import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  MemoryDocumentWorkbench,
  type MemoryDocumentWorkbenchProps,
} from "@/components/projects/private-work/memory/memory-document-workbench";
import { I18nProvider } from "@/core/i18n/context";

const TIMESTAMP = "2026-08-05T00:00:00Z";

function props(): MemoryDocumentWorkbenchProps {
  const latestVersion = {
    version: 4,
    trigger: "manual_dream" as const,
    historyCount: 3,
    changed: true,
    needsReview: false,
    createdAt: TIMESTAMP,
  };
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
      data: [latestVersion],
      latest: latestVersion,
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
  test("offers current memory and archive tabs with the document frame", () => {
    const html = render();

    expect(html).toContain("当前记忆");
    expect(html).toContain("历史归档");
    expect(html).toContain("MEMORY.md");
    expect(html).toContain("源码");
    expect(html).toContain("预览");
    expect(html).toContain("查看变化");
    expect(html).toContain("版本历史");
    expect(html).toContain("版本 4");
    expect(html).toContain("用户希望执行计划可直接落地");
    expect(html).toContain('data-slot="memory-document-frame"');
    expect(html).toContain("待整理");
    expect(html).toContain("立即整理");
    expect(html).not.toContain("整理记录");
  });

  test("renders archived episodes on the archive tab", () => {
    const html = render((value) => {
      value.activeTab = "archive";
    });

    expect(html).toContain("搜索归档记忆");
    expect(html).toContain("部署目标是 region-eu");
    expect(html).toContain("自动摘要");
    expect(html).toContain("主动记忆");
    expect(html).toContain("永久");
    expect(html).toContain("持久");
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
      value.versions.latest = null;
    });

    expect(html).toContain("还没有长期记忆");
    expect(html).not.toContain("查看变化");
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

  test("surfaces the pending backlog with origins", () => {
    const html = render();

    expect(html).toContain("待整理内容");
    expect(html).toContain("正在排查导入抖动");
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
    expect(html).not.toMatch(
      /<button[^>]*\sdisabled=""[^>]*>(?:(?!<\/button>)[\s\S])*?(?:立即压缩文档|立即整理)/u,
    );
  });

  test("surfaces versions that need review in both the banner and history", () => {
    const html = render((value) => {
      value.versions.data![0]!.needsReview = true;
    });

    expect(html).toContain("最新版本建议复核");
    expect(html).toContain("查看需复核版本");
    expect(html).toContain("建议复核");
  });

  test("keeps version history pagination and load failures recoverable", () => {
    const paged = render((value) => {
      value.versions.page = 1;
      value.versions.hasNext = true;
    });
    const failed = render((value) => {
      value.versions.data = [];
      value.versions.error = new Error("boom");
    });

    expect(paged).toContain("上一页");
    expect(paged).toContain("下一页");
    expect(failed).toContain("无法加载版本历史");
  });

  test("does not promote an older page item to the latest review banner", () => {
    const html = render((value) => {
      value.versions.page = 1;
      value.versions.data = [
        {
          version: 3,
          trigger: "auto_dream",
          historyCount: 2,
          changed: true,
          needsReview: true,
          createdAt: TIMESTAMP,
        },
      ];
    });

    expect(html).toContain("建议复核");
    expect(html).not.toContain("最新版本建议复核");
  });

  test("distinguishes archive empty state from a search without matches", () => {
    const empty = render((value) => {
      value.activeTab = "archive";
      value.episodes.items = [];
    });
    const noMatch = render((value) => {
      value.activeTab = "archive";
      value.episodes.items = [];
      value.episodes.activeQuery = "deployment";
    });
    const more = render((value) => {
      value.activeTab = "archive";
      value.episodes.hasMore = true;
    });

    expect(empty).toContain("还没有归档的记忆条目");
    expect(noMatch).toContain("没有找到匹配的归档记忆");
    expect(more).toContain("加载更多");
  });
});

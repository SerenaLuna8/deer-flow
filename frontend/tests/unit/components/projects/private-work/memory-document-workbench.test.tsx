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
          createdAt: TIMESTAMP,
        },
        {
          version: 3,
          trigger: "restore",
          historyCount: null,
          changed: false,
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
});

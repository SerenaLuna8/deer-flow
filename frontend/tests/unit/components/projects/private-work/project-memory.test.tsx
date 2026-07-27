import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import type { MemorySectionGroup } from "@/components/workspace/settings/memory/memory-view-model";
import {
  MemoryContextSidecar,
  MemoryFactList,
  MemoryStatusBar,
} from "@/components/workspace/settings/memory/memory-workbench";
import { enUS } from "@/core/i18n/locales/en-US";

describe("project Memory page", () => {
  test("injects a project source href and prefers sourceThreadId", () => {
    const html = renderToStaticMarkup(
      <MemoryFactList
        facts={[
          {
            id: "fact-1",
            content: "Project provenance",
            category: "project",
            confidence: 0.9,
            createdAt: "2026-07-15T00:00:00Z",
            source: "legacy-source",
            sourceThreadId: "thread/source",
            sourceRunId: "run-source",
          },
        ]}
        t={enUS}
        isDeleting={false}
        sourceThreadHref={(fact) =>
          `/projects/research-lab/chats/${encodeURIComponent(
            fact.sourceThreadId ?? fact.source,
          )}`
        }
      />,
    );

    expect(html).toContain(
      'href="/projects/research-lab/chats/thread%2Fsource"',
    );
    expect(html).not.toContain("/workspace/chats/");
  });

  test("renders the compact Memory status row from the existing snapshot counts", () => {
    const html = renderToStaticMarkup(
      <MemoryStatusBar
        t={enUS}
        factCount={5}
        summaryCount={2}
        lastUpdated="about 1 hour ago"
      />,
    );

    expect(html).toContain('data-testid="memory-status-bar"');
    expect(html).toContain("5 facts");
    expect(html).toContain("2 summaries");
    expect(html).toContain("Last updated");
    expect(html).toContain("about 1 hour ago");
  });

  test("keeps top-of-mind and read-only summary previews in a supporting sidecar", () => {
    const groups: MemorySectionGroup[] = [
      {
        title: "User context",
        sections: [
          {
            title: "Work",
            summary: "Building a project Memory redesign.",
            updatedAt: "2026-07-15T00:00:00Z",
          },
          {
            title: "Personal",
            summary: "",
            updatedAt: "",
          },
          {
            title: "Top of mind",
            summary: "Testing subtask execution.",
            updatedAt: "2026-07-15T00:00:00Z",
          },
        ],
      },
      {
        title: "History and background",
        sections: [
          {
            title: "Recent months",
            summary: "Defined the current execution boundaries.",
            updatedAt: "2026-07-15T00:00:00Z",
          },
          {
            title: "Earlier context",
            summary: "",
            updatedAt: "",
          },
          {
            title: "Long-term background",
            summary: "",
            updatedAt: "",
          },
        ],
      },
    ];

    const html = renderToStaticMarkup(
      <MemoryContextSidecar
        t={enUS}
        recentFocus="Testing subtask execution."
        groups={groups}
        summaryCount={3}
        onViewSummaries={() => undefined}
      />,
    );

    expect(html).toContain('data-testid="memory-context-sidecar"');
    expect(html).toContain("Recent focus");
    expect(html).toContain("Testing subtask execution.");
    expect(html.match(/Testing subtask execution\./g)).toHaveLength(1);
    expect(html).toContain("Smart summaries");
    expect(html).toContain("Read only");
    expect(html).toContain("Work");
    expect(html).toContain("Recent months");
    expect(html).not.toContain(">Personal<");
  });
});

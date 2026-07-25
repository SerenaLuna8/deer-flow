import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { MemoryFactList } from "@/components/workspace/settings/memory/memory-workbench";
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
});

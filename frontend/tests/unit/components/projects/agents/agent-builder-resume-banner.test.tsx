import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentBuilderResumeBannerView } from "@/components/projects/agents/agent-builder-resume-banner";

describe("AgentBuilderResumeBannerView", () => {
  test("offers a separate delete action for every unfinished Agent", () => {
    const html = renderToStaticMarkup(
      <AgentBuilderResumeBannerView
        projectSlug="default-project"
        sessions={[
          {
            id: "00000000-0000-4000-8000-000000000010",
            slug: "code-reviewer",
            display_name: "code-reviewer",
            status: "interviewing",
            revision: 7,
            updated_at: "2026-08-09T06:00:00Z",
          },
        ]}
        onDelete={async () => undefined}
      />,
    );

    expect(html).toContain("继续设计未完成的 Agent");
    expect(html).toContain("code-reviewer");
    expect(html).toContain("删除未完成的 Agent：code-reviewer");
    expect(html).toContain(
      "/projects/default-project/agents/new/00000000-0000-4000-8000-000000000010",
    );
  });
});

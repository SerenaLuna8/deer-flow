import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SkillBuilderResumeBannerView } from "@/components/projects/skills/skill-builder-resume-banner";
import type { SkillBuilderSessionSummary } from "@/core/skill-builder";

const SESSION_ID = "11111111-1111-4111-8111-111111111111";

function session(
  overrides: Partial<SkillBuilderSessionSummary> = {},
): SkillBuilderSessionSummary {
  return {
    id: SESSION_ID,
    slug: "transmission-channel-rquery",
    display_name: "transmission-channel-rquery",
    status: "interviewing",
    revision: 3,
    updated_at: "2026-08-13T09:11:00Z",
    ...overrides,
  };
}

describe("SkillBuilderResumeBannerView", () => {
  test("shows a delete control for unfinished sessions", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderResumeBannerView
        projectSlug="demo"
        sessions={[session()]}
        onDelete={async () => undefined}
      />,
    );

    expect(html).toContain("继续创建未完成的 Skill");
    expect(html).toContain("transmission-channel-rquery");
    expect(html).toContain(
      'aria-label="删除未完成的 Skill：transmission-channel-rquery"',
    );
    expect(html).toContain(`/projects/demo/skills/new/${SESSION_ID}`);
  });

  test("hides completed and cancelled sessions", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderResumeBannerView
        projectSlug="demo"
        sessions={[
          session({ status: "completed" }),
          session({
            id: "22222222-2222-4222-8222-222222222222",
            status: "cancelled",
          }),
        ]}
        onDelete={async () => undefined}
      />,
    );

    expect(html).toBe("");
  });
});

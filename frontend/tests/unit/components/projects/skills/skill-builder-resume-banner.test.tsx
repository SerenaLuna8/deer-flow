import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SkillBuilderResumeBannerView } from "@/components/projects/skills/skill-builder-resume-banner";
import { I18nProvider } from "@/core/i18n/context";
import type { SkillBuilderSessionSummary } from "@/core/skill-builder";

function renderUi(node: React.ReactNode) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">{node}</I18nProvider>,
  );
}

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
    session_kind: "create",
    ...overrides,
  };
}

describe("SkillBuilderResumeBannerView", () => {
  test("shows a delete control for unfinished sessions", () => {
    const html = renderUi(
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

  test("labels revision sessions separately from create sessions", () => {
    const html = renderUi(
      <SkillBuilderResumeBannerView
        projectSlug="demo"
        sessions={[
          session({ session_kind: "revise", display_name: "catalog-auditor" }),
        ]}
        onDelete={async () => undefined}
      />,
    );

    expect(html).toContain("继续未完成的修订");
    expect(html).toContain("修订 ·");
    expect(html).toContain('aria-label="删除未完成的修订：catalog-auditor"');
  });

  test("hides completed and cancelled sessions", () => {
    const html = renderUi(
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

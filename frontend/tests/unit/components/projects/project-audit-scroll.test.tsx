import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectAuditStateView } from "@/components/projects/governance/project-audit-page";
import { I18nProvider } from "@/core/i18n/context";
import { auditItemSchema } from "@/core/project-governance/audit";

describe("ProjectAuditStateView scrolling", () => {
  test("uses the route scroller instead of nested viewport-height scrollers", () => {
    const item = auditItemSchema.parse({
      id: "11111111-1111-4111-8111-111111111111",
      occurred_at: "2026-08-22T08:00:00+08:00",
      actor: "user",
      action: "project.updated",
      target_kind: "project",
      outcome: "success",
      public_error_code: null,
      metadata: {},
    });
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <ProjectAuditStateView
          state={{
            status: "ready",
            data: { items: [item], next_cursor: null },
          }}
        />
      </I18nProvider>,
    );

    expect(html).not.toContain("max-h-[calc(100svh-11rem)]");
    expect(html).not.toContain("overflow-y-auto");
    expect(html).toContain("md:hidden");
    expect(html).toContain("md:block");
  });
});

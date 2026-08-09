import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectAuditStateView } from "@/components/projects/governance/project-audit-page";
import { I18nProvider } from "@/core/i18n/context";
import type { ProjectAuditPage } from "@/core/project-governance/audit";

const auditPage: ProjectAuditPage = {
  items: [
    {
      id: "11111111-1111-4111-8111-111111111111",
      occurred_at: "2026-08-07T11:26:00+00:00",
      actor: "user",
      action: "asset.updated",
      target_kind: "asset",
      outcome: "success",
      public_error_code: null,
      metadata: { asset_kind: "agent" },
    },
  ],
  next_cursor: null,
};

describe("ProjectAuditStateView", () => {
  test("keeps the audit table in a viewport-bounded scroll container", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <ProjectAuditStateView state={{ status: "ready", data: auditPage }} />
      </I18nProvider>,
    );

    expect(html).toContain('data-slot="admin-data-table-container"');
    expect(html).toContain("max-h-[calc(100svh-11rem)]");
    expect(html).toContain("overflow-auto");
    expect(html).toContain("sticky top-0");
  });
});

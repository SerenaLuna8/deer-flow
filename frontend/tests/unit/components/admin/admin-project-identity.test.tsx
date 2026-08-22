import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AdminProjectIdentitySummary } from "@/components/admin/assets/admin-project-assets-shell";
import { adminProjectOptionLabel } from "@/components/admin/operations/admin-operations-ui";
import type { AdminProjectPage } from "@/core/admin-operations/types";
import { I18nProvider } from "@/core/i18n/context";

const project: AdminProjectPage["items"][number] = {
  project_id: "11111111-1111-4111-8111-111111111111",
  slug: "replay-alpha",
  display_name: "Replay project",
  status: "active",
  is_suspended: false,
  state_version: 1,
  created_at: "2026-08-22T00:00:00Z",
  updated_at: "2026-08-22T00:00:00Z",
  deletion_effective_at: null,
};

describe("admin project identity", () => {
  test("distinguishes same-name projects in selector options", () => {
    expect(adminProjectOptionLabel(project)).toBe(
      "Replay project · replay-alpha · 11111111",
    );
  });

  test("shows the project name, slug, and short UUID in governance context", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <AdminProjectIdentitySummary project={project} />
      </I18nProvider>,
    );

    expect(html).toContain("Replay project");
    expect(html).toContain("replay-alpha");
    expect(html).toContain("11111111");
    expect(html).toContain('title="11111111-1111-4111-8111-111111111111"');
  });
});

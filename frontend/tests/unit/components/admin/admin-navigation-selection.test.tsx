import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AdminOperationsNavigation } from "@/components/admin/operations/admin-operations-shell";

describe("AdminOperationsNavigation", () => {
  test("shows knowledge settings as its own current destination", () => {
    const html = renderToStaticMarkup(
      <AdminOperationsNavigation pathname="/admin/settings/knowledge" />,
    );
    expect(html).toMatch(/href="\/admin\/settings\/knowledge"/u);
    expect(html).toContain("Knowledge settings");
    expect(html).toContain('aria-current="page"');
  });

  test("hides knowledge navigation when the client administrator gate closes", () => {
    const html = renderToStaticMarkup(
      <AdminOperationsNavigation
        pathname="/admin/settings/knowledge"
        showKnowledgeSettings={false}
      />,
    );
    expect(html).not.toContain("/admin/settings/knowledge");
  });

  test("marks platform configuration as the current destination", () => {
    const html = renderToStaticMarkup(
      <AdminOperationsNavigation pathname="/admin/settings/system" />,
    );

    expect(html).toContain("Platform configuration");
    expect(html).toContain('aria-current="page"');
  });
});

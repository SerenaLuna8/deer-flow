import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AdminOperationsNavigation } from "@/components/admin/operations/admin-operations-shell";

describe("AdminOperationsNavigation", () => {
  test("marks platform configuration as the current destination", () => {
    const html = renderToStaticMarkup(
      <AdminOperationsNavigation pathname="/admin/settings/system" />,
    );

    expect(html).toContain("Platform configuration");
    expect(html).toContain('aria-current="page"');
  });
});

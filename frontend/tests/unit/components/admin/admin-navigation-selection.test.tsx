import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AdminOperationsNavigation } from "@/components/admin/operations/admin-operations-shell";

describe("AdminOperationsNavigation", () => {
  test("uses the blue selected state for platform configuration", () => {
    const html = renderToStaticMarkup(
      <AdminOperationsNavigation pathname="/admin/settings/system" />,
    );

    expect(html).toContain("Platform configuration");
    expect(html).toContain('aria-current="page"');
    expect(html).toContain("bg-blue-50 text-blue-600 before:bg-blue-600");
    expect(html).toContain("hover:bg-blue-50");
    expect(html).not.toContain("hover:text-blue-600");
  });
});

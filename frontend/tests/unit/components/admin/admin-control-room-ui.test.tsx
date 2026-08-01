import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AdminPage,
  AdminPageHeader,
  AdminSection,
} from "@/components/admin/ui/admin-page";

describe("admin control room composition", () => {
  test("provides semantic page, header, action, metadata, and section regions", () => {
    const html = renderToStaticMarkup(
      <AdminPage>
        <AdminPageHeader
          eyebrow="Control room"
          title="Platform overview"
          description="Inspect platform health."
          actions={<button type="button">Refresh</button>}
          meta={<span>Updated now</span>}
        />
        <AdminSection
          title="Readiness"
          description="Live component health."
          actions={<button type="button">Inspect</button>}
        >
          <p>All systems reported.</p>
        </AdminSection>
      </AdminPage>,
    );

    expect(html).toContain("<main");
    expect(html).toContain("<h1");
    expect(html).toContain("Control room");
    expect(html).toContain("Platform overview");
    expect(html).toContain("Inspect platform health.");
    expect(html).toContain("Refresh");
    expect(html).toContain("Updated now");
    expect(html).toContain("<section");
    expect(html).toContain("Readiness");
    expect(html).toContain("Live component health.");
    expect(html).toContain("Inspect");
    expect(html).toContain("All systems reported.");
  });
});

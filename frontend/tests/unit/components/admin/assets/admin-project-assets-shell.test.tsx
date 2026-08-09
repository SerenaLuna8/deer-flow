import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  usePathname: () =>
    "/admin/projects/11111111-1111-4111-8111-111111111111/assets/quotas",
}));

import { AdminProjectAssetsShell } from "@/components/admin/assets/admin-project-assets-shell";
import { I18nProvider } from "@/core/i18n/context";

describe("AdminProjectAssetsShell", () => {
  test("exposes the project quota tab beside asset kinds", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AdminProjectAssetsShell projectId="11111111-1111-4111-8111-111111111111">
          <p>Quota content</p>
        </AdminProjectAssetsShell>
      </I18nProvider>,
    );

    expect(html).toContain(">配额</span>");
    expect(html).toContain(
      'href="/admin/projects/11111111-1111-4111-8111-111111111111/assets/quotas"',
    );
    expect(html).toMatch(
      /aria-current="page"[^>]*href="\/admin\/projects\/11111111-1111-4111-8111-111111111111\/assets\/quotas"/u,
    );
  });
});

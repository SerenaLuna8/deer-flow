import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/core/auth/AuthProvider", () => ({
  useAuth: () => ({
    user: {
      id: "11111111-1111-4111-8111-111111111111",
      system_role: "system_admin",
    },
  }),
}));

rs.mock("@/core/shared-assets", () => ({
  useAdminAssets: () => ({
    data: {
      items: [
        {
          id: "22222222-2222-4222-8222-222222222222",
          slug: "replay-agent",
          display_name: "Replay Agent",
          status: "active",
          revision: 1,
          current_version_id: null,
        },
      ],
    },
    isLoading: false,
    error: null,
  }),
  useAdminAssetVersions: () => ({
    data: { data: [] },
    isLoading: false,
    error: null,
  }),
}));

import { AdminAssetPage } from "@/components/admin/assets/admin-asset-page";
import { I18nProvider } from "@/core/i18n/context";

describe("admin asset catalog locale", () => {
  test("renders catalog controls and actions in English", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <AdminAssetPage kind="agents" />
      </I18nProvider>,
    );

    for (const text of [
      "System Agent",
      "Asset catalog",
      "Search by name or identifier",
      "All statuses",
      "View details",
    ]) {
      expect(html).toContain(text);
    }
    expect(html).not.toMatch(/\p{Script=Han}/u);
  });
});

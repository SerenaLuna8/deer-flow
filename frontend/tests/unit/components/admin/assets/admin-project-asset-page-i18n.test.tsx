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
  useAdminProjectAssets: () => ({
    data: { request_id: "request", system_items: [], project_items: [] },
    isLoading: false,
    error: null,
  }),
  useAdminProjectAssetVersions: () => ({ data: { data: [] } }),
  useChangeAdminProjectAssetStatus: () => ({
    error: null,
    isPending: false,
    mutate: () => undefined,
  }),
  useCreateAdminProjectAssetVersion: () => ({
    error: null,
    isPending: false,
    mutate: () => undefined,
  }),
  usePublishAdminProjectMcpVersion: () => ({
    error: null,
    isPending: false,
    mutate: () => undefined,
  }),
}));

import { createVersionDialogCopy } from "@/components/admin/assets/admin-asset-dialogs";
import { AdminProjectAssetPage } from "@/components/admin/assets/admin-project-asset-page";
import { I18nProvider } from "@/core/i18n/context";
import { enUS } from "@/core/i18n/locales";

describe("admin project asset governance locale", () => {
  test("renders the project catalog in English", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <AdminProjectAssetPage
          projectId="22222222-2222-4222-8222-222222222222"
          kind="agents"
        />
      </I18nProvider>,
    );

    for (const text of [
      "Project Agents",
      "Project assets",
      "Search by name or identifier",
      "System assets",
      "No visible system assets",
      "This project has no assets of this type",
    ]) {
      expect(html).toContain(text);
    }
    expect(html).not.toMatch(/\p{Script=Han}/u);
  });

  test("renders MCP version authoring in English", () => {
    const copy = createVersionDialogCopy(enUS, "mcp-servers", "Replay MCP");
    const renderedCopy = Object.values(copy).join(" ");

    expect(copy.title).toBe("Create a version for Replay MCP");
    expect(copy.secretSlots).toBe("Secret slots JSON");
    expect(copy.save).toBe("Save version");
    expect(renderedCopy).not.toMatch(/\p{Script=Han}/u);
  });
});

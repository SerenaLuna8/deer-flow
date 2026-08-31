import { describe, expect, rs, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AdminKnowledgeSettingsPage } from "@/components/admin/settings/admin-knowledge-settings-page";

const guard = rs.hoisted(() => ({
  user: null as { system_role: "user" } | null,
  settings: rs.fn(),
  models: rs.fn(),
}));
rs.mock("@/core/auth/AuthProvider", () => ({
  useAuth: () => ({ user: guard.user }),
}));
rs.mock("@/core/admin-settings/knowledge/hooks", () => ({
  useAdminKnowledgeSettings: guard.settings,
  useSaveAdminKnowledgeSettings: rs.fn(),
}));
rs.mock("@/core/models/hooks", () => ({ useModels: guard.models }));

describe("knowledge settings administrator boundary", () => {
  test("does not construct settings or model queries before identity is known", () => {
    guard.user = null;
    expect(renderToStaticMarkup(<AdminKnowledgeSettingsPage />)).toBe("");
    expect(guard.settings).not.toHaveBeenCalled();
    expect(guard.models).not.toHaveBeenCalled();
  });

  test("does not render or query for an authenticated non-administrator", () => {
    guard.user = { system_role: "user" };
    expect(renderToStaticMarkup(<AdminKnowledgeSettingsPage />)).toBe("");
    expect(guard.settings).not.toHaveBeenCalled();
    expect(guard.models).not.toHaveBeenCalled();
  });
});

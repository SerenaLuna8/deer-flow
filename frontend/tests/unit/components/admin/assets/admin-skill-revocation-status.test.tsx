import { describe, expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import {
  adminAssetVersionStatus,
  VersionTimeline,
} from "@/components/admin/assets/admin-asset-page";
import { I18nProvider } from "@/core/i18n/context";
import type { AssetVersion } from "@/core/shared-assets";

const revokedVersion: Extract<AssetVersion, { skill_id: string }> = {
  id: "11111111-1111-4111-8111-111111111111",
  skill_id: "22222222-2222-4222-8222-222222222222",
  version_number: 3,
  relation: "current",
  description: "Revoked version",
  frontmatter: {},
  compatibility: null,
  secret_requirements: [],
  scan_decision: "allow",
  scan_rule_ids: [],
  scan_summary: {},
  file_views: [],
  supersedes_version_id: null,
  payload_checksum: "a".repeat(64),
  revoked_at: "2026-08-20T00:00:00Z",
  revoked_by_user_id: "33333333-3333-4333-8333-333333333333",
  revocation_reason_code: "security",
  governance_status: "revoked",
  binding_eligible: false,
  created_by_user_id: "33333333-3333-4333-8333-333333333333",
  created_at: "2026-08-19T00:00:00Z",
};

describe("admin System Skill revocation status", () => {
  test("governance revocation overrides the workflow status", () => {
    expect(adminAssetVersionStatus(revokedVersion)).toBe("revoked");
    const queryClient = new QueryClient();

    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <QueryClientProvider client={queryClient}>
          <VersionTimeline kind="skills" versions={[revokedVersion]} />
        </QueryClientProvider>
      </I18nProvider>,
    );

    expect(html).toContain("已撤销");
    expect(html).not.toContain(">已发布<");
  });
});

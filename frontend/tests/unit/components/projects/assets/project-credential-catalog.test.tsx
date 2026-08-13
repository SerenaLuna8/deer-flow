import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { CREATE_CREDENTIAL_TYPE_OPTIONS } from "@/components/admin/assets/admin-asset-dialogs";
import { ProjectCredentialCatalogView } from "@/components/projects/assets/project-assets-page";
import { I18nProvider } from "@/core/i18n/context";
import type { ProjectCredentialList } from "@/core/shared-assets";

const CREDENTIAL_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const VERSION_ID = "33333333-3333-4333-8333-333333333333";

describe("ProjectCredentialCatalogView", () => {
  test("offers only the three governed credential types", () => {
    expect(CREATE_CREDENTIAL_TYPE_OPTIONS).toEqual([
      "mcp_auth",
      "skill_auth",
      "model_api_key",
    ]);
  });

  test("shows current credential metadata without version history", () => {
    const data: ProjectCredentialList = {
      system_items: [],
      project_items: [
        {
          id: CREDENTIAL_ID,
          scope: "project",
          project_id: PROJECT_ID,
          name: "trans-resource-mcp",
          display_name: "传输MCP凭证",
          credential_type: "skill_auth",
          status: "active",
          current_version_id: VERSION_ID,
          version: 2,
          created_by_user_id: "owner",
          created_at: "2026-08-13T09:00:00Z",
          updated_at: "2026-08-13T10:00:00Z",
          capabilities: [],
        },
      ],
      request_id: "request-credentials",
    };

    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <ProjectCredentialCatalogView data={data} />
      </I18nProvider>,
    );

    expect(html).toContain("传输MCP凭证");
    expect(html).toContain("SKILL 认证");
    expect(html).toContain("元数据版本");
    expect(html).not.toContain("版本历史");
    expect(html).not.toContain("上一版本");
  });
});

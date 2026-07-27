import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  SkillCredentialBindingEditor,
  skillCredentialBindingsPayload,
} from "@/components/projects/assets/skill-credential-bindings";
import type { SkillCredentialBindingsResponse } from "@/core/shared-assets";

const CREDENTIAL_VERSION_ID = "66666666-6666-4666-8666-666666666666";

const bindings: SkillCredentialBindingsResponse = {
  skill_id: "33333333-3333-4333-8333-333333333333",
  skill_version_id: "44444444-4444-4444-8444-444444444444",
  revision: 3,
  requirements: [
    {
      name: "WEATHER_API_KEY",
      optional: false,
      configured: true,
      credential_id: "55555555-5555-4555-8555-555555555555",
      credential_version_id: CREDENTIAL_VERSION_ID,
      credential_display_name: "Weather production",
      credential_version_number: 2,
      eligible_credentials: [
        {
          credential_id: "55555555-5555-4555-8555-555555555555",
          credential_version_id: CREDENTIAL_VERSION_ID,
          display_name: "Weather production",
          version_number: 2,
        },
      ],
    },
    {
      name: "OPTIONAL_REGION",
      optional: true,
      configured: false,
      credential_id: null,
      credential_version_id: null,
      credential_display_name: null,
      credential_version_number: null,
      eligible_credentials: [],
    },
  ],
  request_id: "request-bindings",
};

describe("Skill Credential binding editor", () => {
  test("renders a safe empty state when the current published version declares no requirements", () => {
    const html = renderToStaticMarkup(
      <SkillCredentialBindingEditor
        data={{ ...bindings, requirements: [] }}
        canManage
        credentialsHref="/projects/demo/credentials"
        pending={false}
        errorMessage={null}
        onReload={rs.fn()}
        onSave={rs.fn()}
      />,
    );

    expect(html).toContain("当前发布版本没有声明环境变量");
    expect(html).not.toContain("保存配置");
  });

  test("shows declarations and Credential metadata without rendering secret inputs", () => {
    const html = renderToStaticMarkup(
      <SkillCredentialBindingEditor
        data={bindings}
        canManage
        credentialsHref="/projects/demo/credentials"
        pending={false}
        errorMessage={null}
        onReload={rs.fn()}
        onSave={rs.fn()}
      />,
    );

    expect(html).toContain("环境变量");
    expect(html).toContain("WEATHER_API_KEY");
    expect(html).toContain("Weather production");
    expect(html).toContain("OPTIONAL_REGION");
    expect(html).toContain("添加环境变量");
    expect(html).toContain("/projects/demo/credentials");
    expect(html).not.toContain('type="password"');
    expect(html).not.toContain("凭据值");
    expect(html).not.toContain("must-never-enter-query-cache");
  });

  test("builds a metadata-only whole-set update", () => {
    expect(
      skillCredentialBindingsPayload(3, {
        WEATHER_API_KEY: CREDENTIAL_VERSION_ID,
        OPTIONAL_REGION: "",
      }),
    ).toEqual({
      expected_revision: 3,
      bindings: [
        {
          name: "WEATHER_API_KEY",
          credential_version_id: CREDENTIAL_VERSION_ID,
        },
      ],
    });
  });
});

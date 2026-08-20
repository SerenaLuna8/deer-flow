import { describe, expect, rs, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SkillCredentialBindingEditor } from "@/components/projects/assets/skill-credential-bindings";
import { SkillCredentialOptionSelect } from "@/components/projects/assets/skill-credential-option-select";
import { I18nProvider } from "@/core/i18n/context";

const REPLACEMENT_VERSION_ID = "11111111-1111-4111-8111-111111111111";
const STALE_VERSION_ID = "22222222-2222-4222-8222-222222222222";

const options = [
  {
    credential_id: "33333333-3333-4333-8333-333333333333",
    credential_version_id: REPLACEMENT_VERSION_ID,
    display_name: "Replacement database",
    version_number: 2,
    env_fields: ["DB_DATABASE"],
  },
];

function renderRow({
  credentialVersionId = "",
  sourceEnvFieldName = "",
  allowEmpty = false,
}: {
  credentialVersionId?: string;
  sourceEnvFieldName?: string;
  allowEmpty?: boolean;
} = {}) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">
      <SkillCredentialOptionSelect
        name="TEXT_ROUTE_DB_NAME"
        optional={false}
        mappingStatus={credentialVersionId ? "invalid" : "missing"}
        options={options}
        credentialVersionId={credentialVersionId}
        sourceEnvFieldName={sourceEnvFieldName}
        allowEmpty={allowEmpty}
        onCredentialChange={rs.fn()}
        onSourceEnvFieldChange={rs.fn()}
      />
    </I18nProvider>,
  );
}

describe("Skill Credential source-field selector", () => {
  test("keeps a disabled placeholder for an unbound active required mapping", () => {
    const html = renderRow();

    expect(html).toContain('value="" disabled="" selected=""');
    expect(html).toContain(`value="${REPLACEMENT_VERSION_ID}"`);
    expect(html).toContain("Not configured");
    expect(html).not.toContain("未配置");
  });

  test("renders an explicit unavailable option before a valid replacement", () => {
    const html = renderRow({
      credentialVersionId: STALE_VERSION_ID,
      sourceEnvFieldName: "OLD_DATABASE",
    });

    expect(html).toContain("Unavailable Credential · select a replacement");
    expect(html).toContain(
      `value="${STALE_VERSION_ID}" disabled="" selected=""`,
    );
    expect(html).toContain(`value="${REPLACEMENT_VERSION_ID}"`);
    expect(html).toContain("env.OLD_DATABASE · field no longer available");
  });

  test("hides redundant helper copy while preserving accessible names", () => {
    const html = renderRow({
      credentialVersionId: REPLACEMENT_VERSION_ID,
      sourceEnvFieldName: "DB_DATABASE",
    });

    expect(html).toContain(
      'aria-label="Project Credential · TEXT_ROUTE_DB_NAME"',
    );
    expect(html).toContain(
      'aria-label="Source environment variable · TEXT_ROUTE_DB_NAME"',
    );
    expect(html).not.toContain(
      "Project Credential · <code>TEXT_ROUTE_DB_NAME</code>",
    );
    expect(html).not.toContain(
      "Source environment variable · <code>TEXT_ROUTE_DB_NAME</code>",
    );
    expect(html).not.toContain("TEXT_ROUTE_DB_NAME ←");
  });

  test("renders the mapping workbench normal path entirely in English", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <SkillCredentialBindingEditor
          data={{
            skill_id: "44444444-4444-4444-8444-444444444444",
            skill_version_id: "55555555-5555-4555-8555-555555555555",
            revision: 0,
            requirements: [
              {
                name: "TEXT_ROUTE_DB_NAME",
                optional: false,
                configured: false,
                mapping_status: "missing",
                credential_id: null,
                credential_version_id: null,
                credential_display_name: null,
                credential_version_number: null,
                source_env_field_name: null,
                eligible_credentials: options,
              },
            ],
            request_id: "mapping-copy-test",
          }}
          skillActive={false}
          canManage
          credentialsHref="/projects/alpha/credentials"
          pending={false}
          errorMessage={null}
          onReload={rs.fn()}
          onSave={rs.fn()}
        />
      </I18nProvider>,
    );

    expect(html).toContain("2. Project Credential mappings");
    expect(html).toContain("Map each Skill environment variable");
    expect(html).toContain("Manage project Credentials");
    expect(html).toContain("Save mappings");
    expect(html).not.toContain("项目凭证映射");
  });
});

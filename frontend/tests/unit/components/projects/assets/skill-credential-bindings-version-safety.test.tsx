import { describe, expect, rs, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  guardedSkillCredentialBindingResponse,
  SkillCredentialBindings,
  skillCredentialVersionGuardAfterResponse,
} from "@/components/projects/assets/skill-credential-bindings";
import { I18nProvider } from "@/core/i18n/context";

const retainedV1 = {
  skill_id: "11111111-1111-4111-8111-111111111111",
  skill_version_id: "33333333-3333-4333-8333-333333333333",
  revision: 4,
  requirements: [],
  request_id: "binding-v1",
};

const refetch = rs.fn(() => Promise.resolve());

rs.mock("@/core/shared-assets", () => ({
  SharedAssetApiError: class SharedAssetApiError extends Error {},
  useProjectSkillCredentialBindings: () => ({
    data: {
      skill_id: "11111111-1111-4111-8111-111111111111",
      skill_version_id: "33333333-3333-4333-8333-333333333333",
      revision: 4,
      requirements: [],
      request_id: "stale-binding-response",
    },
    isLoading: false,
    isFetching: false,
    error: null,
    refetch,
  }),
  useUpdateProjectSkillCredentialBindings: () => ({
    isPending: false,
    error: null,
    mutate: rs.fn(),
    reset: rs.fn(),
  }),
}));

describe("Skill Credential binding version safety", () => {
  test("retains the mounted editor synchronously when the published pointer changes", () => {
    expect(
      guardedSkillCredentialBindingResponse(
        { retained: retainedV1, conflicted: false },
        "22222222-2222-4222-8222-222222222222",
        retainedV1,
      ),
    ).toEqual({ data: retainedV1, versionChanged: true });
  });

  test("requires explicit reload when the pointer and response advance together", () => {
    const responseV2 = {
      ...retainedV1,
      skill_version_id: "22222222-2222-4222-8222-222222222222",
      revision: 0,
      request_id: "binding-v2",
    };

    expect(
      skillCredentialVersionGuardAfterResponse(
        { retained: retainedV1, conflicted: false },
        responseV2.skill_version_id,
        responseV2,
      ),
    ).toEqual({ retained: retainedV1, conflicted: true });
  });

  test("fails closed when the binding response belongs to another Skill version", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <SkillCredentialBindings
          accountId="account-1"
          projectId="project-1"
          skillId="11111111-1111-4111-8111-111111111111"
          versionId="22222222-2222-4222-8222-222222222222"
          skillActive={false}
          canManage
          credentialsHref="/projects/demo/credentials"
        />
      </I18nProvider>,
    );

    expect(html).toContain(
      "The returned Credential mappings do not belong to this version",
    );
    expect(html).not.toContain("Save mappings");
    expect(html).not.toContain("Manage project Credentials");
  });
});

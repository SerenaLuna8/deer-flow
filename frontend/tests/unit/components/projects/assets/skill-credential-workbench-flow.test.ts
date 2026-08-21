import { describe, expect, test } from "@rstest/core";

import { skillCredentialBindingsMounted } from "@/components/projects/assets/skill-asset-detail";
import {
  skillCredentialBindingsPayload,
  skillCredentialVersionGuardAfterResponse,
} from "@/components/projects/assets/skill-credential-bindings";
import { beginSkillCredentialEditing } from "@/components/projects/assets/skill-version-workbench";

const CURRENT_VERSION_ID = "11111111-1111-4111-8111-111111111111";
const CANDIDATE_VERSION_ID = "22222222-2222-4222-8222-222222222222";

describe("Skill running Credential workbench flow", () => {
  test("enters editing without leaving the running Credential tab", () => {
    const events: string[] = [];

    beginSkillCredentialEditing(
      (surface) => events.push(`surface:${surface}`),
      (editing) => events.push(`editing:${editing}`),
    );

    expect(events).toEqual(["surface:secrets", "editing:true"]);
  });

  test("keeps Current Version bindings mounted while tabs hide and reveal the running Credential panel", () => {
    expect(
      skillCredentialBindingsMounted({
        selectedVersionId: CURRENT_VERSION_ID,
        editing: false,
      }),
    ).toBe(true);
  });

  test("unmounts exact-version mappings only while SKILL.md has no new version id yet", () => {
    expect(
      skillCredentialBindingsMounted({
        selectedVersionId: CURRENT_VERSION_ID,
        editing: true,
      }),
    ).toBe(false);
  });

  test("renders the exact Candidate Version mapping editor before activation", () => {
    expect(
      skillCredentialBindingsMounted({
        selectedVersionId: CANDIDATE_VERSION_ID,
        editing: false,
      }),
    ).toBe(true);
  });

  test("retains the exact-version editor and enters conflict when a newer version response arrives", () => {
    const retained = {
      skill_id: "33333333-3333-4333-8333-333333333333",
      skill_version_id: CURRENT_VERSION_ID,
      revision: 4,
      requirements: [],
      request_id: "binding-v1",
    };
    const newer = {
      ...retained,
      skill_version_id: CANDIDATE_VERSION_ID,
      revision: 0,
      request_id: "binding-v2",
    };

    expect(
      skillCredentialVersionGuardAfterResponse(
        { retained, conflicted: false },
        CURRENT_VERSION_ID,
        newer,
      ),
    ).toEqual({ retained, conflicted: true });
  });

  test("pins a binding replacement to the exact version shown by the editor", () => {
    expect(
      skillCredentialBindingsPayload(4, {
        API_KEY: {
          credentialVersionId: "33333333-3333-4333-8333-333333333333",
          sourceEnvFieldName: "SOURCE_API_TOKEN",
        },
      }),
    ).toEqual({
      expected_revision: 4,
      bindings: [
        {
          name: "API_KEY",
          credential_version_id: "33333333-3333-4333-8333-333333333333",
          source_env_field_name: "SOURCE_API_TOKEN",
        },
      ],
    });
  });
});

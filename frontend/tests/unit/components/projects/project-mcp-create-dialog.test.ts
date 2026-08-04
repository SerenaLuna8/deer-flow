import { describe, expect, test } from "@rstest/core";

import {
  compatibleProjectCredentialOptions,
  PROJECT_MCP_CREDENTIAL_TYPE,
  projectMcpSubmitIntent,
} from "@/components/projects/assets/project-mcp-create-dialog";
import type { AssetVersion, ProjectCredentialItem } from "@/core/shared-assets";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const CREATED_AT = "2026-08-03T08:00:00+00:00";

function credential({
  id,
  currentVersionId,
  scope = "project",
  status = "active",
  displayName,
}: {
  id: string;
  currentVersionId: string | null;
  scope?: "project" | "system";
  status?: "active" | "revoked";
  displayName: string;
}): ProjectCredentialItem {
  return {
    id,
    scope,
    project_id: scope === "project" ? PROJECT_ID : null,
    name: displayName.toLowerCase().replaceAll(" ", "-"),
    display_name: displayName,
    credential_type: "token",
    status,
    current_version_id: currentVersionId,
    version: 1,
    created_by_user_id: "user-1",
    created_at: CREATED_AT,
    updated_at: CREATED_AT,
    capabilities: [],
  };
}

function credentialVersion({
  id,
  credentialId,
  payloadSchema,
  status = "active",
}: {
  id: string;
  credentialId: string;
  payloadSchema: Record<string, string[]>;
  status?: "active" | "retired" | "revoked";
}): AssetVersion {
  return {
    id,
    credential_id: credentialId,
    version_number: 1,
    status,
    payload_schema_version: 1,
    payload_schema: payloadSchema,
    supersedes_version_id: null,
    created_by_user_id: "user-1",
    created_at: CREATED_AT,
  };
}

describe("project MCP create dialog model", () => {
  test("uses one internal Credential classification for MCP authentication", () => {
    expect(PROJECT_MCP_CREDENTIAL_TYPE).toBe("mcp_auth");
  });

  test("shows only active project Credentials whose current schema exactly matches", () => {
    const matchingId = "21111111-1111-4111-8111-111111111111";
    const wrongGroupId = "31111111-1111-4111-8111-111111111111";
    const extraFieldId = "41111111-1111-4111-8111-111111111111";
    const wrongCaseId = "51111111-1111-4111-8111-111111111111";
    const systemId = "61111111-1111-4111-8111-111111111111";
    const revokedId = "71111111-1111-4111-8111-111111111111";

    const rows = [
      {
        credential: credential({
          id: matchingId,
          currentVersionId: `${matchingId.slice(0, -1)}2`,
          displayName: "Trans Resource Basic Auth",
        }),
        versions: [
          credentialVersion({
            id: `${matchingId.slice(0, -1)}2`,
            credentialId: matchingId,
            payloadSchema: { headers: ["Authorization"] },
          }),
        ],
      },
      {
        credential: credential({
          id: wrongGroupId,
          currentVersionId: `${wrongGroupId.slice(0, -1)}2`,
          displayName: "Query Token",
        }),
        versions: [
          credentialVersion({
            id: `${wrongGroupId.slice(0, -1)}2`,
            credentialId: wrongGroupId,
            payloadSchema: { query: ["Authorization"] },
          }),
        ],
      },
      {
        credential: credential({
          id: extraFieldId,
          currentVersionId: `${extraFieldId.slice(0, -1)}2`,
          displayName: "Extra Header",
        }),
        versions: [
          credentialVersion({
            id: `${extraFieldId.slice(0, -1)}2`,
            credentialId: extraFieldId,
            payloadSchema: {
              headers: ["Authorization", "X-Tenant"],
            },
          }),
        ],
      },
      {
        credential: credential({
          id: wrongCaseId,
          currentVersionId: `${wrongCaseId.slice(0, -1)}2`,
          displayName: "Wrong Case",
        }),
        versions: [
          credentialVersion({
            id: `${wrongCaseId.slice(0, -1)}2`,
            credentialId: wrongCaseId,
            payloadSchema: { headers: ["authorization"] },
          }),
        ],
      },
      {
        credential: credential({
          id: systemId,
          currentVersionId: `${systemId.slice(0, -1)}2`,
          scope: "system",
          displayName: "System Match",
        }),
        versions: [
          credentialVersion({
            id: `${systemId.slice(0, -1)}2`,
            credentialId: systemId,
            payloadSchema: { headers: ["Authorization"] },
          }),
        ],
      },
      {
        credential: credential({
          id: revokedId,
          currentVersionId: `${revokedId.slice(0, -1)}2`,
          status: "revoked",
          displayName: "Revoked Match",
        }),
        versions: [
          credentialVersion({
            id: `${revokedId.slice(0, -1)}2`,
            credentialId: revokedId,
            payloadSchema: { headers: ["Authorization"] },
          }),
        ],
      },
    ];

    expect(
      compatibleProjectCredentialOptions(rows, {
        group: "headers",
        fields: ["Authorization"],
      }),
    ).toEqual([
      {
        credentialId: matchingId,
        credentialVersionId: `${matchingId.slice(0, -1)}2`,
        displayName: "Trans Resource Basic Auth",
        name: "trans-resource-basic-auth",
      },
    ]);
  });

  test("treats Credential field order as part of the exact schema", () => {
    const credentialId = "81111111-1111-4111-8111-111111111111";
    const versionId = "91111111-1111-4111-8111-111111111111";
    const rows = [
      {
        credential: credential({
          id: credentialId,
          currentVersionId: versionId,
          displayName: "Reversed Headers",
        }),
        versions: [
          credentialVersion({
            id: versionId,
            credentialId,
            payloadSchema: {
              headers: ["X-Tenant", "Authorization"],
            },
          }),
        ],
      },
    ];

    expect(
      compatibleProjectCredentialOptions(rows, {
        group: "headers",
        fields: ["Authorization", "X-Tenant"],
      }),
    ).toEqual([]);
  });

  test("chooses a truthful submit intent for publish, approval, and handoff", () => {
    expect(
      projectMcpSubmitIntent({
        authMode: "none",
        canApprove: false,
        selectedCredentialVersionId: null,
      }),
    ).toBe("publish");
    expect(
      projectMcpSubmitIntent({
        authMode: "headers",
        canApprove: true,
        selectedCredentialVersionId: "81111111-1111-4111-8111-111111111111",
      }),
    ).toBe("approve");
    expect(
      projectMcpSubmitIntent({
        authMode: "query",
        canApprove: false,
        selectedCredentialVersionId: null,
      }),
    ).toBe("submit");
  });
});

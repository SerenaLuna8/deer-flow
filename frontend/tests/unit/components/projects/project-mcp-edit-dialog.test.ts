import { describe, expect, test } from "@rstest/core";

import {
  projectMcpActiveGrantCredentialVersionId,
  projectMcpEditInputMatchesVersion,
  projectMcpEditOperation,
  projectMcpEligibleGrantCredentialVersionId,
} from "@/components/projects/assets/project-mcp-edit-dialog";
import type {
  ProjectMcpEditableConfigurationResponse,
  UpdateConfiguredMcpInput,
} from "@/core/shared-assets";

const PROJECT_ID = "33333333-3333-4333-8333-333333333333";
const ASSET_ID = "11111111-1111-4111-8111-111111111111";
const VERSION_ID = "22222222-2222-4222-8222-222222222222";
const SLOT_ID = "44444444-4444-4444-8444-444444444444";
const CREDENTIAL_VERSION_ID = "55555555-5555-4555-8555-555555555555";

function configuration(
  workflowStatus: "published" | "pending_approval",
): ProjectMcpEditableConfigurationResponse {
  const currentPublishedVersionId =
    workflowStatus === "published"
      ? VERSION_ID
      : "66666666-6666-4666-8666-666666666666";
  return {
    item: {
      id: ASSET_ID,
      scope: "project",
      project_id: PROJECT_ID,
      slug: "transfer-resource-mcp",
      display_name: "Transfer resource MCP",
      status: "active",
      current_published_version_id: currentPublishedVersionId,
      version: 7,
      created_by_user_id: "editor-1",
      created_at: "2026-08-03T08:00:00Z",
      updated_at: "2026-08-03T08:00:00Z",
    },
    version: {
      id: VERSION_ID,
      mcp_server_id: ASSET_ID,
      version_number: 3,
      workflow_status: workflowStatus,
      definition: {
        description: "Transfer resource MCP",
        transport: "http",
        command: null,
        args: [],
        url: "http://127.0.0.1:8771/api/mcp",
        env: {},
        headers: {},
        oauth: {},
        routing: {},
        tool_overrides: {},
        timeout_seconds: 30,
        credential_slots: [
          {
            name: "auth",
            purpose: "MCP request header authentication",
            payload_schema: { headers: ["Authorization"] },
            required: true,
          },
        ],
      },
      credential_slots: [
        {
          id: SLOT_ID,
          name: "auth",
          purpose: "MCP request header authentication",
          payload_schema: { headers: ["Authorization"] },
          required: true,
        },
      ],
      credential_grants:
        workflowStatus === "published"
          ? [
              {
                id: "77777777-7777-4777-8777-777777777777",
                mcp_server_version_id: VERSION_ID,
                credential_slot_id: SLOT_ID,
                credential_version_id: CREDENTIAL_VERSION_ID,
                status: "active",
                version: 1,
                created_by_user_id: "admin-1",
                created_at: "2026-08-03T08:00:00Z",
              },
            ]
          : [],
      supersedes_version_id:
        workflowStatus === "pending_approval"
          ? currentPublishedVersionId
          : null,
      payload_checksum: "a".repeat(64),
      submitted_at:
        workflowStatus === "pending_approval" ? "2026-08-03T08:00:00Z" : null,
      reviewed_at:
        workflowStatus === "published" ? "2026-08-03T08:01:00Z" : null,
      reviewed_by_user_id: workflowStatus === "published" ? "admin-1" : null,
      created_by_user_id: "editor-1",
      created_at: "2026-08-03T08:00:00Z",
    },
    request_id: "req-editable-mcp",
  };
}

function input(
  baseline: ProjectMcpEditableConfigurationResponse,
): UpdateConfiguredMcpInput {
  return {
    description: baseline.version.definition.description,
    transport: "http",
    command: null,
    args: [],
    url: baseline.version.definition.url,
    env: {},
    headers: {},
    oauth: {},
    routing: {},
    tool_overrides: {},
    timeout_seconds: 30,
    credential_slots: baseline.version.credential_slots.map((slot) => ({
      name: slot.name,
      purpose: slot.purpose,
      payload_schema: slot.payload_schema,
      required: slot.required,
    })),
    expected_asset_version: baseline.item.version,
  };
}

describe("Project MCP edit state machine", () => {
  test("compares the complete endpoint path as part of the editable baseline", () => {
    const baseline = configuration("published");
    expect(
      projectMcpEditInputMatchesVersion(input(baseline), baseline.version),
    ).toBe(true);
    expect(
      projectMcpEditInputMatchesVersion(
        { ...input(baseline), url: "http://127.0.0.1:8771" },
        baseline.version,
      ),
    ).toBe(false);
  });

  test("approves an unchanged existing pending configuration without another update", () => {
    const baseline = configuration("pending_approval");
    expect(
      projectMcpEditOperation({
        approvalTarget: null,
        baseline,
        input: input(baseline),
        canApprove: true,
        credentialSelectionTouched: true,
        selectedCredentialVersionId: CREDENTIAL_VERSION_ID,
        baselineCredentialVersionId: "",
      }),
    ).toEqual({ type: "approve", target: baseline });

    expect(
      projectMcpEditOperation({
        approvalTarget: null,
        baseline,
        input: input(baseline),
        canApprove: false,
        credentialSelectionTouched: false,
        selectedCredentialVersionId: "",
        baselineCredentialVersionId: "",
      }),
    ).toEqual({ type: "complete", target: baseline });
  });

  test("updates a changed pending configuration instead of ignoring edits", () => {
    const baseline = configuration("pending_approval");
    expect(
      projectMcpEditOperation({
        approvalTarget: null,
        baseline,
        input: { ...input(baseline), url: "http://127.0.0.1:8771/new/mcp" },
        canApprove: true,
        credentialSelectionTouched: true,
        selectedCredentialVersionId: CREDENTIAL_VERSION_ID,
        baselineCredentialVersionId: "",
      }),
    ).toEqual({ type: "update" });
  });

  test("an approval failure target makes every later submit approval-only", () => {
    const baseline = configuration("pending_approval");
    expect(
      projectMcpEditOperation({
        approvalTarget: baseline,
        baseline,
        input: { ...input(baseline), url: "http://127.0.0.1:8771/ignored" },
        canApprove: true,
        credentialSelectionTouched: true,
        selectedCredentialVersionId: CREDENTIAL_VERSION_ID,
        baselineCredentialVersionId: "",
      }),
    ).toEqual({ type: "approve", target: baseline });
  });

  test("preselects only an eligible current grant and treats a user-cleared grant as an update", () => {
    const baseline = configuration("published");
    const options = [
      {
        credentialId: "88888888-8888-4888-8888-888888888888",
        credentialVersionId: CREDENTIAL_VERSION_ID,
        displayName: "Transfer Basic Auth",
        name: "transfer-basic-auth",
      },
    ];
    expect(projectMcpActiveGrantCredentialVersionId(baseline.version)).toBe(
      CREDENTIAL_VERSION_ID,
    );
    expect(
      projectMcpEligibleGrantCredentialVersionId(baseline.version, options),
    ).toBe(CREDENTIAL_VERSION_ID);
    expect(
      projectMcpEditOperation({
        approvalTarget: null,
        baseline,
        input: input(baseline),
        canApprove: true,
        credentialSelectionTouched: true,
        selectedCredentialVersionId: "",
        baselineCredentialVersionId: CREDENTIAL_VERSION_ID,
      }),
    ).toEqual({ type: "update" });
  });
});

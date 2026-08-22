import { describe, expect, test } from "@rstest/core";

import {
  buildProjectMcpFormSubmission,
  projectMcpSecretSlotsForSubmission,
} from "@/components/projects/assets/project-mcp-form-dialog";

describe("Project MCP form secret-slot preservation", () => {
  test("editing a multi-slot definition preserves every exact slot", () => {
    const existing: Parameters<typeof projectMcpSecretSlotsForSubmission>[0] = [
      {
        name: "primary",
        purpose: "Primary authentication",
        payload_schema: { headers: ["Authorization"] },
        required: true,
      },
      {
        name: "tenant",
        purpose: "Tenant routing",
        payload_schema: { query: ["tenant_token"] },
        required: true,
      },
    ];
    const editedFirstSlot: Parameters<
      typeof projectMcpSecretSlotsForSubmission
    >[1] = [
      {
        name: "primary-edited",
        purpose: "Edited in the single-slot control",
        payload_schema: { headers: ["X-Api-Key"] },
        required: true,
      },
    ];

    expect(
      projectMcpSecretSlotsForSubmission(existing, editedFirstSlot),
    ).toEqual(existing);
  });

  test("new and single-slot definitions use the visible editor value", () => {
    const edited = [
      {
        name: "auth",
        purpose: "MCP request header secrets",
        payload_schema: { headers: ["Authorization"] },
        required: true,
      },
    ];

    expect(projectMcpSecretSlotsForSubmission([], edited)).toEqual(edited);
    expect(projectMcpSecretSlotsForSubmission([edited[0]!], edited)).toEqual(
      edited,
    );
  });

  test("an unchanged single-slot editor preserves optionality and purpose", () => {
    const existing: Parameters<typeof projectMcpSecretSlotsForSubmission>[0] = [
      {
        name: "tenant",
        purpose: "Optional tenant-specific routing",
        payload_schema: { query: ["tenant_token"] },
        required: false,
      },
    ];
    const editorProjection: Parameters<
      typeof projectMcpSecretSlotsForSubmission
    >[1] = [
      {
        name: "tenant",
        purpose: "MCP query parameter secrets",
        payload_schema: { query: ["tenant_token"] },
        required: true,
      },
    ];

    expect(
      projectMcpSecretSlotsForSubmission(existing, editorProjection),
    ).toEqual(existing);
  });

  test("local validation failure consumes write-only secret values first", () => {
    const form = new FormData();
    form.set("url", "https://mcp.example.test/tools?invalid=true");
    let cleared = false;

    expect(() =>
      buildProjectMcpFormSubmission({
        form,
        mode: "headers",
        slotName: "auth",
        fields: ["Authorization"],
        secretValues: { Authorization: "temporary-secret" },
        clearSecretValues: () => {
          cleared = true;
        },
      }),
    ).toThrow("MCP URL 必须是没有 query 或 fragment 的有效地址。");
    expect(cleared).toBe(true);
  });
});

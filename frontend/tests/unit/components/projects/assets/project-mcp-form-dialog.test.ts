import { describe, expect, test } from "@rstest/core";

import {
  buildProjectMcpFormSubmission,
  projectMcpSecretInputName,
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
    form.set(projectMcpSecretInputName(0), "temporary-secret");
    let cleared = false;

    expect(() =>
      buildProjectMcpFormSubmission({
        form,
        mode: "headers",
        slotName: "auth",
        fields: ["Authorization"],
        clearSecretValues: () => {
          cleared = true;
        },
      }),
    ).toThrow("MCP URL 必须是没有 query 或 fragment 的有效地址。");
    expect(cleared).toBe(true);
  });

  test("submits a complete write-only slot from the form snapshot", () => {
    const form = new FormData();
    form.set("display_name", "Example MCP");
    form.set("slug", "example-mcp");
    form.set("url", "http://127.0.0.1:65535/mcp");
    form.set(projectMcpSecretInputName(0), "temporary-secret");
    let cleared = false;

    const submission = buildProjectMcpFormSubmission({
      form,
      mode: "headers",
      slotName: "auth",
      fields: ["Authorization"],
      clearSecretValues: () => {
        cleared = true;
      },
    });

    expect(submission.secret).toEqual({
      slotName: "auth",
      payload: { headers: { Authorization: "temporary-secret" } },
    });
    expect(cleared).toBe(true);
  });
});

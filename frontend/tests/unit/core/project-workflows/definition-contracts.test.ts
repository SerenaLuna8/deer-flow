import { describe, expect, it } from "@rstest/core";

import {
  workflowCredentialGrantMutationRequestV1Schema,
  workflowCredentialGrantResponseV1Schema,
  workflowDefinitionArchiveRequestV1Schema,
  workflowDefinitionCreateRequestV1Schema,
  workflowDefinitionListQueryV1Schema,
  workflowDefinitionPageV1Schema,
  workflowDefinitionResponseV1Schema,
  workflowDefinitionUpdateRequestV1Schema,
  workflowDraftGrantIntentDeleteResponseV1Schema,
  workflowDraftGrantIntentResponseV1Schema,
  workflowDraftResponseV1Schema,
  workflowDraftSaveRequestV1Schema,
  workflowDraftValidateRequestV1Schema,
  workflowDraftValidationResponseV1Schema,
  workflowPublishRequestV1Schema,
  workflowPublishResponseV1Schema,
  workflowPublishedCredentialSlotV1Schema,
  workflowPublishedHttpRequirementV1Schema,
  workflowPublishedRequirementsV1Schema,
  workflowVersionListQueryV1Schema,
  workflowVersionPageV1Schema,
  workflowVersionResponseV1Schema,
} from "@/core/project-workflows";

import publicFixture from "../../../fixtures/workflows/public-projections-v1.json";
import definitionFixture from "../../../fixtures/workflows/workflow-definition-transport-v1.json";

const version = {
  id: definitionFixture.definition.current_published_version_id,
  workflow_id: definitionFixture.definition.id,
  version_number: 1,
  graph_schema_version: 1,
  canvas_schema_version: 1,
  compiler_contract_version: 1,
  semantic_checksum: definitionFixture.validation.semantic_checksum,
  spec: publicFixture.workflow_spec,
  canvas: publicFixture.canvas_document,
  credential_slots: [],
  missing_required_credential_slot_ids: [],
  executable: true,
  published_at: "2026-08-10T00:02:00Z",
};

const checksum = "e".repeat(64);
const grantMutation = {
  credential_id: definitionFixture.grant.credential_id,
  expected_credential_version_id: definitionFixture.grant.credential_version_id,
  expected_slot_schema_checksum:
    definitionFixture.grant.payload_schema_checksum,
};

describe("Workflow Definition transport V1", () => {
  it("accepts the strict public golden projections", () => {
    expect(
      workflowDefinitionResponseV1Schema.parse(definitionFixture.definition),
    ).toEqual(definitionFixture.definition);
    expect(
      workflowDraftResponseV1Schema.parse(definitionFixture.draft),
    ).toEqual(definitionFixture.draft);
    expect(
      workflowPublishedRequirementsV1Schema.parse(
        definitionFixture.requirements,
      ),
    ).toEqual(definitionFixture.requirements);
    expect(
      workflowDraftValidationResponseV1Schema.parse({
        ...definitionFixture.validation,
        requirements: definitionFixture.requirements,
      }),
    ).toEqual({
      ...definitionFixture.validation,
      requirements: definitionFixture.requirements,
    });
    expect(workflowVersionResponseV1Schema.parse(version)).toEqual(version);
    expect(
      workflowCredentialGrantResponseV1Schema.parse(definitionFixture.grant),
    ).toEqual(definitionFixture.grant);
    expect(
      workflowDraftGrantIntentResponseV1Schema.parse(
        definitionFixture.grant_intent,
      ),
    ).toEqual(definitionFixture.grant_intent);
    expect(
      workflowDraftGrantIntentDeleteResponseV1Schema.parse({
        workflow_id: definitionFixture.definition.id,
        slot_id: "http_auth",
        deleted: true,
      }),
    ).toEqual({
      workflow_id: definitionFixture.definition.id,
      slot_id: "http_auth",
      deleted: true,
    });
    expect(
      workflowDefinitionPageV1Schema.parse({
        items: [definitionFixture.definition],
        next_cursor: null,
      }).items,
    ).toHaveLength(1);
    expect(
      workflowVersionPageV1Schema.parse({ items: [version], next_cursor: null })
        .items,
    ).toHaveLength(1);
    expect(
      workflowPublishResponseV1Schema.parse({
        request_id: "req-g16-publish",
        workflow_id: version.workflow_id,
        version_id: version.id,
        version_number: version.version_number,
        graph_schema_version: version.graph_schema_version,
        canvas_schema_version: version.canvas_schema_version,
        compiler_contract_version: version.compiler_contract_version,
        semantic_checksum: version.semantic_checksum,
        spec: version.spec,
        canvas: version.canvas,
        credential_slots: version.credential_slots,
        missing_required_credential_slot_ids:
          version.missing_required_credential_slot_ids,
        executable: version.executable,
        published_at: version.published_at,
      }).version_id,
    ).toBe(version.id);
  });

  it("covers every closed Definition request and query contract", () => {
    expect(
      workflowDefinitionCreateRequestV1Schema.parse({
        name: "订单审核",
        description: "",
      }),
    ).toEqual({ name: "订单审核", description: "" });
    expect(
      workflowDefinitionUpdateRequestV1Schema.parse({
        expected_revision: 4,
        name: "订单审核 v2",
      }).expected_revision,
    ).toBe(4);
    expect(
      workflowDefinitionArchiveRequestV1Schema.parse({ expected_revision: 4 }),
    ).toEqual({ expected_revision: 4 });
    expect(workflowDefinitionListQueryV1Schema.parse({})).toEqual({
      query: null,
      lifecycle: "active",
      publication: "all",
      sort: "updated_desc",
      cursor: null,
      limit: 50,
    });
    expect(workflowVersionListQueryV1Schema.parse({})).toEqual({
      cursor: null,
      limit: 50,
    });
    expect(
      workflowCredentialGrantMutationRequestV1Schema.parse(grantMutation),
    ).toEqual(grantMutation);
    const cas = { expected_revision: 3, expected_draft_checksum: checksum };
    expect(workflowDraftValidateRequestV1Schema.parse(cas)).toEqual(cas);
    expect(workflowPublishRequestV1Schema.parse(cas)).toEqual(cas);
  });

  it("keeps Draft transport partial while rejecting invalid present values", () => {
    const request = {
      expected_revision: 3,
      spec: {
        schema_version: 1,
        nodes: [{ type: "llm", config: {} }],
      },
      canvas: { schema_version: 1 },
    };
    expect(workflowDraftSaveRequestV1Schema.safeParse(request).success).toBe(
      true,
    );
    expect(
      workflowDraftSaveRequestV1Schema.safeParse({
        ...request,
        spec: {
          ...request.spec,
          nodes: [{ type: "llm", config: { model_ref: 7 } }],
        },
      }).success,
    ).toBe(false);
    expect(
      workflowDraftSaveRequestV1Schema.safeParse({
        ...request,
        spec: {
          ...request.spec,
          nodes: [{ type: "llm", config: { unknown: true } }],
        },
      }).success,
    ).toBe(false);
    expect(
      workflowDraftSaveRequestV1Schema.safeParse({
        ...request,
        spec: { ...request.spec, nodes: new Set() },
      }).success,
    ).toBe(false);
    expect(
      workflowDraftSaveRequestV1Schema.safeParse({
        expected_revision: 3,
        spec: {
          schema_version: 1,
          workflow_inputs: [{ default: "\ud800" }],
        },
        canvas: { schema_version: 1 },
      }).success,
    ).toBe(false);
    expect(
      workflowPublishedCredentialSlotV1Schema.safeParse({
        slot_id: "http_auth",
        name: "HTTP auth",
        purpose: "http_auth",
        payload_schema: { description: "\ud800" },
        payload_schema_checksum: checksum,
        required: true,
      }).success,
    ).toBe(false);
  });

  it("matches backend partial-config acceptance through nested union branches", () => {
    const backendAcceptedPartialHttpConfig = {
      query: [{ id: "q", name: "q", value: {} }],
    };
    expect(
      workflowDraftSaveRequestV1Schema.safeParse({
        expected_revision: 3,
        spec: {
          schema_version: 1,
          nodes: [
            {
              type: "http_request",
              config: backendAcceptedPartialHttpConfig,
            },
          ],
        },
        canvas: { schema_version: 1 },
      }).success,
    ).toBe(true);
  });

  it("rejects a present invalid discriminator inside a partial union", () => {
    expect(
      workflowDraftSaveRequestV1Schema.safeParse({
        expected_revision: 3,
        spec: {
          schema_version: 1,
          nodes: [
            {
              type: "http_request",
              config: {
                query: [{ id: "q", name: "q", value: { kind: "future" } }],
              },
            },
          ],
        },
        canvas: { schema_version: 1 },
      }).success,
    ).toBe(false);
  });

  it("rejects server coordinates and independent authored payloads on mutations", () => {
    const cases: Array<[unknown, unknown]> = [
      [
        workflowDefinitionCreateRequestV1Schema,
        { name: "订单审核", description: "" },
      ],
      [
        workflowDefinitionUpdateRequestV1Schema,
        { expected_revision: 4, name: "订单审核" },
      ],
      [workflowDefinitionArchiveRequestV1Schema, { expected_revision: 4 }],
      [workflowCredentialGrantMutationRequestV1Schema, grantMutation],
      [
        workflowPublishRequestV1Schema,
        { expected_revision: 3, expected_draft_checksum: checksum },
      ],
    ];
    for (const [schema, value] of cases) {
      expect(
        (
          schema as {
            safeParse(input: unknown): { success: boolean };
          }
        ).safeParse({ ...(value as object), owner_id: "server-owned" }).success,
      ).toBe(false);
    }
    expect(
      workflowPublishRequestV1Schema.safeParse({
        expected_revision: 3,
        expected_draft_checksum: checksum,
        spec: publicFixture.workflow_spec,
        canvas: publicFixture.canvas_document,
      }).success,
    ).toBe(false);
  });

  it("preserves required-null and rejects omitted required-null fields", () => {
    expect(
      definitionFixture.definition.current_published_version_id,
    ).not.toBeNull();
    expect(definitionFixture.grant.revoked_at).toBeNull();
    const { revoked_at: requiredNull, ...missingRequiredNull } =
      definitionFixture.grant;
    expect(requiredNull).toBeNull();
    expect(
      workflowCredentialGrantResponseV1Schema.safeParse(missingRequiredNull)
        .success,
    ).toBe(false);
  });

  it.each([
    "owner_id",
    "project_id",
    "runtime_profile",
    "executor",
    "secret",
    "envelope_id",
    "__private",
  ])("rejects server-owned Draft material recursively: %s", (field) => {
    const request = {
      expected_revision: 3,
      spec: {
        schema_version: 1,
        nodes: [{ type: "start", config: { [field]: "forbidden" } }],
      },
      canvas: { schema_version: 1 },
    };
    expect(workflowDraftSaveRequestV1Schema.safeParse(request).success).toBe(
      false,
    );
  });

  it("rejects response drift and publication/grant contradictions", () => {
    expect(
      workflowDefinitionResponseV1Schema.safeParse({
        ...definitionFixture.definition,
        authority: "server-private",
      }).success,
    ).toBe(false);
    expect(
      workflowDefinitionResponseV1Schema.safeParse({
        ...definitionFixture.definition,
        publication: "draft_only",
      }).success,
    ).toBe(false);
    expect(
      workflowVersionResponseV1Schema.safeParse({
        ...version,
        executable: false,
      }).success,
    ).toBe(false);
    expect(
      workflowDraftValidationResponseV1Schema.safeParse({
        ...definitionFixture.validation,
        valid: false,
        issues: [],
        requirements: null,
      }).success,
    ).toBe(false);
    expect(
      workflowPublishedHttpRequirementV1Schema.safeParse({
        node_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        method: "GET",
        endpoint_policy_id: "public-api",
        credential_slot_id: null,
      }).success,
    ).toBe(false);
  });

  it("enforces code-point, canonical UUID, safe integer, and opaque cursor bounds", () => {
    expect(
      workflowDefinitionResponseV1Schema.safeParse({
        ...definitionFixture.definition,
        name: "😀".repeat(255),
      }).success,
    ).toBe(true);
    expect(
      workflowDefinitionResponseV1Schema.safeParse({
        ...definitionFixture.definition,
        name: "😀".repeat(256),
      }).success,
    ).toBe(false);
    expect(
      workflowDefinitionPageV1Schema.safeParse({
        items: [definitionFixture.definition],
        next_cursor: "x".repeat(1024),
      }).success,
    ).toBe(true);
    expect(
      workflowDefinitionPageV1Schema.safeParse({
        items: [definitionFixture.definition],
        next_cursor: "x".repeat(1025),
      }).success,
    ).toBe(false);
    expect(
      workflowDefinitionPageV1Schema.safeParse({
        items: [definitionFixture.definition],
        next_cursor: "服务端 opaque 游标",
      }).success,
    ).toBe(true);
    expect(
      workflowDefinitionResponseV1Schema.safeParse({
        ...definitionFixture.definition,
        id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".toUpperCase(),
      }).success,
    ).toBe(false);
    expect(
      workflowDefinitionResponseV1Schema.safeParse({
        ...definitionFixture.definition,
        revision: Number.MAX_SAFE_INTEGER + 1,
      }).success,
    ).toBe(false);
  });
});

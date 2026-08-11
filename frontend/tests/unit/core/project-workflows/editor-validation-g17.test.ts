import { describe, expect, it } from "@rstest/core";

import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import {
  validateWorkflowControlConnectionV1,
  validateWorkflowDraftStructureV1,
  workflowValidationIssueTarget,
} from "@/core/project-workflows/editor/validation";
import { valueTypeFromJsonSchema } from "@/core/project-workflows/json-schema";
import type { JsonSchema } from "@/core/project-workflows/types";

const START_ID = "00000000-0000-4000-8000-000000000001";
const FIRST_ID = "00000000-0000-4000-8000-000000000002";
const SECOND_ID = "00000000-0000-4000-8000-000000000003";
const END_ID = "00000000-0000-4000-8000-000000000004";
const LOOP_ID = "00000000-0000-4000-8000-000000000005";

function node(
  id: string,
  type: string,
  config: Record<string, unknown> = {},
  scope: Record<string, unknown> = { kind: "root" },
) {
  return { id, type, type_version: 1, scope, config };
}

function edge(
  id: string,
  sourceNodeId: string,
  sourcePortId: string,
  targetNodeId: string,
  targetPortId = "in",
) {
  return {
    id,
    source: { node_id: sourceNodeId, port_id: sourcePortId },
    target: { node_id: targetNodeId, port_id: targetPortId },
  };
}

function document(
  nodes: Array<Record<string, unknown>>,
  transitions: Array<Record<string, unknown>>,
): WorkflowPersistedDocumentV1 {
  return {
    spec: {
      schema_version: 1,
      entry_node_id: START_ID,
      nodes,
      transitions,
    },
    canvas: {
      schema_version: 1,
      node_layouts: nodes.map((item, index) => ({
        node_id: item.id as string,
        position: { x: index * 200, y: 0 },
        ...(typeof (item.scope as { loop_node_id?: unknown })?.loop_node_id ===
        "string"
          ? {
              parent_node_id: (item.scope as { loop_node_id: string })
                .loop_node_id,
            }
          : {}),
      })),
      edge_layouts: transitions.map((item) => ({
        edge_id: item.id as string,
        routing: "smoothstep" as const,
      })),
    },
  } as WorkflowPersistedDocumentV1;
}

function issueCodes(value: WorkflowPersistedDocumentV1): string[] {
  return validateWorkflowDraftStructureV1(value).map((issue) => issue.code);
}

describe("G17 local Workflow structural validation", () => {
  it("accepts a locally valid root DAG without requiring publish-complete node configs", () => {
    const value = document(
      [
        node(START_ID, "start"),
        node(FIRST_ID, "transform"),
        node(END_ID, "end"),
      ],
      [
        edge("edge-start-first", START_ID, "next", FIRST_ID),
        edge("edge-first-end", FIRST_ID, "next", END_ID),
      ],
    );

    expect(validateWorkflowDraftStructureV1(value)).toEqual([]);
  });

  it("locates missing nodes, wrong direction, non-control type, and unknown ports", () => {
    const value = document(
      [node(START_ID, "start"), node(FIRST_ID, "llm"), node(END_ID, "end")],
      [
        edge(
          "edge-missing",
          "00000000-0000-4000-8000-999999999999",
          "next",
          END_ID,
        ),
        edge("edge-direction", START_ID, "next", FIRST_ID, "next"),
        edge("edge-data", FIRST_ID, "text", END_ID),
        edge("edge-port", START_ID, "does_not_exist", END_ID),
      ],
    );
    const issues = validateWorkflowDraftStructureV1(value);

    expect(issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "WORKFLOW_TRANSITION_SOURCE_UNKNOWN",
          edge_id: "edge-missing",
        }),
        expect.objectContaining({
          code: "WORKFLOW_PORT_DIRECTION_INVALID",
          edge_id: "edge-direction",
          node_id: FIRST_ID,
          port_id: "next",
        }),
        expect.objectContaining({
          code: "WORKFLOW_CONTROL_PORT_TYPE_MISMATCH",
          edge_id: "edge-data",
          node_id: FIRST_ID,
          port_id: "text",
        }),
        expect.objectContaining({
          code: "WORKFLOW_SOURCE_PORT_UNKNOWN",
          edge_id: "edge-port",
          port_id: "does_not_exist",
        }),
      ]),
    );
  });

  it("rejects self loops, semantic duplicates, and one-cardinality fanout with edge/port targets", () => {
    const value = document(
      [
        node(START_ID, "start"),
        node(FIRST_ID, "http_request"),
        node(SECOND_ID, "end"),
        node(END_ID, "end"),
      ],
      [
        edge("edge-self", FIRST_ID, "success", FIRST_ID),
        edge("edge-one", FIRST_ID, "success", SECOND_ID),
        edge("edge-duplicate", FIRST_ID, "success", SECOND_ID),
        edge("edge-two", FIRST_ID, "success", END_ID),
      ],
    );
    const issues = validateWorkflowDraftStructureV1(value);

    expect(issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "WORKFLOW_CONTROL_SELF_LOOP",
          edge_id: "edge-self",
        }),
        expect.objectContaining({
          code: "WORKFLOW_CONTROL_EDGE_DUPLICATE",
          edge_id: "edge-duplicate",
        }),
        expect.objectContaining({
          code: "WORKFLOW_SOURCE_PORT_CARDINALITY",
          node_id: FIRST_ID,
          port_id: "success",
        }),
      ]),
    );
  });

  it("validates candidate connection before the store mutates its Draft", () => {
    const value = document(
      [node(START_ID, "start"), node(FIRST_ID, "end")],
      [edge("edge-existing", START_ID, "next", FIRST_ID)],
    );

    expect(
      validateWorkflowControlConnectionV1(value, {
        id: "edge-duplicate",
        source: { node_id: START_ID, port_id: "next" },
        target: { node_id: FIRST_ID, port_id: "in" },
      }).map((issue) => issue.code),
    ).toContain("WORKFLOW_CONTROL_EDGE_DUPLICATE");
  });

  it("rejects a third one-cardinality edge even when the Draft already has the same cardinality issue", () => {
    const thirdEndId = "00000000-0000-4000-8000-000000000006";
    const value = document(
      [
        node(START_ID, "start"),
        node(FIRST_ID, "http_request"),
        node(SECOND_ID, "end"),
        node(END_ID, "end"),
        node(thirdEndId, "end"),
      ],
      [
        edge("edge-one", FIRST_ID, "success", SECOND_ID),
        edge("edge-two", FIRST_ID, "success", END_ID),
      ],
    );

    expect(issueCodes(value)).toContain("WORKFLOW_SOURCE_PORT_CARDINALITY");
    expect(
      validateWorkflowControlConnectionV1(value, {
        id: "edge-three",
        source: { node_id: FIRST_ID, port_id: "success" },
        target: { node_id: thirdEndId, port_id: "in" },
      }),
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "WORKFLOW_SOURCE_PORT_CARDINALITY",
          edge_id: "edge-three",
          node_id: FIRST_ID,
          port_id: "success",
        }),
      ]),
    );
  });

  it.each([
    {
      type: "llm",
      outputId: "result",
      schema: {
        type: "object",
        properties: { answer: { type: "string" } },
        additionalProperties: false,
      },
      config: (schema: Record<string, unknown>) => ({
        structured_output: { enabled: true, schema },
      }),
      requirement: "object" as const,
    },
    {
      type: "transform",
      outputId: "result",
      schema: {
        anyOf: [{ type: "array", items: { type: "string" } }, { type: "null" }],
      },
      config: (schema: Record<string, unknown>) => ({
        mode: "json",
        output_schema: schema,
      }),
      requirement: "any" as const,
    },
    {
      type: "http_request",
      outputId: "body",
      schema: {
        anyOf: [{ type: "array", items: { type: "number" } }, { type: "null" }],
      },
      config: (schema: Record<string, unknown>) => ({
        response: { mode: "json", schema },
      }),
      requirement: "any" as const,
    },
    {
      type: "python_code",
      outputId: "result",
      schema: {
        type: "object",
        properties: { value: { type: "number" } },
        additionalProperties: false,
      },
      config: (schema: Record<string, unknown>) => ({ output_schema: schema }),
      requirement: "object" as const,
    },
  ])(
    "derives exact $type dynamic value_type including collection/nullability/schema_ref",
    ({ type, outputId, schema, config, requirement }) => {
      const expected = valueTypeFromJsonSchema(
        schema as unknown as JsonSchema,
        requirement,
      );
      const target = {
        ...node(SECOND_ID, "transform", {
          input_variables: [
            { id: "input", name: "input", value_type: expected },
          ],
        }),
        input_bindings: {
          input: {
            kind: "node_output",
            node_id: FIRST_ID,
            output_id: outputId,
          },
        },
      };
      const value = document(
        [node(FIRST_ID, type, config(schema)), target],
        [],
      );

      expect(issueCodes(value)).not.toContain("WORKFLOW_VALUE_TYPE_MISMATCH");
      (
        (target.config as { input_variables: Array<{ value_type: object }> })
          .input_variables[0]!.value_type as { schema_ref?: string }
      ).schema_ref = "inline-json-schema-v1:sha256:" + "0".repeat(64);
      expect(issueCodes(value)).toContain("WORKFLOW_VALUE_TYPE_MISMATCH");
    },
  );

  it("reports dangling node/Loop bindings anywhere inside existing node config", () => {
    const missingId = "00000000-0000-4000-8000-999999999999";
    const value = document(
      [
        node(FIRST_ID, "llm", {
          messages: [
            {
              id: "message",
              role: "user",
              content: {
                version: 1,
                segments: [
                  {
                    kind: "binding",
                    value: {
                      kind: "node_output",
                      node_id: missingId,
                      output_id: "text",
                    },
                  },
                  {
                    kind: "binding",
                    value: {
                      kind: "loop_variable",
                      loop_node_id: missingId,
                      variable_id: "value",
                    },
                  },
                ],
              },
            },
          ],
        }),
      ],
      [],
    );
    const issues = validateWorkflowDraftStructureV1(value);

    expect(issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "WORKFLOW_BINDING_SOURCE_UNKNOWN",
          node_id: FIRST_ID,
          path: expect.arrayContaining(["config", "messages"]),
        }),
        expect.objectContaining({
          code: "WORKFLOW_LOOP_VARIABLE_BINDING_UNKNOWN",
          node_id: FIRST_ID,
          path: expect.arrayContaining(["config", "messages"]),
        }),
      ]),
    );
  });

  it("checks root and Loop-body DAGs, cross-scope edges, nested Loop, and Canvas parent agreement", () => {
    const bodyScope = { kind: "loop_body", loop_node_id: LOOP_ID };
    const value = document(
      [
        node(START_ID, "start"),
        node(LOOP_ID, "loop", {
          body_entry_node_id: FIRST_ID,
          body_exit_node_id: SECOND_ID,
        }),
        node(FIRST_ID, "transform", {}, bodyScope),
        node(SECOND_ID, "loop", {}, bodyScope),
        node(END_ID, "end"),
      ],
      [
        edge("edge-body-forward", FIRST_ID, "next", SECOND_ID),
        edge("edge-body-cycle", SECOND_ID, "next", FIRST_ID),
        edge("edge-cross-scope", START_ID, "next", FIRST_ID),
        edge("edge-compiler-body", LOOP_ID, "body", FIRST_ID),
      ],
    );
    value.canvas.node_layouts!.find(
      (layout) => layout.node_id === FIRST_ID,
    )!.parent_node_id = undefined;
    const issues = validateWorkflowDraftStructureV1(value);

    expect(issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "WORKFLOW_AUTHORED_CYCLE",
          node_id: LOOP_ID,
        }),
        expect.objectContaining({
          code: "WORKFLOW_CROSS_SCOPE_TRANSITION",
          edge_id: "edge-cross-scope",
        }),
        expect.objectContaining({
          code: "WORKFLOW_LOOP_BODY_ROUTE_AUTHORED",
          edge_id: "edge-compiler-body",
          node_id: LOOP_ID,
          port_id: "body",
        }),
        expect.objectContaining({
          code: "WORKFLOW_NESTED_LOOP_FORBIDDEN",
          node_id: SECOND_ID,
        }),
        expect.objectContaining({
          code: "WORKFLOW_CANVAS_SCOPE_MISMATCH",
          node_id: FIRST_ID,
        }),
      ]),
    );
  });

  it("checks Condition branch/fallback identities and one-edge routing", () => {
    const value = document(
      [
        node(START_ID, "start"),
        node(FIRST_ID, "condition", {
          branches: [
            {
              id: "if",
              output_port_id: "branch",
              label: null,
              predicate: { op: "and", items: [] },
            },
            {
              id: "if",
              output_port_id: "branch",
              label: null,
              predicate: { op: "and", items: [] },
            },
          ],
          else_output_port_id: "branch",
        }),
        node(SECOND_ID, "end"),
        node(END_ID, "end"),
      ],
      [
        edge("edge-branch-one", FIRST_ID, "branch", SECOND_ID),
        edge("edge-branch-two", FIRST_ID, "branch", END_ID),
      ],
    );
    const codes = issueCodes(value);

    expect(codes).toContain("WORKFLOW_CONDITION_BRANCH_ID_DUPLICATE");
    expect(codes).toContain("WORKFLOW_CONDITION_PORT_ID_DUPLICATE");
    expect(codes).toContain("WORKFLOW_SOURCE_PORT_CARDINALITY");
  });

  it("checks present node-output binding ports and value types", () => {
    const valueType = {
      kind: "number",
      collection: false,
      nullable: false,
    };
    const value = document(
      [
        node(START_ID, "start"),
        {
          ...node(FIRST_ID, "transform", {
            mode: "text",
            input_variables: [
              { id: "input", name: "input", value_type: valueType },
            ],
          }),
          input_bindings: {
            input: {
              kind: "node_output",
              node_id: START_ID,
              output_id: "name",
            },
          },
        },
        node(END_ID, "end"),
      ],
      [
        edge("edge-start-first", START_ID, "next", FIRST_ID),
        edge("edge-first-end", FIRST_ID, "next", END_ID),
      ],
    );
    value.spec.workflow_inputs = [
      {
        id: "name",
        name: "name",
        value_type: {
          kind: "string",
          collection: false,
          nullable: false,
        },
      },
    ];
    const issues = validateWorkflowDraftStructureV1(value);

    expect(issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "WORKFLOW_VALUE_TYPE_MISMATCH",
          node_id: FIRST_ID,
          port_id: "input",
        }),
      ]),
    );
  });

  it("projects an issue into a keyboard-focusable node/edge/port target", () => {
    expect(
      workflowValidationIssueTarget({
        severity: "error",
        code: "WORKFLOW_SOURCE_PORT_UNKNOWN",
        message: "bad port",
        path: ["spec", "transitions", "0", "source", "port_id"],
        node_id: START_ID,
        edge_id: "edge-bad",
        port_id: "next",
      }),
    ).toEqual({
      kind: "port",
      node_id: START_ID,
      edge_id: "edge-bad",
      port_id: "next",
    });
  });
});

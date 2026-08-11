import { describe, expect, it } from "@rstest/core";

import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import {
  resolveDraftNodePorts,
  workflowDraftPortSignature,
} from "@/core/project-workflows/editor/ports";

const ids = {
  start: "00000000-0000-4000-8000-000000000001",
  condition: "00000000-0000-4000-8000-000000000002",
  aggregate: "00000000-0000-4000-8000-000000000003",
  loop: "00000000-0000-4000-8000-000000000004",
  http: "00000000-0000-4000-8000-000000000005",
  python: "00000000-0000-4000-8000-000000000006",
} as const;

const document = (): WorkflowPersistedDocumentV1 =>
  ({
    spec: {
      schema_version: 1,
      entry_node_id: ids.start,
      workflow_inputs: [
        {
          id: "input_name",
          name: "name",
          label: "姓名",
          value_type: {
            kind: "string",
            collection: false,
            nullable: false,
          },
          required: true,
          constraints: { kind: "none" },
        },
      ],
      nodes: [
        {
          id: ids.start,
          type: "start",
          type_version: 1,
          scope: { kind: "root" },
          config: {},
        },
        {
          id: ids.condition,
          type: "condition",
          type_version: 1,
          scope: { kind: "root" },
          config: {
            branches: [
              {
                id: "branch_truthy",
                label: "命中",
                output_port_id: "truthy",
                predicate: { op: "is_not_null", input_id: "input_name" },
              },
            ],
            else_output_port_id: "fallback",
          },
        },
        {
          id: ids.aggregate,
          type: "variable_aggregate",
          type_version: 1,
          scope: { kind: "root" },
          config: {
            groups: [
              {
                id: "merged_value",
                name: "合并值",
                value_type: {
                  kind: "string",
                  collection: false,
                  nullable: false,
                },
                candidate_input_ids: ["left", "right"],
                strategy: "exclusive_branch",
              },
            ],
          },
        },
        {
          id: ids.loop,
          type: "loop",
          type_version: 1,
          scope: { kind: "root" },
          config: {
            variables: [
              {
                id: "counter",
                name: "counter",
                value_type: {
                  kind: "number",
                  collection: false,
                  nullable: false,
                },
                initial_input_id: "initial_counter",
                next_input_id: "next_counter",
                output_port_id: "counter_value",
              },
            ],
          },
        },
        {
          id: ids.http,
          type: "http_request",
          type_version: 1,
          scope: { kind: "root" },
          config: {
            response: { mode: "text" },
          },
        },
        {
          id: ids.python,
          type: "python_code",
          type_version: 1,
          scope: { kind: "root" },
          config: {
            output_schema: {
              type: "object",
              properties: { ok: { type: "boolean" } },
              required: ["ok"],
              additionalProperties: false,
            },
          },
        },
      ],
      transitions: [],
    },
    canvas: { schema_version: 1, node_layouts: [], edge_layouts: [] },
  }) as WorkflowPersistedDocumentV1;

describe("G20 shared partial-safe node ports", () => {
  it("projects fixed and dynamic control ports from the single registry authority", () => {
    const value = document();

    expect(
      resolveDraftNodePorts(value, ids.condition, "zh-CN").outputPorts.map(
        (port) => port.id,
      ),
    ).toEqual(["error", "truthy", "fallback"]);
    expect(
      resolveDraftNodePorts(value, ids.loop, "zh-CN").outputPorts.map(
        (port) => port.id,
      ),
    ).toEqual(["body", "next", "error", "iteration_count", "counter_value"]);
    expect(
      resolveDraftNodePorts(value, ids.http, "zh-CN").outputPorts.map(
        (port) => port.id,
      ),
    ).toEqual([
      "success",
      "error",
      "status_code",
      "headers",
      "duration_ms",
      "body",
    ]);
  });

  it("preserves dynamic data types for Start, Aggregate, Loop, HTTP, and Python", () => {
    const value = document();

    expect(
      resolveDraftNodePorts(value, ids.start, "zh-CN").outputPorts,
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: "input_name",
          kind: "data",
          valueType: expect.objectContaining({ kind: "string" }),
        }),
      ]),
    );
    expect(
      resolveDraftNodePorts(value, ids.aggregate, "zh-CN").outputPorts,
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: "merged_value",
          valueType: expect.objectContaining({ kind: "string" }),
        }),
      ]),
    );
    expect(resolveDraftNodePorts(value, ids.http, "zh-CN").outputPorts).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: "body",
          valueType: expect.objectContaining({ kind: "string" }),
        }),
      ]),
    );
    expect(
      resolveDraftNodePorts(value, ids.python, "zh-CN").outputPorts,
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: "result",
          valueType: expect.objectContaining({
            kind: "json",
            collection: false,
          }),
        }),
      ]),
    );
  });

  it("changes localized labels without changing the Handle signature", () => {
    const value = document();
    const zh = resolveDraftNodePorts(value, ids.loop, "zh-CN");
    const en = resolveDraftNodePorts(value, ids.loop, "en-US");

    expect(zh.outputPorts.find((port) => port.id === "body")?.label).toBe(
      "循环体",
    );
    expect(en.outputPorts.find((port) => port.id === "body")?.label).toBe(
      "Loop Body",
    );
    expect(workflowDraftPortSignature(zh)).toBe(workflowDraftPortSignature(en));
  });

  it("keeps an incomplete or unknown Draft visible without throwing", () => {
    const value = document();
    value.spec.nodes = [
      {
        id: ids.condition,
        type: "condition",
        scope: { kind: "root" },
        config: {
          branches: [
            { output_port_id: "valid_partial" },
            { output_port_id: "not a port" },
          ],
        },
      },
      {
        id: ids.python,
        type: "future_node",
        type_version: 99,
        scope: { kind: "root" },
        config: { secret: "must-not-leak" },
      },
    ] as WorkflowPersistedDocumentV1["spec"]["nodes"];

    expect(() =>
      resolveDraftNodePorts(value, ids.condition, "zh-CN"),
    ).not.toThrow();
    expect(
      resolveDraftNodePorts(value, ids.condition, "zh-CN").outputPorts.map(
        (port) => port.id,
      ),
    ).toEqual(["error", "valid_partial"]);
    expect(resolveDraftNodePorts(value, ids.python, "zh-CN")).toEqual({
      inputPorts: [],
      outputPorts: [],
    });
  });
});

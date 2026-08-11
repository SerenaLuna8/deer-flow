import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import { describe, expect, it } from "@rstest/core";

import {
  canvasDocumentV1Schema,
  canonicalizeWorkflowSemanticValue,
  controlTransitionSchema,
  edgeIdSchema,
  loopNodeConfigV1Schema,
  predicateClauseSchema,
  pythonCodeNodeConfigV1Schema,
  resolveWorkflowInstancePortsV1,
  serializeCanonicalJsonValue,
  serializeCanonicalJsonValueWithinUtf8Budget,
  serializeWorkflowSemanticChecksumInput,
  valueBindingSchema,
  workflowNodeScopeSchema,
  workflowInputIdSchema,
  workflowCredentialSlotIdSchema,
  workflowCredentialSlotDeclSchema,
  workflowPortIdSchema,
  workflowValueTypeSchema,
  workflowSpecV1Schema,
  CANONICAL_BINARY64_ALGORITHM,
} from "@/core/project-workflows";

import canonicalBinary64Fixture from "../../../fixtures/workflows/canonical-binary64-v1.json";
import canonicalNumbersFixture from "../../../fixtures/workflows/canonical-numbers-v1.json";
import canvasFixture from "../../../fixtures/workflows/canvas-document-v1.json";
import publicProjectionsFixture from "../../../fixtures/workflows/public-projections-v1.json";
import unicodeBoundariesFixture from "../../../fixtures/workflows/unicode-code-point-boundaries-v1.json";
import workflowSpecFixture from "../../../fixtures/workflows/workflow-spec-v1.json";

const cloneFixture = (): Record<string, unknown> =>
  structuredClone(workflowSpecFixture) as Record<string, unknown>;

const CANONICAL_NODE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const UPPERCASE_NODE_ID = CANONICAL_NODE_ID.toUpperCase();

describe("WorkflowSpecV1 contract", () => {
  it("parses the frozen Python public projections without inventing omitted fields", () => {
    const spec = workflowSpecV1Schema.parse(
      publicProjectionsFixture.workflow_spec,
    );
    const canvas = canvasDocumentV1Schema.parse(
      publicProjectionsFixture.canvas_document,
    );

    expect(spec).toEqual(publicProjectionsFixture.workflow_spec);
    expect(canvas).toEqual(publicProjectionsFixture.canvas_document);
    expect(spec.workflow_inputs[0]!.label).toBeNull();
    expect("default" in spec.workflow_inputs[0]!).toBe(false);
    expect("schema_ref" in spec.workflow_inputs[0]!.value_type).toBe(false);
    expect("parent_node_id" in canvas.node_layouts[0]!).toBe(false);
  });

  it("counts JSON Pointer bounds in Unicode code points across runtimes", () => {
    const character = unicodeBoundariesFixture.astral_character;
    const { minimum, maximum } = unicodeBoundariesFixture.json_pointer;
    const binding = (path: string) => ({
      kind: "node_output" as const,
      node_id: CANONICAL_NODE_ID,
      output_id: "result",
      path,
    });

    expect(
      valueBindingSchema.safeParse(binding(character.repeat(minimum))).success,
    ).toBe(true);
    expect(
      valueBindingSchema.safeParse(binding(character.repeat(maximum))).success,
    ).toBe(true);
    expect(
      valueBindingSchema.safeParse(binding(character.repeat(maximum + 1)))
        .success,
    ).toBe(false);
  });

  it.each([
    "输入",
    "😀",
    "1starts-with-digit",
    "_leading-underscore",
    "a".repeat(129),
  ])("rejects non-canonical Workflow input id %s", (value) => {
    expect(workflowInputIdSchema.safeParse(value).success).toBe(false);
  });

  it.each([
    ["_leading-underscore", workflowPortIdSchema],
    ["分支", workflowPortIdSchema],
    ["1starts-with-digit", workflowCredentialSlotIdSchema],
    ["😀", workflowCredentialSlotIdSchema],
  ] as const)("rejects non-canonical port/slot id %s", (candidate, schema) => {
    expect(schema.safeParse(candidate).success).toBe(false);
  });

  it.each(["file", "image", "document"])(
    "rejects future first-batch value type %s",
    (kind) => {
      expect(
        workflowValueTypeSchema.safeParse({
          kind,
          collection: false,
          nullable: false,
        }).success,
      ).toBe(false);
    },
  );

  it("rejects integer coercion for the credential-slot required literal", () => {
    expect(
      workflowCredentialSlotDeclSchema.safeParse({
        id: "http_auth",
        name: "HTTP auth",
        purpose: "http_auth",
        payload_schema: {},
        required: 1,
      }).success,
    ).toBe(false);
  });

  it("accepts the golden fixture containing every first-wave node kind", () => {
    const parsed = workflowSpecV1Schema.parse(workflowSpecFixture);
    const resolved = resolveWorkflowInstancePortsV1(workflowSpecFixture);

    expect(new Set(parsed.nodes.map((node) => node.type))).toEqual(
      new Set([
        "start",
        "llm",
        "condition",
        "transform",
        "variable_aggregate",
        "loop",
        "http_request",
        "python_code",
        "end",
      ]),
    );
    expect(resolved.nodes).toHaveLength(parsed.nodes.length);
  });

  it("accepts only Workflow input ids that resolve as Start output ports", () => {
    const validInputId = `a${"_".repeat(127)}`;
    const valid = cloneFixture();
    const validInputs = valid.workflow_inputs as Array<Record<string, unknown>>;
    validInputs[0]!.id = validInputId;

    expect(workflowSpecV1Schema.safeParse(valid).success).toBe(true);
    expect(
      resolveWorkflowInstancePortsV1(valid)
        .nodes.find((node) => node.node_id === valid.entry_node_id)
        ?.output_ports.some((port) => port.id === validInputId),
    ).toBe(true);

    const invalid = cloneFixture();
    const invalidInputs = invalid.workflow_inputs as Array<
      Record<string, unknown>
    >;
    invalidInputs[0]!.id = "_leading-underscore";
    expect(workflowSpecV1Schema.safeParse(invalid).success).toBe(false);
    expect(() => resolveWorkflowInstancePortsV1(invalid)).toThrow();
  });

  it("enforces canonical UTF-8 budgets incrementally before number amplification", () => {
    expect(serializeCanonicalJsonValueWithinUtf8Budget(["测"], 7)).toEqual({
      canonical: '["测"]',
      utf8Bytes: 7,
    });
    expect(() =>
      serializeCanonicalJsonValueWithinUtf8Budget(["测"], 6),
    ).toThrow(/UTF-8 byte budget/);

    const escapedValue = {
      accent: "e\u0301",
      control: "\0\b\f\n\r\t",
      quoted: '"\\',
      separator: "\u2028",
    };
    const escapedCanonical = serializeCanonicalJsonValue(escapedValue);
    const escapedBytes = new TextEncoder().encode(escapedCanonical).byteLength;
    expect(
      serializeCanonicalJsonValueWithinUtf8Budget(escapedValue, escapedBytes),
    ).toEqual({ canonical: escapedCanonical, utf8Bytes: escapedBytes });
    expect(() =>
      serializeCanonicalJsonValueWithinUtf8Budget(
        escapedValue,
        escapedBytes - 1,
      ),
    ).toThrow(/UTF-8 byte budget/);

    let visited = 0;
    const amplified = new Proxy(Array(65_535).fill(Number.MIN_VALUE), {
      get(target, property, receiver) {
        if (typeof property === "string" && /^\d+$/.test(property)) {
          visited += 1;
        }
        return Reflect.get(target, property, receiver);
      },
    });
    expect(() =>
      serializeCanonicalJsonValueWithinUtf8Budget(amplified, 4_096),
    ).toThrow(/UTF-8 byte budget/);
    expect(visited).toBeLessThan(10);
  });

  it("rejects unknown fields at every authority-bearing object boundary", () => {
    const topLevel = { ...cloneFixture(), origin_trace_id: "private" };
    expect(workflowSpecV1Schema.safeParse(topLevel).success).toBe(false);

    const nested = cloneFixture();
    const nodes = nested.nodes as Array<Record<string, unknown>>;
    nodes[0] = { ...nodes[0], runtime_executor: "browser" };
    expect(workflowSpecV1Schema.safeParse(nested).success).toBe(false);

    const code = cloneFixture();
    const codeNode = (code.nodes as Array<Record<string, unknown>>).find(
      (node) => node.type === "python_code",
    );
    codeNode!.config = {
      ...(codeNode!.config as Record<string, unknown>),
      network: true,
    };
    expect(workflowSpecV1Schema.safeParse(code).success).toBe(false);
  });

  it("rejects a node type/config mismatch", () => {
    const mismatched = cloneFixture();
    const startNode = (mismatched.nodes as Array<Record<string, unknown>>).find(
      (node) => node.type === "start",
    );
    const llmNode = (mismatched.nodes as Array<Record<string, unknown>>).find(
      (node) => node.type === "llm",
    );
    startNode!.config = structuredClone(llmNode!.config);

    expect(workflowSpecV1Schema.safeParse(mismatched).success).toBe(false);
  });

  it("uses bounded safe edge identities for transitions", () => {
    expect(edgeIdSchema.parse("edge-1")).toBe("edge-1");

    for (const edgeId of ["", "has space", "a".repeat(129), "-leading"]) {
      const invalid = cloneFixture();
      (invalid.transitions as Array<Record<string, unknown>>)[0]!.id = edgeId;
      expect(
        controlTransitionSchema.safeParse({
          ...(invalid.transitions as Array<Record<string, unknown>>)[0],
        }).success,
      ).toBe(false);
      expect(workflowSpecV1Schema.safeParse(invalid).success).toBe(false);
    }
  });

  it("rejects unsupported spec and node contract versions", () => {
    const specVersion = cloneFixture();
    specVersion.schema_version = 2;
    expect(workflowSpecV1Schema.safeParse(specVersion).success).toBe(false);

    for (let index = 0; index < workflowSpecFixture.nodes.length; index += 1) {
      const nodeVersion = cloneFixture();
      (nodeVersion.nodes as Array<Record<string, unknown>>)[
        index
      ]!.type_version = 2;
      expect(workflowSpecV1Schema.safeParse(nodeVersion).success).toBe(false);
    }
  });

  it("rejects non-JSON values and non-finite numbers", () => {
    const invalid = cloneFixture();
    const llmNode = (invalid.nodes as Array<Record<string, unknown>>).find(
      (node) => node.type === "llm",
    );
    (llmNode!.config as Record<string, unknown>).model_parameters = {
      invalid: Number.NaN,
    };

    expect(workflowSpecV1Schema.safeParse(invalid).success).toBe(false);
  });

  it("rejects unpaired UTF-16 surrogates at the strict schema boundary", () => {
    const value = cloneFixture();
    const input = (value.workflow_inputs as Array<Record<string, unknown>>)[0]!;
    input.default = "\uD800";
    expect(workflowSpecV1Schema.safeParse(value).success).toBe(false);

    const key = cloneFixture();
    const llmNode = (key.nodes as Array<Record<string, unknown>>).find(
      (node) => node.type === "llm",
    );
    (llmNode!.config as Record<string, unknown>).model_parameters = {
      ["bad\uD800"]: true,
    };
    expect(workflowSpecV1Schema.safeParse(key).success).toBe(false);
  });

  it("rejects integers outside the JavaScript safe-integer range", () => {
    for (const value of canonicalNumbersFixture.rejected) {
      const invalid = cloneFixture();
      const llmNode = (invalid.nodes as Array<Record<string, unknown>>).find(
        (node) => node.type === "llm",
      );
      (llmNode!.config as Record<string, unknown>).model_parameters = {
        invalid: value,
      };

      expect(workflowSpecV1Schema.safeParse(invalid).success).toBe(false);
    }
  });

  it("distinguishes optional fields from explicitly nullable fields", () => {
    expect(
      workflowValueTypeSchema.safeParse({
        kind: "json",
        collection: false,
        nullable: true,
        schema_ref: null,
      }).success,
    ).toBe(false);
    expect(
      valueBindingSchema.safeParse({
        kind: "node_output",
        node_id: "00000000-0000-4000-8000-000000000001",
        output_id: "result",
        path: null,
      }).success,
    ).toBe(false);
    expect(
      predicateClauseSchema.safeParse({
        left: { kind: "literal", value: true },
        operator: "is_null",
        right: null,
      }).success,
    ).toBe(false);
  });

  it("rejects non-canonical uppercase UUIDs for every authored node identity", () => {
    const entry = cloneFixture();
    entry.entry_node_id = UPPERCASE_NODE_ID;
    expect(workflowSpecV1Schema.safeParse(entry).success).toBe(false);

    const node = cloneFixture();
    (node.nodes as Array<Record<string, unknown>>)[0]!.id = UPPERCASE_NODE_ID;
    expect(workflowSpecV1Schema.safeParse(node).success).toBe(false);

    expect(
      workflowNodeScopeSchema.safeParse({
        kind: "loop_body",
        loop_node_id: UPPERCASE_NODE_ID,
      }).success,
    ).toBe(false);
    expect(
      valueBindingSchema.safeParse({
        kind: "loop_variable",
        loop_node_id: UPPERCASE_NODE_ID,
        variable_id: "counter",
      }).success,
    ).toBe(false);
    expect(
      valueBindingSchema.safeParse({
        kind: "node_output",
        node_id: UPPERCASE_NODE_ID,
        output_id: "result",
      }).success,
    ).toBe(false);

    const loopNode = (
      workflowSpecFixture.nodes as Array<Record<string, unknown>>
    ).find((candidate) => candidate.type === "loop")!;
    for (const field of ["body_entry_node_id", "body_exit_node_id"] as const) {
      const config = structuredClone(loopNode.config) as Record<
        string,
        unknown
      >;
      config[field] = UPPERCASE_NODE_ID;
      expect(loopNodeConfigV1Schema.safeParse(config).success).toBe(false);
    }

    for (const endpoint of ["source", "target"] as const) {
      const transition = {
        id: "transition",
        source: { node_id: CANONICAL_NODE_ID, port_id: "next" },
        target: { node_id: CANONICAL_NODE_ID, port_id: "input" },
      };
      transition[endpoint].node_id = UPPERCASE_NODE_ID;
      expect(controlTransitionSchema.safeParse(transition).success).toBe(false);
    }

    expect(
      pythonCodeNodeConfigV1Schema.safeParse({
        source: "def main(inputs):\n    return {}",
        input_variables: [
          {
            id: UPPERCASE_NODE_ID,
            name: "arg1",
            value_type: {
              kind: "string",
              collection: false,
              nullable: false,
            },
          },
        ],
        output_schema: {},
        timeout_ms: null,
      }).success,
    ).toBe(false);
  });

  it.each([CANONICAL_NODE_ID.replaceAll("-", ""), `{${CANONICAL_NODE_ID}}`])(
    "rejects non-canonical node UUID representation %s",
    (nodeId) => {
      expect(
        workflowSpecV1Schema.safeParse({
          ...cloneFixture(),
          entry_node_id: nodeId,
        }).success,
      ).toBe(false);
    },
  );
});

describe("CanvasDocumentV1 contract", () => {
  it("accepts the golden canvas fixture", () => {
    expect(canvasDocumentV1Schema.parse(canvasFixture)).toEqual(canvasFixture);
  });

  it("rejects unsupported canvas contract versions", () => {
    expect(
      canvasDocumentV1Schema.safeParse({
        ...canvasFixture,
        schema_version: 2,
      }).success,
    ).toBe(false);
  });

  it("rejects viewport and React Flow runtime state", () => {
    expect(
      canvasDocumentV1Schema.safeParse({
        ...canvasFixture,
        viewport: { x: 0, y: 0, zoom: 1 },
      }).success,
    ).toBe(false);

    const measured = structuredClone(canvasFixture) as Record<string, unknown>;
    const layouts = measured.node_layouts as Array<Record<string, unknown>>;
    layouts[0] = { ...layouts[0], measured: { width: 100, height: 60 } };
    expect(canvasDocumentV1Schema.safeParse(measured).success).toBe(false);
  });

  it("rejects non-canonical UUIDs for canvas node and parent identities", () => {
    for (const field of ["node_id", "parent_node_id"] as const) {
      const invalid = structuredClone(canvasFixture) as Record<string, unknown>;
      const layouts = invalid.node_layouts as Array<Record<string, unknown>>;
      layouts[0]![field] = UPPERCASE_NODE_ID;
      expect(canvasDocumentV1Schema.safeParse(invalid).success).toBe(false);
    }
  });

  it("uses the same bounded safe edge identity for canvas layout", () => {
    for (const edgeId of ["has space", "a".repeat(129)]) {
      const invalid = structuredClone(canvasFixture) as Record<string, unknown>;
      const layouts = invalid.edge_layouts as Array<Record<string, unknown>>;
      layouts[0]!.edge_id = edgeId;
      expect(canvasDocumentV1Schema.safeParse(invalid).success).toBe(false);
    }
  });
});

describe("semantic canonical JSON", () => {
  const valueFromBinary64Bits = (bits: string): number => {
    const buffer = new ArrayBuffer(8);
    const view = new DataView(buffer);
    view.setBigUint64(0, BigInt(`0x${bits}`), false);
    return view.getFloat64(0, false);
  };

  const canonicalOrRejection = (value: number): string => {
    try {
      return serializeCanonicalJsonValue(value);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("finite")) return "reject:non-finite";
      if (message.includes("safe-integer")) return "reject:unsafe-integer";
      throw error;
    }
  };

  it("matches the versioned raw binary64 bit-pattern corpus", () => {
    expect(canonicalBinary64Fixture.algorithm).toBe(
      CANONICAL_BINARY64_ALGORITHM,
    );
    const legacyMismatches = canonicalBinary64Fixture.cases.filter(
      (testCase) => "legacy_python" in testCase,
    );
    expect(legacyMismatches).toHaveLength(
      canonicalBinary64Fixture.legacy_cross_runtime_mismatch_count,
    );
    for (const testCase of legacyMismatches) {
      expect(testCase.legacy_python).not.toBe(testCase.legacy_typescript);
    }
    for (const testCase of canonicalBinary64Fixture.cases) {
      expect(canonicalOrRejection(valueFromBinary64Bits(testCase.bits))).toBe(
        testCase.canonical,
      );
    }
  });

  it("matches the deterministic random binary64 property digest", () => {
    const propertyContract = canonicalBinary64Fixture.random_property;
    expect(propertyContract.generator).toBe("splitmix64-v1");
    const mask = (1n << 64n) - 1n;
    let state = BigInt(`0x${propertyContract.seed}`);
    const hash = createHash("sha256");

    for (let index = 0; index < propertyContract.count; index += 1) {
      state = (state + 0x9e3779b97f4a7c15n) & mask;
      let generated = state;
      generated =
        ((generated ^ (generated >> 30n)) * 0xbf58476d1ce4e5b9n) & mask;
      generated =
        ((generated ^ (generated >> 27n)) * 0x94d049bb133111ebn) & mask;
      const bits = (generated ^ (generated >> 31n)) & mask;
      const bitsHex = bits.toString(16).padStart(16, "0");
      hash.update(
        `${bitsHex}:${canonicalOrRejection(valueFromBinary64Bits(bitsHex))}\n`,
      );
    }

    expect(hash.digest("hex")).toBe(propertyContract.sha256);
  });

  it("matches the shared Python/TypeScript canonical value corpus", () => {
    const corpus = JSON.parse(
      readFileSync("tests/fixtures/workflows/canonical-values-v1.json", "utf8"),
    ) as {
      accepted: Array<{
        value: Parameters<typeof serializeCanonicalJsonValue>[0];
        canonical: string;
      }>;
      rejected: Array<{
        value: Parameters<typeof serializeCanonicalJsonValue>[0];
      }>;
    };

    for (const { value, canonical } of corpus.accepted) {
      expect(serializeCanonicalJsonValue(value)).toBe(canonical);
      const expectedBytes = new TextEncoder().encode(canonical).byteLength;
      expect(
        serializeCanonicalJsonValueWithinUtf8Budget(value, expectedBytes),
      ).toEqual({ canonical, utf8Bytes: expectedBytes });
    }
    for (const { value } of corpus.rejected) {
      expect(() => serializeCanonicalJsonValue(value)).toThrow();
    }
  });

  it("uses the cross-runtime numeric golden encoding", () => {
    for (const { value, canonical } of canonicalNumbersFixture.accepted) {
      expect(serializeCanonicalJsonValue(value)).toBe(canonical);
    }
    for (const value of canonicalNumbersFixture.rejected) {
      expect(() => serializeCanonicalJsonValue(value)).toThrow(
        "safe-integer range",
      );
    }
  });

  it("matches the shared cross-runtime semantic checksum golden", () => {
    expect(
      resolveWorkflowInstancePortsV1(workflowSpecFixture).nodes,
    ).toHaveLength(workflowSpecFixture.nodes.length);
    const checksumInput =
      serializeWorkflowSemanticChecksumInput(workflowSpecFixture);
    const actual = createHash("sha256").update(checksumInput).digest("hex");
    const expected = readFileSync(
      "tests/fixtures/workflows/workflow-spec-v1.semantic.sha256",
      "utf8",
    ).trim();

    expect(actual).toBe(expected);
  });

  it("sorts only stable top-level collections and ignores presentation fields", () => {
    const reordered = cloneFixture();
    reordered.nodes = [
      ...(reordered.nodes as Array<Record<string, unknown>>),
    ].reverse();
    reordered.transitions = [
      ...(reordered.transitions as Array<Record<string, unknown>>),
    ].reverse();
    reordered.workflow_inputs = [
      ...(reordered.workflow_inputs as Array<Record<string, unknown>>),
    ].reverse();

    const nodes = reordered.nodes as Array<Record<string, unknown>>;
    nodes[0] = {
      ...nodes[0],
      custom_label: "另一个展示名称",
      description: "另一个展示描述",
    };
    const inputs = reordered.workflow_inputs as Array<Record<string, unknown>>;
    inputs[0] = { ...inputs[0], description: "另一个输入描述" };
    const outputs = reordered.workflow_outputs as Array<
      Record<string, unknown>
    >;
    outputs[0] = { ...outputs[0], description: "另一个输出描述" };

    expect(serializeWorkflowSemanticChecksumInput(reordered)).toBe(
      serializeWorkflowSemanticChecksumInput(workflowSpecFixture),
    );
  });

  it("preserves ordered semantic arrays", () => {
    const changed = cloneFixture();
    const aggregate = (changed.nodes as Array<Record<string, unknown>>).find(
      (node) => node.type === "variable_aggregate",
    );
    const groups = (
      aggregate!.config as { groups: Array<Record<string, unknown>> }
    ).groups;
    groups[0]!.candidate_input_ids = ["short-candidate", "long-candidate"];

    expect(serializeWorkflowSemanticChecksumInput(changed)).not.toBe(
      serializeWorkflowSemanticChecksumInput(workflowSpecFixture),
    );
  });

  it("normalizes Unicode, object-key order, and negative zero", () => {
    const first = cloneFixture();
    const second = cloneFixture();
    const firstLlm = (first.nodes as Array<Record<string, unknown>>).find(
      (node) => node.type === "llm",
    );
    const secondLlm = (second.nodes as Array<Record<string, unknown>>).find(
      (node) => node.type === "llm",
    );
    (firstLlm!.config as Record<string, unknown>).model_parameters = {
      zeta: -0,
      accent: "e\u0301",
    };
    (secondLlm!.config as Record<string, unknown>).model_parameters = {
      accent: "é",
      zeta: 0,
    };

    expect(serializeWorkflowSemanticChecksumInput(first)).toBe(
      serializeWorkflowSemanticChecksumInput(second),
    );
  });

  it("orders object keys by normalized Unicode scalar values", () => {
    expect(
      serializeCanonicalJsonValue({
        "\u{10000}": "supplementary",
        "\uE000": "private-use",
      }),
    ).toBe('{"\uE000":"private-use","\u{10000}":"supplementary"}');
  });

  it("orders integer-like object keys lexically instead of by JS enumeration", () => {
    expect(serializeCanonicalJsonValue({ "2": "two", "10": "ten" })).toBe(
      '{"10":"ten","2":"two"}',
    );
  });

  it.each(["\uD800", "\uDC00", `prefix\uD800suffix`])(
    "rejects unpaired UTF-16 surrogate %s",
    (value) => {
      expect(() => serializeCanonicalJsonValue(value)).toThrow(
        "unpaired UTF-16 surrogate",
      );
    },
  );

  it("normalizes an explicitly undefined optional field to omission", () => {
    const explicitUndefined = cloneFixture();
    const output = (
      explicitUndefined.workflow_outputs as Array<Record<string, unknown>>
    )[0]!;
    output.default = undefined;

    expect(serializeWorkflowSemanticChecksumInput(explicitUndefined)).toBe(
      serializeWorkflowSemanticChecksumInput(workflowSpecFixture),
    );
  });

  it("preserves explicit null defaults and distinguishes them from omission", () => {
    const explicitNull = cloneFixture();
    const output = (
      explicitNull.workflow_outputs as Array<Record<string, unknown>>
    )[0]!;
    output.default = null;

    expect(workflowSpecV1Schema.safeParse(explicitNull).success).toBe(true);
    expect(serializeWorkflowSemanticChecksumInput(explicitNull)).not.toBe(
      serializeWorkflowSemanticChecksumInput(workflowSpecFixture),
    );
  });

  it("returns a canonical JSON value suitable for cross-runtime golden tests", () => {
    const value = canonicalizeWorkflowSemanticValue(workflowSpecFixture);

    expect(value).toEqual(JSON.parse(JSON.stringify(value)));
    expect(Object.keys(value)).toEqual([...Object.keys(value)].sort());
  });
});

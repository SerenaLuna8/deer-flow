import { describe, expect, it } from "@rstest/core";

import {
  firstBatchNodeTitles,
  localizedNodeTitleSchema,
  nodeAvailabilitySchema,
  workflowNodeDisabledReasonCodes,
  nodeCatalogEntrySchema,
  nodeCatalogResponseV1Schema,
  nodePublicLimitsSchema,
  nodeTypeDefinitionSchema,
  portDefinitionSchema,
  resolveWorkflowInstancePortsV1,
  resolvedNodeInstancePortsV1Schema,
  resolvedWorkflowInstancePortsV1Schema,
  workflowNodeCatalogKinds,
  workflowNodeRegistryV1,
} from "@/core/project-workflows/catalog";
import {
  INLINE_SCHEMA_REF_PREFIX,
  inlineJsonSchemaRef,
  valueTypeFromJsonSchema,
} from "@/core/project-workflows/json-schema";
import registryGolden from "@/core/project-workflows/node-registry-v1.json";
import {
  conditionNodeConfigV1Schema,
  endNodeConfigV1Schema,
  httpRequestNodeConfigV1Schema,
  llmNodeConfigV1Schema,
  loopNodeConfigV1Schema,
  pythonCodeNodeConfigV1Schema,
  startNodeConfigV1Schema,
  transformNodeConfigV1Schema,
  variableAggregateNodeConfigV1Schema,
  workflowSpecV1Schema,
} from "@/core/project-workflows/types";

import instancePortsGolden from "../../../fixtures/workflows/instance-ports-v1.json";
import nodeConfigCorpus from "../../../fixtures/workflows/node-config-corpus-v1.json";
import publicProjectionsFixture from "../../../fixtures/workflows/public-projections-v1.json";
import unicodeBoundariesFixture from "../../../fixtures/workflows/unicode-code-point-boundaries-v1.json";
import runInvalidFixture from "../../../fixtures/workflows/workflow-run-invalid-v1.json";
import workflowSpecGolden from "../../../fixtures/workflows/workflow-spec-v1.json";

const NODE_KINDS = [
  "start",
  "llm",
  "condition",
  "transform",
  "variable_aggregate",
  "loop",
  "http_request",
  "python_code",
  "end",
] as const;

describe("resolved Workflow instance UUIDs", () => {
  it("parses the frozen Python resolved-port public projection", () => {
    const parsed = resolvedWorkflowInstancePortsV1Schema.parse(
      publicProjectionsFixture.resolved_ports,
    );

    expect(parsed).toEqual(publicProjectionsFixture.resolved_ports);
    expect(parsed.nodes[0]!.output_ports[0]!.value_type).toBeNull();
    expect("schema_ref" in parsed.nodes[0]!.output_ports[1]!.value_type!).toBe(
      false,
    );
  });

  it.each(runInvalidFixture.uuid_values)(
    "rejects $id as a resolved node identity",
    ({ value: uuid }) => {
      expect(
        resolvedNodeInstancePortsV1Schema.safeParse({
          node_id: uuid,
          input_ports: [],
          output_ports: [],
        }).success,
      ).toBe(false);
    },
  );
});

type NodeKind = (typeof NODE_KINDS)[number];

type MutableWorkflowFixture = {
  entry_node_id: string;
  nodes: Array<{
    id: string;
    type: string;
    config: Record<string, unknown>;
  }>;
  workflow_inputs: Array<Record<string, unknown>>;
};

const mutableInstancePortSpec = (): MutableWorkflowFixture =>
  structuredClone(
    instancePortsGolden.workflow_spec,
  ) as unknown as MutableWorkflowFixture;

const mutableNodeConfig = (
  workflow: MutableWorkflowFixture,
  nodeType: string,
): { nodeId: string; config: Record<string, unknown> } => {
  const node = workflow.nodes.find((candidate) => candidate.type === nodeType);
  if (node === undefined) {
    throw new Error(`missing ${nodeType} fixture node`);
  }
  return { nodeId: node.id, config: node.config };
};

const replaceDynamicPorts = (
  workflow: MutableWorkflowFixture,
  nodeType: "start" | "condition" | "variable_aggregate" | "loop",
  count: number,
): string => {
  if (nodeType === "start") {
    const source = structuredClone(workflow.workflow_inputs[0]!);
    workflow.workflow_inputs = Array.from({ length: count }, (_, index) => ({
      ...source,
      id: `input_${index}`,
      name: `Input ${index}`,
    }));
    return workflow.entry_node_id;
  }

  const { nodeId, config } = mutableNodeConfig(workflow, nodeType);
  if (nodeType === "condition") {
    const source = structuredClone(
      (config.branches as Array<Record<string, unknown>>)[0]!,
    );
    config.branches = Array.from({ length: count }, (_, index) => ({
      ...source,
      id: `branch-${index}`,
      output_port_id: `branch_${index}`,
    }));
  } else if (nodeType === "variable_aggregate") {
    const source = structuredClone(
      (config.groups as Array<Record<string, unknown>>)[0]!,
    );
    config.groups = Array.from({ length: count }, (_, index) => ({
      ...source,
      id: `group_${index}`,
      name: `Group ${index}`,
    }));
  } else {
    const source = structuredClone(
      (config.variables as Array<Record<string, unknown>>)[0]!,
    );
    config.variables = Array.from({ length: count }, (_, index) => ({
      ...source,
      id: `variable-${index}`,
      name: `Variable ${index}`,
      initial_input_id: `initial-${index}`,
      next_input_id: `next-${index}`,
      output_port_id: `variable_${index}`,
    }));
  }
  return nodeId;
};

const setDynamicPortTitle = (
  workflow: MutableWorkflowFixture,
  testCase: string,
  value: string,
): void => {
  if (testCase.startsWith("workflow_input_")) {
    workflow.workflow_inputs[0]![testCase.slice("workflow_input_".length)] =
      value;
    return;
  }
  const [nodeType, field] = testCase.split(":");
  if (nodeType === undefined || field === undefined) {
    throw new Error(`invalid dynamic title case: ${testCase}`);
  }
  const { config } = mutableNodeConfig(workflow, nodeType);
  const collection = {
    condition: "branches",
    variable_aggregate: "groups",
    loop: "variables",
  }[nodeType];
  if (collection === undefined) {
    throw new Error(`invalid dynamic title node: ${nodeType}`);
  }
  (config[collection] as Array<Record<string, unknown>>)[0]![field] = value;
};

const RETRY_SEMANTICS = {
  start: "pure",
  llm: "read",
  condition: "pure",
  transform: "pure",
  variable_aggregate: "pure",
  loop: "loop_body_v1",
  http_request: "http_method_v1",
  python_code: "isolated_compute",
  end: "pure",
} as const satisfies Record<NodeKind, string>;

const REQUIRED_CAPABILITIES: Partial<Record<NodeKind, readonly string[]>> = {
  http_request: ["workflow.http.use"],
  python_code: ["workflow.code.use"],
};

function valueType() {
  return {
    kind: "string" as const,
    collection: false,
    nullable: false,
  };
}

function dataPort(id = "prompt") {
  return {
    id,
    title_i18n: { "zh-CN": "提示词", "en-US": "Prompt" },
    kind: "data" as const,
    value_type: valueType(),
    cardinality: "one" as const,
    required: true,
  };
}

function controlPort(id = "next") {
  return {
    id,
    title_i18n: { "zh-CN": "下一步", "en-US": "Next" },
    kind: "control" as const,
    value_type: null,
    cardinality: "many" as const,
    required: false,
  };
}

function projectPort(port: ReturnType<typeof portDefinitionSchema.parse>) {
  if (port.kind === "control") {
    return `control:${port.id}`;
  }
  if (port.value_type === null) {
    throw new Error("resolved data port has no value type");
  }
  return `data:${port.id}:${port.value_type.kind}:${String(port.value_type.collection)}:${String(port.value_type.nullable)}`;
}

function mutateNodeConfig(
  config: Record<string, unknown>,
  mutation: string,
): void {
  if (mutation === "none") return;
  if (mutation === "add_unknown_field") {
    config.unexpected = true;
    return;
  }
  if (mutation === "transform_text_with_json_shape") {
    config.template = { version: 1, template: {}, bindings: {} };
    config.output_schema = { type: "object" };
    return;
  }
  if (mutation === "http_reversed_status_range") {
    const response = config.response as {
      accepted_statuses: Array<{ from: number; to: number }>;
    };
    response.accepted_statuses[0] = { from: 300, to: 200 };
    return;
  }
  if (mutation === "llm_null_node_output_path") {
    const messages = config.messages as Array<{
      content: { segments: unknown[] };
    }>;
    messages[0]!.content.segments = [
      {
        kind: "binding",
        value: {
          kind: "node_output",
          node_id: "00000000-0000-4000-8000-000000000002",
          output_id: "text",
          path: null,
        },
      },
    ];
    return;
  }
  if (mutation === "loop_null_schema_ref") {
    const variables = config.variables as Array<{
      value_type: { schema_ref?: unknown };
    }>;
    variables[0]!.value_type.schema_ref = null;
    return;
  }
  if (mutation === "condition_fixed_output_collision") {
    const branches = config.branches as Array<{ output_port_id: string }>;
    branches[0]!.output_port_id = "error";
    return;
  }
  if (mutation === "aggregate_fixed_output_collision") {
    const groups = config.groups as Array<{ id: string }>;
    groups[0]!.id = "next";
    return;
  }
  if (mutation === "loop_fixed_output_collision") {
    const variables = config.variables as Array<{ output_port_id: string }>;
    variables[0]!.output_port_id = "body";
    return;
  }
  throw new Error(`unknown shared node-config mutation: ${mutation}`);
}

function nodeConfigIsValid(nodeType: NodeKind, config: unknown): boolean {
  switch (nodeType) {
    case "start":
      return startNodeConfigV1Schema.safeParse(config).success;
    case "llm":
      return llmNodeConfigV1Schema.safeParse(config).success;
    case "condition":
      return conditionNodeConfigV1Schema.safeParse(config).success;
    case "transform":
      return transformNodeConfigV1Schema.safeParse(config).success;
    case "variable_aggregate":
      return variableAggregateNodeConfigV1Schema.safeParse(config).success;
    case "loop":
      return loopNodeConfigV1Schema.safeParse(config).success;
    case "http_request":
      return httpRequestNodeConfigV1Schema.safeParse(config).success;
    case "python_code":
      return pythonCodeNodeConfigV1Schema.safeParse(config).success;
    case "end":
      return endNodeConfigV1Schema.safeParse(config).success;
  }
}

function definition(nodeType: NodeKind = "start") {
  return {
    type: nodeType,
    version: 1,
    renderer_key: nodeType,
    title_i18n: firstBatchNodeTitles[nodeType],
    config_schema: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
    input_ports: [],
    output_ports: [],
    port_derivation: {
      version: 1 as const,
      input_source: "none" as const,
      output_source: {
        start: "workflow_inputs",
        llm: "llm_result_v1",
        condition: "condition_branches",
        transform: "transform_result_v1",
        variable_aggregate: "aggregate_groups",
        loop: "loop_variables",
        http_request: "http_response_body",
        python_code: "python_result_v1",
        end: "none",
      }[nodeType],
    },
    required_capabilities: [...(REQUIRED_CAPABILITIES[nodeType] ?? [])],
    retry_semantics: RETRY_SEMANTICS[nodeType],
    supports_streaming: nodeType === "llm",
  };
}

function catalogEntry(nodeType: NodeKind = "start") {
  const registryDefinition = workflowNodeRegistryV1.find(
    (candidate) => candidate.type === nodeType,
  );
  if (registryDefinition === undefined) {
    throw new Error(`missing registry definition for ${nodeType}`);
  }
  return {
    definition: registryDefinition,
    availability: { state: "enabled" as const, reason_code: null },
    public_limits: null,
  };
}

function catalogResponse(
  entries: ReturnType<typeof catalogEntry>[] = NODE_KINDS.map((nodeType) =>
    catalogEntry(nodeType),
  ),
) {
  return {
    schema_version: 1,
    catalog_generation: "a".repeat(64),
    availability_generation: "b".repeat(64),
    entries,
  };
}

describe("Workflow Node Catalog v1", () => {
  it("derives compact canonical schema identities and precise collections", () => {
    const schema = {
      type: "array",
      items: { type: "string" },
    } as const;
    const valueType = valueTypeFromJsonSchema(schema);

    expect(valueType).toEqual({
      kind: "json",
      collection: true,
      nullable: false,
      schema_ref:
        "inline-json-schema-v1:sha256:681004346c78d5f384b654c986b4eda8fea28010db3be7da70b19aa0080f3ff9",
    });
    expect(valueType.schema_ref).toMatch(
      new RegExp(`^${INLINE_SCHEMA_REF_PREFIX}[0-9a-f]{64}$`),
    );
    expect(
      inlineJsonSchemaRef({
        properties: { b: { type: "number" }, a: { type: "string" } },
        type: "object",
      }),
    ).toBe(
      inlineJsonSchemaRef({
        type: "object",
        properties: { a: { type: "string" }, b: { type: "number" } },
      }),
    );
    expect(() =>
      valueTypeFromJsonSchema({ type: "string", pattern: "(a+)+$" }),
    ).toThrow(/unsupported JSON Schema keyword/);
    expect(
      valueTypeFromJsonSchema({
        anyOf: [
          { type: "object", properties: { value: { type: "string" } } },
          { type: "null" },
        ],
      }),
    ).toMatchObject({
      kind: "json",
      collection: false,
      nullable: true,
    });
    expect(() =>
      valueTypeFromJsonSchema({ type: "string", minimum: 0 }),
    ).toThrow(/number schemas/);
  });

  it("requires at least one Condition branch, Aggregate group, and Loop variable", () => {
    const condition = structuredClone(
      workflowSpecGolden.nodes.find((node) => node.type === "condition")!
        .config,
    );
    const aggregate = structuredClone(
      workflowSpecGolden.nodes.find(
        (node) => node.type === "variable_aggregate",
      )!.config,
    );
    const loop = structuredClone(
      workflowSpecGolden.nodes.find((node) => node.type === "loop")!.config,
    );
    condition.branches = [];
    aggregate.groups = [];
    loop.variables = [];

    expect(conditionNodeConfigV1Schema.safeParse(condition).success).toBe(
      false,
    );
    expect(
      variableAggregateNodeConfigV1Schema.safeParse(aggregate).success,
    ).toBe(false);
    expect(loopNodeConfigV1Schema.safeParse(loop).success).toBe(false);
  });

  it("matches every registry field against the shared backend/frontend golden manifest", () => {
    expect(workflowNodeRegistryV1).toEqual(registryGolden);
    expect(
      workflowNodeRegistryV1.map(({ type, version }) => [type, version]),
    ).toEqual(NODE_KINDS.map((nodeType) => [nodeType, 1]));
  });

  it("declares only real fixed handles and closed versioned port derivation", () => {
    const definitions = Object.fromEntries(
      workflowNodeRegistryV1.map((item) => [item.type, item]),
    ) as Record<NodeKind, (typeof workflowNodeRegistryV1)[number]>;

    expect(definitions.start.port_derivation).toEqual({
      version: 1,
      input_source: "none",
      output_source: "workflow_inputs",
    });
    expect(definitions.condition.port_derivation.output_source).toBe(
      "condition_branches",
    );
    expect(definitions.llm.port_derivation.output_source).toBe("llm_result_v1");
    expect(definitions.transform.port_derivation.output_source).toBe(
      "transform_result_v1",
    );
    expect(definitions.variable_aggregate.port_derivation.output_source).toBe(
      "aggregate_groups",
    );
    expect(definitions.loop.port_derivation.output_source).toBe(
      "loop_variables",
    );
    expect(definitions.http_request.port_derivation.output_source).toBe(
      "http_response_body",
    );
    expect(definitions.python_code.port_derivation.output_source).toBe(
      "python_result_v1",
    );

    expect(
      definitions.condition.output_ports.map((port) => port.id),
    ).not.toContain("branch");
    expect(
      definitions.condition.output_ports.map((port) => port.id),
    ).not.toContain("else");
    expect(
      definitions.variable_aggregate.output_ports.map((port) => port.id),
    ).not.toContain("group");
    expect(definitions.loop.output_ports.map((port) => port.id)).not.toContain(
      "variable",
    );
    expect(
      definitions.http_request.output_ports.map((port) => port.id),
    ).not.toContain("body");

    expect(
      nodeTypeDefinitionSchema.safeParse({
        ...definition("start"),
        port_derivation: {
          version: 1,
          input_source: "none",
          output_source: "$.workflow_inputs[*]",
        },
      }).success,
    ).toBe(false);
  });

  it("resolves the same instance handles as the shared backend/frontend golden", () => {
    const resolved = resolveWorkflowInstancePortsV1(
      instancePortsGolden.workflow_spec,
    );
    const projection = resolved.nodes.map((node) => ({
      node_id: node.node_id,
      input_ports: node.input_ports.map(projectPort),
      output_ports: node.output_ports.map(projectPort),
    }));

    expect(projection).toEqual(instancePortsGolden.expected);
  });

  it("rejects derived collisions, duplicate ids, and pseudo-handle transitions", () => {
    for (const transition of instancePortsGolden.invalid_transitions) {
      expect(() =>
        resolveWorkflowInstancePortsV1({
          ...structuredClone(instancePortsGolden.workflow_spec),
          transitions: [transition],
        }),
      ).toThrow(/resolved control port/);
    }

    const collision = structuredClone(instancePortsGolden.workflow_spec);
    const condition = collision.nodes.find((node) => node.type === "condition");
    if (condition?.type !== "condition") {
      throw new Error("condition fixture node missing");
    }
    const conditionConfig = condition.config as {
      branches: Array<{ output_port_id: string }>;
    };
    conditionConfig.branches[0]!.output_port_id = "error";
    expect(() => resolveWorkflowInstancePortsV1(collision)).toThrow();

    const duplicate = structuredClone(instancePortsGolden.workflow_spec);
    const aggregate = duplicate.nodes.find(
      (node) => node.type === "variable_aggregate",
    );
    if (aggregate?.type !== "variable_aggregate") {
      throw new Error("aggregate fixture node missing");
    }
    const aggregateConfig = aggregate.config as {
      groups: Array<{ id: string }>;
    };
    aggregateConfig.groups[1]!.id = aggregateConfig.groups[0]!.id;
    expect(() => resolveWorkflowInstancePortsV1(duplicate)).toThrow();
  });

  it.each([
    ["start", 255],
    ["condition", 254],
    ["variable_aggregate", 254],
    ["loop", 252],
  ] as const)(
    "caps %s dynamic ports at the strict Spec boundary",
    (nodeType, maximum) => {
      const atLimit = mutableInstancePortSpec();
      const nodeId = replaceDynamicPorts(atLimit, nodeType, maximum);
      expect(workflowSpecV1Schema.safeParse(atLimit).success).toBe(true);
      const resolved = resolveWorkflowInstancePortsV1(atLimit);
      expect(
        resolved.nodes.find((node) => node.node_id === nodeId)?.output_ports,
      ).toHaveLength(256);

      const overLimit = mutableInstancePortSpec();
      replaceDynamicPorts(overLimit, nodeType, maximum + 1);
      expect(workflowSpecV1Schema.safeParse(overLimit).success).toBe(false);
      expect(() => resolveWorkflowInstancePortsV1(overLimit)).toThrow();
    },
  );

  it.each([
    "workflow_input_name",
    "workflow_input_label",
    "condition:id",
    "condition:label",
    "variable_aggregate:name",
    "loop:name",
  ])("counts %s in shared Unicode code points", (testCase) => {
    const character = unicodeBoundariesFixture.astral_character;
    const maximum = unicodeBoundariesFixture.port_title.maximum;
    const valid = mutableInstancePortSpec();
    setDynamicPortTitle(valid, testCase, character.repeat(maximum));
    expect(workflowSpecV1Schema.safeParse(valid).success).toBe(true);
    expect(() => resolveWorkflowInstancePortsV1(valid)).not.toThrow();

    const invalid = mutableInstancePortSpec();
    setDynamicPortTitle(invalid, testCase, character.repeat(maximum + 1));
    expect(workflowSpecV1Schema.safeParse(invalid).success).toBe(false);
    expect(() => resolveWorkflowInstancePortsV1(invalid)).toThrow();
  });

  it.each(["workflow_input_label", "condition:label"])(
    "rejects empty dynamic port label %s before resolution",
    (testCase) => {
      const invalid = mutableInstancePortSpec();
      setDynamicPortTitle(invalid, testCase, "");
      expect(workflowSpecV1Schema.safeParse(invalid).success).toBe(false);
      expect(() => resolveWorkflowInstancePortsV1(invalid)).toThrow();
    },
  );

  it("rejects every structural dynamic-port failure in the strict Spec", () => {
    const cases: Array<{
      name: string;
      mutate: (workflow: MutableWorkflowFixture) => void;
    }> = [
      {
        name: "start fixed collision",
        mutate: (workflow) => {
          workflow.workflow_inputs[0]!.id = "next";
        },
      },
      {
        name: "start duplicate",
        mutate: (workflow) => {
          workflow.workflow_inputs[1]!.id = workflow.workflow_inputs[0]!.id;
        },
      },
      {
        name: "condition branch fixed collision",
        mutate: (workflow) => {
          const { config } = mutableNodeConfig(workflow, "condition");
          (
            config.branches as Array<Record<string, unknown>>
          )[0]!.output_port_id = "error";
        },
      },
      {
        name: "condition else fixed collision",
        mutate: (workflow) => {
          mutableNodeConfig(workflow, "condition").config.else_output_port_id =
            "error";
        },
      },
      {
        name: "condition duplicate",
        mutate: (workflow) => {
          const branches = mutableNodeConfig(workflow, "condition").config
            .branches as Array<Record<string, unknown>>;
          branches[1]!.output_port_id = branches[0]!.output_port_id;
        },
      },
      {
        name: "condition else duplicate",
        mutate: (workflow) => {
          const config = mutableNodeConfig(workflow, "condition").config;
          config.else_output_port_id = (
            config.branches as Array<Record<string, unknown>>
          )[0]!.output_port_id;
        },
      },
      {
        name: "aggregate fixed collision",
        mutate: (workflow) => {
          const groups = mutableNodeConfig(workflow, "variable_aggregate")
            .config.groups as Array<Record<string, unknown>>;
          groups[0]!.id = "next";
        },
      },
      {
        name: "aggregate duplicate",
        mutate: (workflow) => {
          const groups = mutableNodeConfig(workflow, "variable_aggregate")
            .config.groups as Array<Record<string, unknown>>;
          groups[1]!.id = groups[0]!.id;
        },
      },
      {
        name: "loop fixed collision",
        mutate: (workflow) => {
          const variables = mutableNodeConfig(workflow, "loop").config
            .variables as Array<Record<string, unknown>>;
          variables[0]!.output_port_id = "body";
        },
      },
      {
        name: "loop duplicate",
        mutate: (workflow) => {
          const variables = mutableNodeConfig(workflow, "loop").config
            .variables as Array<Record<string, unknown>>;
          variables.push({
            ...structuredClone(variables[0]!),
            id: "duplicate-variable",
          });
        },
      },
    ];

    for (const testCase of cases) {
      const invalid = mutableInstancePortSpec();
      testCase.mutate(invalid);
      expect(
        workflowSpecV1Schema.safeParse(invalid).success,
        testCase.name,
      ).toBe(false);
      expect(
        () => resolveWorkflowInstancePortsV1(invalid),
        testCase.name,
      ).toThrow();
    }
  });

  it("matches the shared per-node valid and invalid config corpus", () => {
    const source = workflowSpecV1Schema.parse(workflowSpecGolden);
    const sourceConfigs = new Map(
      source.nodes.map((node) => [node.type, node.config]),
    );

    for (const testCase of nodeConfigCorpus.cases) {
      const nodeType = testCase.node_type as NodeKind;
      const sourceConfig = sourceConfigs.get(nodeType);
      if (sourceConfig === undefined) {
        throw new Error(`missing source config for ${nodeType}`);
      }
      const config = structuredClone(sourceConfig) as Record<string, unknown>;
      mutateNodeConfig(config, testCase.mutation);
      expect(nodeConfigIsValid(nodeType, config), testCase.name).toBe(
        testCase.valid,
      );
    }
  });

  it("rejects any config, port, title, retry, or capability drift from the canonical registry", () => {
    const configDrift = structuredClone(catalogEntry("python_code"));
    const sourceSchema = (
      configDrift.definition.config_schema.properties as Record<
        string,
        Record<string, unknown>
      >
    ).source;
    if (sourceSchema === undefined) {
      throw new Error("python_code source schema is missing");
    }
    sourceSchema.description = "drift";
    expect(nodeCatalogEntrySchema.safeParse(configDrift).success).toBe(false);

    const portDrift = structuredClone(catalogEntry("http_request"));
    portDrift.definition.output_ports[0]!.cardinality = "many";
    expect(nodeCatalogEntrySchema.safeParse(portDrift).success).toBe(false);

    const titleDrift = structuredClone(catalogEntry("start"));
    titleDrift.definition.title_i18n["en-US"] = "Renamed";
    expect(nodeCatalogEntrySchema.safeParse(titleDrift).success).toBe(false);

    const retryDrift = structuredClone(catalogEntry("start"));
    retryDrift.definition.retry_semantics = "unsafe_write";
    expect(nodeCatalogEntrySchema.safeParse(retryDrift).success).toBe(false);

    const capabilityDrift = structuredClone(catalogEntry("start"));
    capabilityDrift.definition.required_capabilities = [
      "workflow.unexpected.use",
    ];
    expect(nodeCatalogEntrySchema.safeParse(capabilityDrift).success).toBe(
      false,
    );
  });

  it("freezes the exact first-batch kinds, bilingual titles, renderers, retry policy, capabilities, and streaming", () => {
    expect(workflowNodeCatalogKinds).toEqual(NODE_KINDS);

    for (const nodeType of NODE_KINDS) {
      const parsed = nodeTypeDefinitionSchema.parse(definition(nodeType));

      expect(parsed.type).toBe(nodeType);
      expect(parsed.version).toBe(1);
      expect(parsed.renderer_key).toBe(nodeType);
      expect(parsed.title_i18n).toEqual(firstBatchNodeTitles[nodeType]);
      expect(parsed.retry_semantics).toBe(RETRY_SEMANTICS[nodeType]);
      expect(parsed.required_capabilities).toEqual(
        REQUIRED_CAPABILITIES[nodeType] ?? [],
      );
      expect(parsed.supports_streaming).toBe(nodeType === "llm");

      const invalidDefinitions = [
        { ...definition(nodeType), renderer_key: "dynamic_renderer" },
        {
          ...definition(nodeType),
          supports_streaming: nodeType !== "llm",
        },
      ];

      for (const invalid of invalidDefinitions) {
        expect(nodeTypeDefinitionSchema.safeParse(invalid).success).toBe(false);
      }
    }

    expect(
      nodeTypeDefinitionSchema.safeParse({
        ...definition("start"),
        type: "agent",
      }).success,
    ).toBe(false);
    expect(
      nodeTypeDefinitionSchema.safeParse({ ...definition("start"), version: 2 })
        .success,
    ).toBe(false);
  });

  it("uses the shared value type and JSON Schema contracts while closing node config schemas", () => {
    expect(portDefinitionSchema.parse(dataPort()).value_type).toEqual(
      valueType(),
    );
    expect(portDefinitionSchema.safeParse(controlPort()).success).toBe(true);

    expect(
      portDefinitionSchema.safeParse({
        ...controlPort(),
        value_type: valueType(),
      }).success,
    ).toBe(false);
    expect(
      portDefinitionSchema.safeParse({ ...dataPort(), value_type: null })
        .success,
    ).toBe(false);
    expect(
      portDefinitionSchema.safeParse({ ...dataPort(), id: "_private" }).success,
    ).toBe(false);
    expect(
      portDefinitionSchema.safeParse({
        ...dataPort(),
        value_type: { ...valueType(), kind: "secret" },
      }).success,
    ).toBe(false);

    for (const configSchema of [
      { type: "object", properties: {} },
      { type: "object", properties: {}, additionalProperties: true },
      { type: "array", items: {}, additionalProperties: false },
    ]) {
      expect(
        nodeTypeDefinitionSchema.safeParse({
          ...definition("transform"),
          config_schema: configSchema,
        }).success,
      ).toBe(false);
    }
  });

  it("requires unique port ids within each direction but permits the same id across directions", () => {
    const sameDirection = {
      ...definition("transform"),
      input_ports: [dataPort("value"), dataPort("value")],
    };
    expect(nodeTypeDefinitionSchema.safeParse(sameDirection).success).toBe(
      false,
    );

    const acrossDirections = {
      ...definition("transform"),
      input_ports: [dataPort("value")],
      output_ports: [dataPort("value")],
    };
    expect(nodeTypeDefinitionSchema.safeParse(acrossDirections).success).toBe(
      true,
    );

    const tooManyPorts = {
      ...definition("transform"),
      input_ports: Array.from({ length: 257 }, (_, index) =>
        dataPort(`input-${index}`),
      ),
    };
    expect(nodeTypeDefinitionSchema.safeParse(tooManyPorts).success).toBe(
      false,
    );
  });

  it("enforces safe availability reasons and the enabled/disabled reason invariant", () => {
    expect(nodeAvailabilitySchema.safeParse({ state: "enabled" }).success).toBe(
      true,
    );
    expect(
      nodeAvailabilitySchema.safeParse({ state: "enabled", reason_code: null })
        .success,
    ).toBe(true);
    expect(
      nodeAvailabilitySchema.safeParse({
        state: "disabled",
        reason_code: "WORKFLOW_CODE_PROFILE_UNAVAILABLE",
      }).success,
    ).toBe(true);

    expect(workflowNodeDisabledReasonCodes).toEqual([
      "WORKFLOW_DISABLED",
      "WORKFLOW_NODE_CAPABILITY_REQUIRED",
      "WORKFLOW_NODE_NOT_ALLOWED",
      "WORKFLOW_CODE_DISABLED",
      "WORKFLOW_CODE_PROFILE_UNAVAILABLE",
      "WORKFLOW_HTTP_DISABLED",
      "WORKFLOW_HTTP_PROFILE_UNAVAILABLE",
    ]);

    for (const invalid of [
      { state: "disabled" },
      { state: "disabled", reason_code: null },
      { state: "enabled", reason_code: "WORKFLOW_DISABLED" },
      { state: "disabled", reason_code: "aio-worker-7" },
      { state: "disabled", reason_code: "https://sandbox.internal" },
      { state: "disabled", reason_code: "provider:secret" },
      { state: "disabled", reason_code: "WORKFLOW_UNKNOWN_REASON" },
    ]) {
      expect(nodeAvailabilitySchema.safeParse(invalid).success).toBe(false);
    }
  });

  it("bounds public limits and exposes only limits applicable to each node kind", () => {
    const allowedLimits = {
      start: { max_timeout_ms: 1 },
      llm: { max_timeout_ms: 1 },
      condition: { max_timeout_ms: 1 },
      transform: { max_timeout_ms: 1 },
      variable_aggregate: {
        max_aggregate_groups: 254,
        max_aggregate_candidates: 16,
        max_timeout_ms: 1,
      },
      loop: { max_iterations: 10, max_timeout_ms: 1 },
      http_request: {
        max_timeout_ms: 1,
        max_http_request_bytes: 1_024,
        max_http_response_bytes: 2_048,
      },
      python_code: { max_source_bytes: 65_536, max_timeout_ms: 30_000 },
      end: { max_timeout_ms: 1 },
    } as const satisfies Record<NodeKind, object>;

    for (const nodeType of NODE_KINDS) {
      expect(
        nodeCatalogEntrySchema.safeParse({
          ...catalogEntry(nodeType),
          public_limits: allowedLimits[nodeType],
        }).success,
      ).toBe(true);
    }

    const invalidScopedLimits = [
      ["start", { max_source_bytes: 1 }],
      ["python_code", { max_iterations: 1 }],
      ["loop", { max_source_bytes: 1 }],
      ["variable_aggregate", { max_http_response_bytes: 1 }],
      ["http_request", { max_aggregate_groups: 1 }],
      ["http_request", { max_aggregate_candidates: 1 }],
    ] as const;

    for (const [nodeType, publicLimits] of invalidScopedLimits) {
      expect(
        nodeCatalogEntrySchema.safeParse({
          ...catalogEntry(nodeType),
          public_limits: publicLimits,
        }).success,
      ).toBe(false);
    }

    expect(
      nodePublicLimitsSchema.safeParse({ max_timeout_ms: 31_536_000_000 })
        .success,
    ).toBe(true);
    expect(
      nodePublicLimitsSchema.safeParse({
        max_http_response_bytes: 2_097_152,
      }).success,
    ).toBe(true);

    for (const invalidLimit of [0, 31_536_000_001, 1.5, "1", true]) {
      expect(
        nodePublicLimitsSchema.safeParse({ max_timeout_ms: invalidLimit })
          .success,
      ).toBe(false);
    }

    for (const [field, invalidLimit] of [
      ["max_source_bytes", 2_147_483_649],
      ["max_iterations", 1_000_001],
      ["max_aggregate_groups", 255],
      ["max_aggregate_candidates", 100_001],
      ["max_http_request_bytes", 2_147_483_649],
      ["max_http_response_bytes", 2_097_153],
    ] as const) {
      expect(
        nodePublicLimitsSchema.safeParse({ [field]: invalidLimit }).success,
      ).toBe(false);
    }
  });

  it("accepts only the closed HTTP authoring projection on the HTTP entry", () => {
    const httpAuthoring = {
      endpoints: [
        {
          id: "public-api",
          origin: "https://api.example.com",
          allowed_methods: ["GET", "POST"],
          write_idempotency: "server_derived_key",
          injection_profiles: [
            {
              id: "api-key-v1",
              scheme: "api_key",
              target_header: "x-api-key",
              credential_payload_contract: "api_key_v1",
            },
          ],
        },
      ],
    };
    const parsed = nodeCatalogEntrySchema.parse({
      ...catalogEntry("http_request"),
      http_authoring: httpAuthoring,
    });

    expect(parsed.http_authoring).toEqual(httpAuthoring);
    expect(
      nodeCatalogEntrySchema.safeParse({
        ...catalogEntry("http_request"),
        http_authoring: {
          endpoints: [
            {
              ...httpAuthoring.endpoints[0],
              origin: "https://api.example.com:443",
            },
          ],
        },
      }).success,
    ).toBe(true);
    expect(
      nodeCatalogEntrySchema.safeParse({
        ...catalogEntry("http_request"),
        http_authoring: {
          endpoints: [
            httpAuthoring.endpoints[0],
            {
              ...httpAuthoring.endpoints[0],
              id: "public-api-shadow",
              origin: "https://api.example.com:443",
              allowed_methods: ["GET"],
              write_idempotency: "none",
              injection_profiles: [],
            },
          ],
        },
      }).success,
    ).toBe(false);
    expect(
      nodeCatalogEntrySchema.safeParse({
        ...catalogEntry("start"),
        http_authoring: httpAuthoring,
      }).success,
    ).toBe(false);

    for (const forbiddenField of [
      "policy_version_id",
      "policy_checksum",
      "egress_profile_id",
      "egress_profile_digest",
      "provider_id",
      "worker_id",
      "profile_digest",
      "credential_id",
      "credential_version_id",
      "grant_id",
      "secret",
    ]) {
      for (const level of ["authoring", "endpoint", "profile"] as const) {
        const invalid = structuredClone(httpAuthoring);
        if (level === "authoring") {
          Object.assign(invalid, { [forbiddenField]: "must-not-leak" });
        } else if (level === "endpoint") {
          Object.assign(invalid.endpoints[0]!, {
            [forbiddenField]: "must-not-leak",
          });
        } else {
          Object.assign(invalid.endpoints[0]!.injection_profiles[0]!, {
            [forbiddenField]: "must-not-leak",
          });
        }
        expect(
          nodeCatalogEntrySchema.safeParse({
            ...catalogEntry("http_request"),
            http_authoring: invalid,
          }).success,
        ).toBe(false);
      }
    }

    for (const invalid of [
      {
        ...httpAuthoring,
        endpoints: [
          { ...httpAuthoring.endpoints[0], origin: "http://api.example.com" },
        ],
      },
      {
        ...httpAuthoring,
        endpoints: [
          {
            ...httpAuthoring.endpoints[0],
            allowed_methods: ["POST", "GET"],
          },
        ],
      },
      {
        ...httpAuthoring,
        endpoints: [
          {
            ...httpAuthoring.endpoints[0],
            injection_profiles: [
              {
                ...httpAuthoring.endpoints[0]!.injection_profiles[0],
                scheme: "bearer",
              },
            ],
          },
        ],
      },
    ]) {
      expect(
        nodeCatalogEntrySchema.safeParse({
          ...catalogEntry("http_request"),
          http_authoring: invalid,
        }).success,
      ).toBe(false);
    }
  });

  it("requires the exact canonical nine-entry catalog and rejects subsets, duplicates, and reordering", () => {
    expect(
      nodeCatalogResponseV1Schema.parse(catalogResponse()).entries,
    ).toHaveLength(9);
    expect(
      nodeCatalogResponseV1Schema.safeParse(catalogResponse([])).success,
    ).toBe(false);

    const capabilityFiltered = NODE_KINDS.filter(
      (nodeType) => nodeType !== "http_request" && nodeType !== "python_code",
    ).map((nodeType) => catalogEntry(nodeType));
    expect(
      nodeCatalogResponseV1Schema.safeParse(catalogResponse(capabilityFiltered))
        .success,
    ).toBe(false);

    const canonicalSubset = [
      catalogEntry("llm"),
      catalogEntry("loop"),
      catalogEntry("end"),
    ];
    expect(
      nodeCatalogResponseV1Schema.safeParse(catalogResponse(canonicalSubset))
        .success,
    ).toBe(false);
    expect(
      nodeCatalogResponseV1Schema.safeParse(
        catalogResponse([...canonicalSubset].reverse()),
      ).success,
    ).toBe(false);
    expect(
      nodeCatalogResponseV1Schema.safeParse(
        catalogResponse([catalogEntry("start"), catalogEntry("start")]),
      ).success,
    ).toBe(false);
  });

  it("requires lowercase SHA-256 generations and rejects private or unknown fields at every level", () => {
    for (const invalidGeneration of [
      "a".repeat(63),
      "a".repeat(65),
      "A".repeat(64),
      "g".repeat(64),
    ]) {
      expect(
        nodeCatalogResponseV1Schema.safeParse({
          ...catalogResponse(),
          catalog_generation: invalidGeneration,
        }).success,
      ).toBe(false);
    }

    expect(
      nodeCatalogResponseV1Schema.safeParse({
        ...catalogResponse(),
        schema_version: "1",
      }).success,
    ).toBe(false);
    expect(
      nodeCatalogResponseV1Schema.safeParse({
        ...catalogResponse(),
        schema_version: true,
      }).success,
    ).toBe(false);

    expect(
      localizedNodeTitleSchema.safeParse({
        ...firstBatchNodeTitles.start,
        internal_locale: "secret",
      }).success,
    ).toBe(false);
    expect(
      portDefinitionSchema.safeParse({
        ...dataPort(),
        source_locator: "secret",
      }).success,
    ).toBe(false);
    expect(
      nodeTypeDefinitionSchema.safeParse({
        ...definition("start"),
        dynamic_import: "package.module:Renderer",
      }).success,
    ).toBe(false);
    expect(
      nodeAvailabilitySchema.safeParse({
        state: "enabled",
        worker_id: "worker-7",
      }).success,
    ).toBe(false);
    expect(
      nodePublicLimitsSchema.safeParse({
        max_timeout_ms: 1,
        sandbox_id: "sandbox-7",
      }).success,
    ).toBe(false);
    expect(
      nodeCatalogEntrySchema.safeParse({
        ...catalogEntry(),
        provider_id: "provider-7",
      }).success,
    ).toBe(false);

    for (const field of [
      "policy_version_id",
      "worker_id",
      "provider_id",
      "sandbox_id",
      "profile_locator",
    ]) {
      expect(
        nodeCatalogResponseV1Schema.safeParse({
          ...catalogResponse(),
          [field]: "must-not-leak",
        }).success,
      ).toBe(false);
    }
  });
});

import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import {
  workflowNodeRegistryV1,
  type PortDefinition,
} from "@/core/project-workflows/catalog";
import { valueTypeFromJsonSchema } from "@/core/project-workflows/json-schema";
import type { WorkflowValidationIssueV1 } from "@/core/project-workflows/transport";
import type { JsonSchema } from "@/core/project-workflows/types";

type DraftNode = NonNullable<
  WorkflowPersistedDocumentV1["spec"]["nodes"]
>[number];
type DraftTransition = NonNullable<
  WorkflowPersistedDocumentV1["spec"]["transitions"]
>[number];

type JsonObject = Record<string, unknown>;

type LocalPort = {
  id: string;
  direction: "input" | "output";
  kind: "control" | "data";
  cardinality: "one" | "many";
  valueType: JsonObject | null;
};

type NodePorts = {
  inputs: Map<string, LocalPort>;
  outputs: Map<string, LocalPort>;
};

export type WorkflowValidationIssueTarget =
  | { kind: "document" }
  | { kind: "node"; node_id: string }
  | { kind: "edge"; edge_id: string; node_id?: string }
  | {
      kind: "port";
      node_id: string;
      edge_id?: string;
      port_id: string;
    };

const asObject = (value: unknown): JsonObject | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;

const asArray = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];

const asString = (value: unknown): string | null =>
  typeof value === "string" && value.length > 0 ? value : null;

const issue = (
  code: string,
  message: string,
  path: string[],
  coordinates: {
    node_id?: string;
    edge_id?: string;
    port_id?: string;
  } = {},
): WorkflowValidationIssueV1 => ({
  severity: "error",
  code,
  message,
  path,
  ...coordinates,
});

const issueIdentity = (value: WorkflowValidationIssueV1): string =>
  JSON.stringify([
    value.code,
    value.path,
    value.node_id ?? null,
    value.edge_id ?? null,
    value.port_id ?? null,
  ]);

const sortIssues = (
  issues: WorkflowValidationIssueV1[],
): WorkflowValidationIssueV1[] =>
  [...new Map(issues.map((item) => [issueIdentity(item), item])).values()].sort(
    (left, right) => issueIdentity(left).localeCompare(issueIdentity(right)),
  );

const localPort = (
  port: PortDefinition,
  direction: "input" | "output",
): LocalPort => ({
  id: port.id,
  direction,
  kind: port.kind,
  cardinality: port.cardinality,
  valueType: port.value_type,
});

const dynamicPort = (
  id: string,
  kind: "control" | "data",
  cardinality: "one" | "many",
  valueType: JsonObject | null,
): LocalPort => ({
  id,
  direction: "output",
  kind,
  cardinality,
  valueType,
});

const nodeType = (node: DraftNode): string | null => asString(node.type);

const nodeScope = (
  node: DraftNode,
): { kind: "root" } | { kind: "loop_body"; loop_node_id: string } | null => {
  const scope = asObject(node.scope);
  if (scope?.kind === "root") return { kind: "root" };
  if (scope?.kind === "loop_body") {
    const loopNodeId = asString(scope.loop_node_id);
    if (loopNodeId !== null) {
      return { kind: "loop_body", loop_node_id: loopNodeId };
    }
  }
  return null;
};

const scopeKey = (node: DraftNode): string | null => {
  const scope = nodeScope(node);
  if (scope?.kind === "root") return "root";
  return scope?.kind === "loop_body" ? `loop:${scope.loop_node_id}` : null;
};

function addDynamicPorts(
  node: DraftNode,
  document: WorkflowPersistedDocumentV1,
  outputs: Map<string, LocalPort>,
): void {
  const type = nodeType(node);
  const config = asObject(node.config) ?? {};
  if (type === "start") {
    for (const declaration of document.spec.workflow_inputs ?? []) {
      const id = asString(declaration.id);
      const valueType = asObject(declaration.value_type);
      if (id !== null && valueType !== null) {
        outputs.set(id, dynamicPort(id, "data", "many", valueType));
      }
    }
    return;
  }
  if (type === "condition") {
    for (const branch of asArray(config.branches)) {
      const portId = asString(asObject(branch)?.output_port_id);
      if (portId !== null) {
        outputs.set(portId, dynamicPort(portId, "control", "one", null));
      }
    }
    const fallback = asString(config.else_output_port_id);
    if (fallback !== null) {
      outputs.set(fallback, dynamicPort(fallback, "control", "one", null));
    }
    return;
  }
  if (type === "variable_aggregate") {
    for (const group of asArray(config.groups)) {
      const object = asObject(group);
      const id = asString(object?.id);
      const valueType = asObject(object?.value_type);
      if (id !== null && valueType !== null) {
        outputs.set(id, dynamicPort(id, "data", "many", valueType));
      }
    }
    return;
  }
  if (type === "loop") {
    for (const variable of asArray(config.variables)) {
      const object = asObject(variable);
      const id = asString(object?.output_port_id);
      const valueType = asObject(object?.value_type);
      if (id !== null && valueType !== null) {
        outputs.set(id, dynamicPort(id, "data", "many", valueType));
      }
    }
    return;
  }
  if (type === "llm") {
    const structuredOutput = asObject(config.structured_output);
    const exactType =
      structuredOutput?.enabled === true
        ? exactSchemaValueType(structuredOutput.schema, "object")
        : structuredOutput?.enabled === false
          ? {
              kind: "json",
              collection: false,
              nullable: true,
            }
          : null;
    outputs.set("result", dynamicPort("result", "data", "many", exactType));
    return;
  }
  if (type === "transform") {
    const exactType =
      config.mode === "text"
        ? { kind: "string", collection: false, nullable: false }
        : config.mode === "json"
          ? exactSchemaValueType(config.output_schema)
          : null;
    outputs.set("result", dynamicPort("result", "data", "many", exactType));
    return;
  }
  if (type === "http_request") {
    const response = asObject(config.response);
    const exactType =
      response?.mode === "text"
        ? { kind: "string", collection: false, nullable: false }
        : response?.mode === "json"
          ? exactSchemaValueType(response.schema)
          : null;
    outputs.set("body", dynamicPort("body", "data", "many", exactType));
    return;
  }
  if (type === "python_code") {
    outputs.set(
      "result",
      dynamicPort(
        "result",
        "data",
        "many",
        exactSchemaValueType(config.output_schema, "object"),
      ),
    );
  }
}

function exactSchemaValueType(
  value: unknown,
  requirement: "any" | "object" = "any",
): JsonObject | null {
  const schema = asObject(value);
  if (schema === null) return null;
  try {
    return valueTypeFromJsonSchema(
      schema as unknown as JsonSchema,
      requirement,
    );
  } catch {
    return null;
  }
}

function resolveLocalPorts(
  document: WorkflowPersistedDocumentV1,
  node: DraftNode,
): NodePorts | null {
  const type = nodeType(node);
  const version = node.type_version;
  const definition = workflowNodeRegistryV1.find(
    (candidate) => candidate.type === type && candidate.version === version,
  );
  if (definition === undefined) return null;
  const inputs = new Map(
    definition.input_ports.map((port) => [port.id, localPort(port, "input")]),
  );
  const outputs = new Map(
    definition.output_ports.map((port) => [port.id, localPort(port, "output")]),
  );
  addDynamicPorts(node, document, outputs);
  return { inputs, outputs };
}

const endpoint = (
  value: unknown,
): { node_id: string; port_id: string } | null => {
  const object = asObject(value);
  const nodeId = asString(object?.node_id);
  const portId = asString(object?.port_id);
  return nodeId !== null && portId !== null
    ? { node_id: nodeId, port_id: portId }
    : null;
};

function addPortEndpointIssue(
  issues: WorkflowValidationIssueV1[],
  transition: DraftTransition,
  transitionIndex: number,
  direction: "source" | "target",
  node: DraftNode,
  ports: NodePorts,
  endpointValue: { node_id: string; port_id: string },
): boolean {
  const expected = direction === "source" ? ports.outputs : ports.inputs;
  const opposite = direction === "source" ? ports.inputs : ports.outputs;
  const port = expected.get(endpointValue.port_id);
  const edgeId = asString(transition.id) ?? undefined;
  const coordinates = {
    edge_id: edgeId,
    node_id: asString(node.id) ?? undefined,
    port_id: endpointValue.port_id,
  };
  const path = [
    "spec",
    "transitions",
    String(transitionIndex),
    direction,
    "port_id",
  ];
  if (port?.kind === "control") return true;
  if (port !== undefined) {
    issues.push(
      issue(
        "WORKFLOW_CONTROL_PORT_TYPE_MISMATCH",
        "A control transition cannot connect a data port",
        path,
        coordinates,
      ),
    );
    return false;
  }
  if (opposite.has(endpointValue.port_id)) {
    issues.push(
      issue(
        "WORKFLOW_PORT_DIRECTION_INVALID",
        `Transition ${direction} references a port in the wrong direction`,
        path,
        coordinates,
      ),
    );
    return false;
  }
  issues.push(
    issue(
      direction === "source"
        ? "WORKFLOW_SOURCE_PORT_UNKNOWN"
        : "WORKFLOW_TARGET_PORT_UNKNOWN",
      `Transition ${direction} must reference a resolved control port`,
      path,
      coordinates,
    ),
  );
  return false;
}

function graphHasCycle(
  nodes: Set<string>,
  edges: Array<[string, string]>,
): boolean {
  const indegree = new Map([...nodes].map((nodeId) => [nodeId, 0]));
  const adjacency = new Map(
    [...nodes].map((nodeId) => [nodeId, [] as string[]]),
  );
  for (const [source, target] of edges) {
    if (!nodes.has(source) || !nodes.has(target)) continue;
    adjacency.get(source)!.push(target);
    indegree.set(target, indegree.get(target)! + 1);
  }
  const ready = [...nodes].filter((nodeId) => indegree.get(nodeId) === 0);
  let visited = 0;
  while (ready.length > 0) {
    const nodeId = ready.pop()!;
    visited += 1;
    for (const target of adjacency.get(nodeId)!) {
      const next = indegree.get(target)! - 1;
      indegree.set(target, next);
      if (next === 0) ready.push(target);
    }
  }
  return visited !== nodes.size;
}

function conditionIssues(
  node: DraftNode,
  index: number,
): WorkflowValidationIssueV1[] {
  if (nodeType(node) !== "condition") return [];
  const config = asObject(node.config) ?? {};
  const branches = asArray(config.branches)
    .map(asObject)
    .filter(Boolean) as JsonObject[];
  const nodeId = asString(node.id) ?? undefined;
  const issues: WorkflowValidationIssueV1[] = [];
  const seenBranchIds = new Set<string>();
  const seenPortIds = new Set<string>();
  branches.forEach((branch, branchIndex) => {
    const branchId = asString(branch.id);
    if (branchId !== null && seenBranchIds.has(branchId)) {
      issues.push(
        issue(
          "WORKFLOW_CONDITION_BRANCH_ID_DUPLICATE",
          "Condition branch identities must be unique",
          [
            "spec",
            "nodes",
            String(index),
            "config",
            "branches",
            String(branchIndex),
            "id",
          ],
          { node_id: nodeId },
        ),
      );
    }
    if (branchId !== null) seenBranchIds.add(branchId);
    const portId = asString(branch.output_port_id);
    if (portId !== null && seenPortIds.has(portId)) {
      issues.push(
        issue(
          "WORKFLOW_CONDITION_PORT_ID_DUPLICATE",
          "Condition branch and fallback port identities must be unique",
          [
            "spec",
            "nodes",
            String(index),
            "config",
            "branches",
            String(branchIndex),
            "output_port_id",
          ],
          { node_id: nodeId, port_id: portId },
        ),
      );
    }
    if (portId !== null) seenPortIds.add(portId);
  });
  const fallback = asString(config.else_output_port_id);
  if (fallback !== null && seenPortIds.has(fallback)) {
    issues.push(
      issue(
        "WORKFLOW_CONDITION_PORT_ID_DUPLICATE",
        "Condition branch and fallback port identities must be unique",
        ["spec", "nodes", String(index), "config", "else_output_port_id"],
        { node_id: nodeId, port_id: fallback },
      ),
    );
  }
  return issues;
}

function expectedInputTypes(node: DraftNode): Map<string, JsonObject> {
  const result = new Map<string, JsonObject>();
  const config = asObject(node.config) ?? {};
  const addVariables = (values: unknown, idField = "id") => {
    for (const value of asArray(values)) {
      const object = asObject(value);
      const id = asString(object?.[idField]);
      const valueType = asObject(object?.value_type);
      if (id !== null && valueType !== null) result.set(id, valueType);
    }
  };
  if (nodeType(node) === "transform" || nodeType(node) === "python_code") {
    addVariables(config.input_variables);
  } else if (nodeType(node) === "variable_aggregate") {
    for (const group of asArray(config.groups)) {
      const object = asObject(group);
      const valueType = asObject(object?.value_type);
      if (valueType === null) continue;
      for (const candidateId of asArray(object?.candidate_input_ids)) {
        const id = asString(candidateId);
        if (id !== null) result.set(id, valueType);
      }
    }
  } else if (nodeType(node) === "loop") {
    for (const variable of asArray(config.variables)) {
      const object = asObject(variable);
      const valueType = asObject(object?.value_type);
      if (valueType === null) continue;
      for (const field of ["initial_input_id", "next_input_id"] as const) {
        const id = asString(object?.[field]);
        if (id !== null) result.set(id, valueType);
      }
    }
  }
  return result;
}

const valueTypesCompatible = (
  source: JsonObject,
  target: JsonObject,
): boolean =>
  source.kind === target.kind &&
  source.collection === target.collection &&
  !(source.nullable === true && target.nullable === false) &&
  (target.schema_ref === undefined || source.schema_ref === target.schema_ref);

function bindingIssues(
  document: WorkflowPersistedDocumentV1,
  nodesById: Map<string, DraftNode>,
  portsById: Map<string, NodePorts>,
): WorkflowValidationIssueV1[] {
  const issues: WorkflowValidationIssueV1[] = [];
  for (const [nodeIndex, node] of (document.spec.nodes ?? []).entries()) {
    const nodeId = asString(node.id);
    if (nodeId === null) continue;
    const inputBindings = asObject(node.input_bindings);
    if (inputBindings === null) continue;
    const expected = expectedInputTypes(node);
    for (const [inputId, rawBinding] of Object.entries(inputBindings)) {
      const binding = asObject(rawBinding);
      if (binding?.kind !== "node_output") continue;
      const sourceNodeId = asString(binding.node_id);
      const sourcePortId = asString(binding.output_id);
      const path = [
        "spec",
        "nodes",
        String(nodeIndex),
        "input_bindings",
        inputId,
      ];
      if (sourceNodeId === null || !nodesById.has(sourceNodeId)) {
        issues.push(
          issue(
            "WORKFLOW_BINDING_SOURCE_UNKNOWN",
            "Node-output binding references an unknown node",
            path,
            { node_id: nodeId, port_id: inputId },
          ),
        );
        continue;
      }
      const output =
        sourcePortId === null
          ? undefined
          : portsById.get(sourceNodeId)?.outputs.get(sourcePortId);
      if (output?.kind !== "data") {
        issues.push(
          issue(
            "WORKFLOW_BINDING_OUTPUT_UNKNOWN",
            "Node-output binding must reference a resolved data output",
            path,
            { node_id: nodeId, port_id: inputId },
          ),
        );
        continue;
      }
      const expectedType = expected.get(inputId);
      if (
        expectedType !== undefined &&
        output.valueType !== null &&
        !valueTypesCompatible(output.valueType, expectedType)
      ) {
        issues.push(
          issue(
            "WORKFLOW_VALUE_TYPE_MISMATCH",
            "Binding output type is incompatible with the declared input type",
            path,
            { node_id: nodeId, port_id: inputId },
          ),
        );
      }
    }
  }
  return issues;
}

function visitConfigBindings(
  value: unknown,
  path: string[],
  visitor: (binding: JsonObject, path: string[]) => void,
): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      visitConfigBindings(item, [...path, String(index)], visitor),
    );
    return;
  }
  const object = asObject(value);
  if (object === null) return;
  if (object.kind === "node_output" || object.kind === "loop_variable") {
    visitor(object, path);
    return;
  }
  for (const [key, nested] of Object.entries(object)) {
    visitConfigBindings(nested, [...path, key], visitor);
  }
}

function configBindingIssues(
  document: WorkflowPersistedDocumentV1,
  nodesById: Map<string, DraftNode>,
  portsById: Map<string, NodePorts>,
): WorkflowValidationIssueV1[] {
  const issues: WorkflowValidationIssueV1[] = [];
  for (const [nodeIndex, owner] of (document.spec.nodes ?? []).entries()) {
    const ownerId = asString(owner.id);
    if (ownerId === null) continue;
    visitConfigBindings(
      owner.config,
      ["spec", "nodes", String(nodeIndex), "config"],
      (binding, path) => {
        if (binding.kind === "node_output") {
          const sourceNodeId = asString(binding.node_id);
          const sourcePortId = asString(binding.output_id);
          if (sourceNodeId === null || !nodesById.has(sourceNodeId)) {
            issues.push(
              issue(
                "WORKFLOW_BINDING_SOURCE_UNKNOWN",
                "Config binding references an unknown node",
                path,
                {
                  node_id: ownerId,
                  ...(sourcePortId === null ? {} : { port_id: sourcePortId }),
                },
              ),
            );
            return;
          }
          const output =
            sourcePortId === null
              ? undefined
              : portsById.get(sourceNodeId)?.outputs.get(sourcePortId);
          if (output?.kind !== "data") {
            issues.push(
              issue(
                "WORKFLOW_BINDING_OUTPUT_UNKNOWN",
                "Config binding must reference a resolved data output",
                path,
                {
                  node_id: ownerId,
                  ...(sourcePortId === null ? {} : { port_id: sourcePortId }),
                },
              ),
            );
          }
          return;
        }

        const loopNodeId = asString(binding.loop_node_id);
        const variableId = asString(binding.variable_id);
        const loop =
          loopNodeId === null ? undefined : nodesById.get(loopNodeId);
        const variables = asArray(asObject(loop?.config)?.variables);
        const variableExists = variables.some(
          (variable) => asString(asObject(variable)?.id) === variableId,
        );
        if (
          loopNodeId === null ||
          loop?.type !== "loop" ||
          variableId === null ||
          !variableExists
        ) {
          issues.push(
            issue(
              "WORKFLOW_LOOP_VARIABLE_BINDING_UNKNOWN",
              "Config binding references an unknown Loop variable",
              path,
              {
                node_id: ownerId,
                ...(variableId === null ? {} : { port_id: variableId }),
              },
            ),
          );
        }
      },
    );
  }
  return issues;
}

/**
 * Instant, advisory validation for the present fields of a partial Draft.
 * Missing publish-grade fields remain saveable; Gateway validation is authoritative.
 */
export function validateWorkflowDraftStructureV1(
  document: WorkflowPersistedDocumentV1,
): WorkflowValidationIssueV1[] {
  const issues: WorkflowValidationIssueV1[] = [];
  const nodes = document.spec.nodes ?? [];
  const transitions = document.spec.transitions ?? [];
  const nodesById = new Map<string, DraftNode>();
  const nodeIndexById = new Map<string, number>();
  const portsById = new Map<string, NodePorts>();

  nodes.forEach((node, index) => {
    const id = asString(node.id);
    if (id === null) {
      issues.push(
        issue(
          "WORKFLOW_NODE_ID_MISSING",
          "Node identity is required before it can be connected",
          ["spec", "nodes", String(index), "id"],
        ),
      );
      return;
    }
    if (nodesById.has(id)) {
      issues.push(
        issue(
          "WORKFLOW_NODE_ID_DUPLICATE",
          "Node identities must be unique",
          ["spec", "nodes", String(index), "id"],
          { node_id: id },
        ),
      );
      return;
    }
    nodesById.set(id, node);
    nodeIndexById.set(id, index);
    const ports = resolveLocalPorts(document, node);
    if (ports === null) {
      issues.push(
        issue(
          "WORKFLOW_NODE_TYPE_UNAVAILABLE",
          "Node type or version is unavailable in the local Registry",
          ["spec", "nodes", String(index), "type"],
          { node_id: id },
        ),
      );
    } else {
      portsById.set(id, ports);
    }

    const scope = nodeScope(node);
    if (scope === null) {
      issues.push(
        issue(
          "WORKFLOW_NODE_SCOPE_INVALID",
          "Node scope must be root or a concrete Loop body",
          ["spec", "nodes", String(index), "scope"],
          { node_id: id },
        ),
      );
    }
    issues.push(...conditionIssues(node, index));
  });

  const loopIds = new Set(
    [...nodesById.entries()]
      .filter(
        ([, node]) =>
          nodeType(node) === "loop" && nodeScope(node)?.kind === "root",
      )
      .map(([id]) => id),
  );
  for (const [id, node] of nodesById) {
    const index = nodeIndexById.get(id)!;
    const scope = nodeScope(node);
    if (nodeType(node) === "loop" && scope?.kind === "loop_body") {
      issues.push(
        issue(
          "WORKFLOW_NESTED_LOOP_FORBIDDEN",
          "Nested Loop nodes are not supported",
          ["spec", "nodes", String(index), "scope"],
          { node_id: id },
        ),
      );
    }
    if (scope?.kind === "loop_body" && !loopIds.has(scope.loop_node_id)) {
      issues.push(
        issue(
          "WORKFLOW_LOOP_BODY_OWNER_UNKNOWN",
          "Loop body scope references a missing root Loop",
          ["spec", "nodes", String(index), "scope", "loop_node_id"],
          { node_id: id },
        ),
      );
    }
    if (
      scope?.kind === "loop_body" &&
      ["start", "end"].includes(nodeType(node) ?? "")
    ) {
      issues.push(
        issue(
          "WORKFLOW_LOOP_BODY_TERMINAL_FORBIDDEN",
          "Start and End nodes cannot belong to a Loop body",
          ["spec", "nodes", String(index), "scope"],
          { node_id: id },
        ),
      );
    }
  }

  const layoutByNodeId = new Map<string, JsonObject>();
  for (const [index, layout] of (
    document.canvas.node_layouts ?? []
  ).entries()) {
    const nodeId = asString(layout.node_id);
    if (nodeId === null) continue;
    if (layoutByNodeId.has(nodeId)) {
      issues.push(
        issue(
          "WORKFLOW_CANVAS_NODE_LAYOUT_DUPLICATE",
          "Canvas node layout identities must be unique",
          ["canvas", "node_layouts", String(index), "node_id"],
          { node_id: nodeId },
        ),
      );
    }
    layoutByNodeId.set(nodeId, layout as JsonObject);
  }
  for (const [id, node] of nodesById) {
    const layout = layoutByNodeId.get(id);
    if (layout === undefined) {
      issues.push(
        issue(
          "WORKFLOW_CANVAS_NODE_LAYOUT_MISSING",
          "Every identified node needs one persisted Canvas layout",
          ["canvas", "node_layouts"],
          { node_id: id },
        ),
      );
      continue;
    }
    const parent = asString(layout.parent_node_id);
    const scope = nodeScope(node);
    const expectedParent =
      scope?.kind === "loop_body" ? scope.loop_node_id : null;
    if (parent !== expectedParent) {
      issues.push(
        issue(
          "WORKFLOW_CANVAS_SCOPE_MISMATCH",
          "Canvas parent must exactly match the authored semantic scope",
          [
            "canvas",
            "node_layouts",
            String(
              (document.canvas.node_layouts ?? []).indexOf(layout as never),
            ),
            "parent_node_id",
          ],
          { node_id: id },
        ),
      );
    }
  }

  const seenEdgeIds = new Set<string>();
  const seenSemanticEdges = new Set<string>();
  const sourceCounts = new Map<string, number>();
  const graphEdgesByScope = new Map<string, Array<[string, string]>>();
  transitions.forEach((transition, index) => {
    const edgeId = asString(transition.id) ?? undefined;
    if (edgeId !== undefined && seenEdgeIds.has(edgeId)) {
      issues.push(
        issue(
          "WORKFLOW_TRANSITION_ID_DUPLICATE",
          "Transition identities must be unique",
          ["spec", "transitions", String(index), "id"],
          { edge_id: edgeId },
        ),
      );
    }
    if (edgeId !== undefined) seenEdgeIds.add(edgeId);
    const source = endpoint(transition.source);
    const target = endpoint(transition.target);
    const sourceNode =
      source === null ? undefined : nodesById.get(source.node_id);
    const targetNode =
      target === null ? undefined : nodesById.get(target.node_id);
    if (source === null || sourceNode === undefined) {
      issues.push(
        issue(
          "WORKFLOW_TRANSITION_SOURCE_UNKNOWN",
          "Transition source node is unknown",
          ["spec", "transitions", String(index), "source"],
          { edge_id: edgeId },
        ),
      );
    }
    if (target === null || targetNode === undefined) {
      issues.push(
        issue(
          "WORKFLOW_TRANSITION_TARGET_UNKNOWN",
          "Transition target node is unknown",
          ["spec", "transitions", String(index), "target"],
          { edge_id: edgeId },
        ),
      );
    }
    if (
      source === null ||
      target === null ||
      sourceNode === undefined ||
      targetNode === undefined
    ) {
      return;
    }
    const sourceValid = addPortEndpointIssue(
      issues,
      transition,
      index,
      "source",
      sourceNode,
      portsById.get(source.node_id) ?? {
        inputs: new Map(),
        outputs: new Map(),
      },
      source,
    );
    const targetValid = addPortEndpointIssue(
      issues,
      transition,
      index,
      "target",
      targetNode,
      portsById.get(target.node_id) ?? {
        inputs: new Map(),
        outputs: new Map(),
      },
      target,
    );
    if (source.node_id === target.node_id) {
      issues.push(
        issue(
          "WORKFLOW_CONTROL_SELF_LOOP",
          "Self-loop control transitions are forbidden",
          ["spec", "transitions", String(index)],
          { edge_id: edgeId, node_id: source.node_id },
        ),
      );
    }
    if (nodeType(sourceNode) === "loop" && source.port_id === "body") {
      issues.push(
        issue(
          "WORKFLOW_LOOP_BODY_ROUTE_AUTHORED",
          "Loop body entry is Compiler-managed and cannot be authored",
          ["spec", "transitions", String(index), "source", "port_id"],
          {
            edge_id: edgeId,
            node_id: source.node_id,
            port_id: source.port_id,
          },
        ),
      );
    }
    const semanticIdentity = `${source.node_id}\0${source.port_id}\0${target.node_id}\0${target.port_id}`;
    if (seenSemanticEdges.has(semanticIdentity)) {
      issues.push(
        issue(
          "WORKFLOW_CONTROL_EDGE_DUPLICATE",
          "Duplicate semantic control transitions are forbidden",
          ["spec", "transitions", String(index)],
          { edge_id: edgeId },
        ),
      );
    }
    seenSemanticEdges.add(semanticIdentity);
    const sourceCountKey = `${source.node_id}\0${source.port_id}`;
    sourceCounts.set(
      sourceCountKey,
      (sourceCounts.get(sourceCountKey) ?? 0) + 1,
    );
    const sourceScope = scopeKey(sourceNode);
    const targetScope = scopeKey(targetNode);
    if (
      sourceScope !== null &&
      targetScope !== null &&
      sourceScope !== targetScope
    ) {
      issues.push(
        issue(
          "WORKFLOW_CROSS_SCOPE_TRANSITION",
          "Authored transitions cannot cross root and Loop-body scopes",
          ["spec", "transitions", String(index)],
          { edge_id: edgeId },
        ),
      );
    } else if (
      sourceValid &&
      targetValid &&
      sourceScope !== null &&
      sourceScope === targetScope
    ) {
      const edges = graphEdgesByScope.get(sourceScope) ?? [];
      edges.push([source.node_id, target.node_id]);
      graphEdgesByScope.set(sourceScope, edges);
    }
  });

  for (const [nodeId, ports] of portsById) {
    for (const port of ports.outputs.values()) {
      if (
        port.kind === "control" &&
        port.cardinality === "one" &&
        (sourceCounts.get(`${nodeId}\0${port.id}`) ?? 0) > 1
      ) {
        issues.push(
          issue(
            "WORKFLOW_SOURCE_PORT_CARDINALITY",
            "Control output exceeds its one-edge cardinality",
            ["spec", "transitions"],
            { node_id: nodeId, port_id: port.id },
          ),
        );
      }
    }
  }

  const graphNodesByScope = new Map<string, Set<string>>();
  for (const [id, node] of nodesById) {
    const scope = scopeKey(node);
    if (scope === null) continue;
    const graphNodes = graphNodesByScope.get(scope) ?? new Set<string>();
    graphNodes.add(id);
    graphNodesByScope.set(scope, graphNodes);
  }
  for (const [scope, graphNodes] of graphNodesByScope) {
    if (graphHasCycle(graphNodes, graphEdgesByScope.get(scope) ?? [])) {
      issues.push(
        issue(
          "WORKFLOW_AUTHORED_CYCLE",
          scope === "root"
            ? "Root authored transitions must form a DAG"
            : "Loop body authored transitions must form a DAG",
          ["spec", "transitions"],
          scope === "root" ? {} : { node_id: scope.slice("loop:".length) },
        ),
      );
    }
  }

  for (const [loopId, loop] of [...nodesById].filter(
    ([, node]) => nodeType(node) === "loop" && nodeScope(node)?.kind === "root",
  )) {
    const config = asObject(loop.config) ?? {};
    for (const field of ["body_entry_node_id", "body_exit_node_id"] as const) {
      const referencedId = asString(config[field]);
      if (referencedId === null) continue;
      const referenced = nodesById.get(referencedId);
      const referencedScope =
        referenced === undefined ? null : nodeScope(referenced);
      if (
        referenced === undefined ||
        referencedScope?.kind !== "loop_body" ||
        referencedScope.loop_node_id !== loopId
      ) {
        issues.push(
          issue(
            "WORKFLOW_LOOP_ENTRY_EXIT_INVALID",
            "Loop entry and exit must identify nodes in its own body",
            [
              "spec",
              "nodes",
              String(nodeIndexById.get(loopId)),
              "config",
              field,
            ],
            { node_id: loopId },
          ),
        );
      }
    }
  }

  issues.push(...bindingIssues(document, nodesById, portsById));
  issues.push(...configBindingIssues(document, nodesById, portsById));
  return sortIssues(issues);
}

/** Validate one proposed control edge without applying any Draft mutation. */
export function validateWorkflowControlConnectionV1(
  document: WorkflowPersistedDocumentV1,
  transition: DraftTransition,
): WorkflowValidationIssueV1[] {
  const preflight: WorkflowValidationIssueV1[] = [];
  const edgeId = asString(transition.id);
  const source = endpoint(transition.source);
  const target = endpoint(transition.target);
  const existingTransitions = document.spec.transitions ?? [];
  if (
    edgeId !== null &&
    existingTransitions.some((item) => item.id === edgeId)
  ) {
    preflight.push(
      issue(
        "WORKFLOW_TRANSITION_ID_DUPLICATE",
        "Transition identity already exists",
        ["spec", "transitions"],
        { edge_id: edgeId },
      ),
    );
  }
  if (source !== null && target !== null) {
    const semanticDuplicate = existingTransitions.some((item) => {
      const existingSource = endpoint(item.source);
      const existingTarget = endpoint(item.target);
      return (
        existingSource?.node_id === source.node_id &&
        existingSource.port_id === source.port_id &&
        existingTarget?.node_id === target.node_id &&
        existingTarget.port_id === target.port_id
      );
    });
    if (semanticDuplicate) {
      preflight.push(
        issue(
          "WORKFLOW_CONTROL_EDGE_DUPLICATE",
          "Duplicate semantic control transitions are forbidden",
          ["spec", "transitions"],
          { ...(edgeId === null ? {} : { edge_id: edgeId }) },
        ),
      );
    }
    const sourceNode = (document.spec.nodes ?? []).find(
      (node) => node.id === source.node_id,
    );
    const sourcePort =
      sourceNode === undefined
        ? undefined
        : resolveLocalPorts(document, sourceNode)?.outputs.get(source.port_id);
    const existingSourceCount = existingTransitions.filter((item) => {
      const existingSource = endpoint(item.source);
      return (
        existingSource?.node_id === source.node_id &&
        existingSource.port_id === source.port_id
      );
    }).length;
    if (
      sourcePort?.kind === "control" &&
      sourcePort.cardinality === "one" &&
      existingSourceCount >= 1
    ) {
      preflight.push(
        issue(
          "WORKFLOW_SOURCE_PORT_CARDINALITY",
          "Control output already reached its one-edge cardinality",
          ["spec", "transitions"],
          {
            ...(edgeId === null ? {} : { edge_id: edgeId }),
            node_id: source.node_id,
            port_id: source.port_id,
          },
        ),
      );
    }
  }

  const before = new Set(
    validateWorkflowDraftStructureV1(document).map(issueIdentity),
  );
  const candidate = structuredClone(document);
  candidate.spec.transitions = [
    ...(candidate.spec.transitions ?? []),
    transition,
  ];
  candidate.canvas.edge_layouts = [
    ...(candidate.canvas.edge_layouts ?? []),
    ...(edgeId === null
      ? []
      : [{ edge_id: edgeId, routing: "smoothstep" as const }]),
  ];
  return sortIssues([
    ...preflight,
    ...validateWorkflowDraftStructureV1(candidate).filter(
      (item) => !before.has(issueIdentity(item)) || item.edge_id === edgeId,
    ),
  ]);
}

/** Pure selection/focus projection used by Canvas and accessible issue lists. */
export function workflowValidationIssueTarget(
  issue: WorkflowValidationIssueV1,
): WorkflowValidationIssueTarget {
  if (issue.port_id !== undefined && issue.port_id !== null && issue.node_id) {
    return {
      kind: "port",
      node_id: issue.node_id,
      ...(issue.edge_id ? { edge_id: issue.edge_id } : {}),
      port_id: issue.port_id,
    };
  }
  if (issue.edge_id !== undefined && issue.edge_id !== null) {
    return {
      kind: "edge",
      edge_id: issue.edge_id,
      ...(issue.node_id ? { node_id: issue.node_id } : {}),
    };
  }
  if (issue.node_id !== undefined && issue.node_id !== null) {
    return { kind: "node", node_id: issue.node_id };
  }
  return { kind: "document" };
}

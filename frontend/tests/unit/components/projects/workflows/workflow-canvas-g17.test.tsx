import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, rs } from "@rstest/core";
import { ReactFlowProvider } from "@xyflow/react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  focusWorkflowTargetElement,
  focusWorkflowValidationTarget,
  focusWorkflowValidationTargetInCanvas,
  WORKFLOW_EDGE_TYPES,
  WORKFLOW_NODE_TYPES,
} from "@/components/projects/workflows/canvas/workflow-canvas";
import {
  projectWorkflowFlow,
  workflowConnectionAllowed,
} from "@/components/projects/workflows/canvas/workflow-canvas-adapter";
import {
  handleWorkflowPortKeyDown,
  WorkflowNodeCard,
} from "@/components/projects/workflows/nodes/workflow-node";
import {
  nodeCatalogResponseV1Schema,
  workflowNodeCatalogKinds,
  workflowNodeRegistryV1,
} from "@/core/project-workflows/catalog";

import canvasFixture from "../../../../fixtures/workflows/canvas-document-v1.json";
import specFixture from "../../../../fixtures/workflows/workflow-spec-v1.json";

const LOOP_ID = "00000000-0000-4000-8000-000000000006";
const LOOP_CHILD_ID = "00000000-0000-4000-8000-000000000009";
const CONDITION_ID = "00000000-0000-4000-8000-000000000003";

const enabledCatalog = () =>
  nodeCatalogResponseV1Schema.parse({
    schema_version: 1,
    catalog_generation: "a".repeat(64),
    availability_generation: "b".repeat(64),
    entries: workflowNodeRegistryV1.map((definition) => ({
      definition,
      availability: { state: "enabled" },
    })),
  });

describe("G17 Workflow React Flow adapter", () => {
  it("projects all nine first-batch node categories through static registries", () => {
    const projection = projectWorkflowFlow(specFixture, canvasFixture, {
      catalog: enabledCatalog(),
    });

    expect(new Set(projection.nodes.map((node) => node.data.nodeKind))).toEqual(
      new Set(workflowNodeCatalogKinds),
    );
    expect(Object.keys(WORKFLOW_NODE_TYPES)).toEqual([
      ...workflowNodeCatalogKinds,
      "unsupported",
    ]);
    expect(Object.keys(WORKFLOW_EDGE_TYPES)).toEqual(["workflow"]);
    expect(projection.edges.every((edge) => edge.animated === false)).toBe(
      true,
    );
  });

  it("keeps partial and future nodes visible but fail-closed", () => {
    const partial = {
      schema_version: 1 as const,
      nodes: [
        {
          id: "10000000-0000-4000-8000-000000000001",
          type: "start",
          type_version: 1,
          scope: { kind: "root" },
          custom_label: "尚未完成",
          config: {},
        },
        {
          id: "10000000-0000-4000-8000-000000000002",
          type: "human_input",
          type_version: 2,
          scope: { kind: "root" },
          custom_label: "未来节点",
          config: { secret: "must-not-leak" },
        },
        {
          id: "10000000-0000-4000-8000-000000000003",
          type: "llm",
          scope: { kind: "root" },
          custom_label: "缺少版本",
          config: {},
        },
        {
          id: "10000000-0000-4000-8000-000000000004",
          type: "start",
          type_version: 2,
          scope: { kind: "root" },
          custom_label: "未来版本",
          config: {},
        },
      ],
      transitions: null,
      workflow_inputs: null,
      workflow_outputs: null,
      credential_slots: null,
    };

    const projection = projectWorkflowFlow(
      partial,
      { schema_version: 1, node_layouts: null, edge_layouts: null },
      { catalog: enabledCatalog() },
    );

    expect(projection.nodes).toHaveLength(4);
    expect(projection.nodes.map((node) => node.type)).toEqual([
      "unsupported",
      "unsupported",
      "unsupported",
      "unsupported",
    ]);
    expect(
      projection.nodes.every(
        (node) => node.data.readOnly && node.data.disabled,
      ),
    ).toBe(true);
    expect(projection.nodes.map((node) => node.data.supportState)).toEqual([
      "incomplete",
      "unsupported",
      "incomplete",
      "unsupported",
    ]);
    expect(JSON.stringify(projection)).not.toContain("must-not-leak");
  });

  it("keeps a schema-valid but structurally invalid Draft inspectable", () => {
    const invalid = structuredClone(specFixture);
    invalid.transitions[0]!.source.port_id = "missing-port";

    expect(() =>
      projectWorkflowFlow(invalid, canvasFixture, {
        catalog: enabledCatalog(),
      }),
    ).not.toThrow();
    const projection = projectWorkflowFlow(invalid, canvasFixture, {
      catalog: enabledCatalog(),
    });
    expect(projection.nodes).toHaveLength(9);
    expect(projection.edges[0]?.sourceHandle).toBe("missing-port");
  });

  it("uses stable port ids for handles and transition endpoints", () => {
    const projection = projectWorkflowFlow(specFixture, canvasFixture, {
      catalog: enabledCatalog(),
    });
    const condition = projection.nodes.find((node) => node.id === CONDITION_ID);

    expect(condition?.data.outputPorts.map((port) => port.id)).toEqual([
      "error",
      "long",
      "short",
    ]);
    expect(projection.edges[0]).toMatchObject({
      sourceHandle: "next",
      targetHandle: "in",
    });

    const changed = structuredClone(specFixture);
    const changedCondition = changed.nodes.find(
      (node) => node.id === CONDITION_ID,
    );
    if (changedCondition?.type !== "condition") {
      throw new Error("condition fixture missing");
    }
    const changedBranches = changedCondition.config.branches;
    if (!changedBranches?.[0]) throw new Error("condition branch missing");
    changedBranches[0].output_port_id = "long-v2";
    const changedProjection = projectWorkflowFlow(changed, canvasFixture, {
      catalog: enabledCatalog(),
    });
    const changedNode = changedProjection.nodes.find(
      (node) => node.id === CONDITION_ID,
    );
    expect(changedNode?.data.portSignature).not.toBe(
      condition?.data.portSignature,
    );
    expect(changedNode?.data.outputPorts.map((port) => port.id)).toContain(
      "long-v2",
    );
  });

  it("orders Loop parents before children and derives parentId only from Spec scope", () => {
    const spec = structuredClone(specFixture);
    const childIndex = spec.nodes.findIndex(
      (node) => node.id === LOOP_CHILD_ID,
    );
    const [child] = spec.nodes.splice(childIndex, 1);
    spec.nodes.unshift(child!);
    const canvas = structuredClone(canvasFixture);
    const childLayout = canvas.node_layouts.find(
      (layout) => layout.node_id === LOOP_CHILD_ID,
    );
    if (childLayout) {
      childLayout.parent_node_id = "00000000-0000-4000-8000-000000000001";
    }

    const projection = projectWorkflowFlow(spec, canvas, {
      catalog: enabledCatalog(),
    });
    const parentIndex = projection.nodes.findIndex(
      (node) => node.id === LOOP_ID,
    );
    const projectedChildIndex = projection.nodes.findIndex(
      (node) => node.id === LOOP_CHILD_ID,
    );
    const projectedChild = projection.nodes[projectedChildIndex];

    expect(parentIndex).toBeLessThan(projectedChildIndex);
    expect(projectedChild).toMatchObject({
      parentId: LOOP_ID,
      extent: "parent",
    });
  });

  it("does not project authored code, HTTP request material, runtime, or private fields", () => {
    const projection = projectWorkflowFlow(specFixture, canvasFixture, {
      catalog: enabledCatalog(),
    });
    const serialized = JSON.stringify(projection);
    const python = specFixture.nodes.find(
      (node) => node.type === "python_code",
    );
    const http = specFixture.nodes.find((node) => node.type === "http_request");

    expect(serialized).not.toContain(python?.config.source ?? "source-secret");
    expect(serialized).not.toContain(
      http?.config.base_origin ?? "origin-secret",
    );
    expect(serialized).not.toContain(
      http?.config.path_template ?? "path-secret",
    );
    expect(serialized).not.toContain("请生成摘要。");
    expect(serialized).not.toContain("input_bindings");
    expect(serialized).not.toContain("execution_policy");
    expect(serialized).not.toContain("runtime_projection");
  });

  it("returns a disposable deep projection with no mutable references to Draft input", () => {
    const spec = structuredClone(specFixture);
    const canvas = structuredClone(canvasFixture);
    const projection = projectWorkflowFlow(spec, canvas, {
      catalog: enabledCatalog(),
    });
    const condition = projection.nodes.find((node) => node.id === CONDITION_ID);
    if (!condition) throw new Error("condition projection missing");

    const originalPosition = structuredClone(condition.position);
    const originalPorts = condition.data.outputPorts.map((port) => port.id);
    const sourceNode = spec.nodes.find((node) => node.id === CONDITION_ID);
    if (sourceNode?.type !== "condition") {
      throw new Error("condition source fixture missing");
    }
    const sourceBranches = sourceNode.config.branches;
    if (!sourceBranches?.[0]) throw new Error("condition branch missing");
    sourceBranches[0].output_port_id = "changed-after-project";
    const sourceLayout = canvas.node_layouts.find(
      (layout) => layout.node_id === LOOP_ID,
    );
    if (sourceLayout) sourceLayout.position.x = 99999;

    expect(condition.position).toEqual(originalPosition);
    expect(condition.data.outputPorts.map((port) => port.id)).toEqual(
      originalPorts,
    );

    condition.position.x = -99999;
    (
      condition.data.outputPorts as unknown as Array<{
        id: string;
      }>
    )[0]!.id = "projection-only-change";
    expect(sourceBranches[0].output_port_id).toBe("changed-after-project");
    expect(
      canvas.node_layouts.find((layout) => layout.node_id === CONDITION_ID)
        ?.position.x,
    ).not.toBe(-99999);
  });

  it("applies readOnly and Catalog disabled state without deleting authored nodes", () => {
    const catalog = enabledCatalog();
    const python = catalog.entries.find(
      (entry) => entry.definition.type === "python_code",
    );
    if (!python) throw new Error("python catalog entry missing");
    python.availability = {
      state: "disabled",
      reason_code: "WORKFLOW_CODE_DISABLED",
    };

    const disabledProjection = projectWorkflowFlow(specFixture, canvasFixture, {
      catalog,
    });
    expect(
      disabledProjection.nodes.find(
        (node) => node.data.nodeKind === "python_code",
      )?.data,
    ).toMatchObject({
      disabled: true,
      readOnly: true,
      availabilityReason: "WORKFLOW_CODE_DISABLED",
    });

    const readOnlyProjection = projectWorkflowFlow(specFixture, canvasFixture, {
      catalog: enabledCatalog(),
      readOnly: true,
    });
    expect(
      readOnlyProjection.nodes.every(
        (node) => node.data.readOnly && !node.connectable && !node.draggable,
      ),
    ).toBe(true);
  });

  it("delegates connection feedback to the injected validator without claiming publish authority", () => {
    const validator = rs.fn(() => true);
    const connection = {
      source: "00000000-0000-4000-8000-000000000001",
      sourceHandle: "next",
      target: "00000000-0000-4000-8000-000000000002",
      targetHandle: "in",
    };

    expect(workflowConnectionAllowed(connection, false, validator)).toBe(true);
    expect(validator).toHaveBeenCalledWith(connection);
    expect(workflowConnectionAllowed(connection, true, validator)).toBe(false);
    expect(validator).toHaveBeenCalledTimes(1);
  });

  it("projects issue targets onto the exact node, port, and edge selection", () => {
    const portProjection = projectWorkflowFlow(specFixture, canvasFixture, {
      catalog: enabledCatalog(),
      focusTarget: {
        kind: "port",
        node_id: CONDITION_ID,
        port_id: "long",
      },
    });
    const condition = portProjection.nodes.find(
      (node) => node.id === CONDITION_ID,
    );
    expect(condition).toMatchObject({
      selected: true,
      data: { focusedPortId: "long" },
    });
    expect(portProjection.nodes.filter((node) => node.selected)).toHaveLength(
      1,
    );

    const edgeProjection = projectWorkflowFlow(specFixture, canvasFixture, {
      catalog: enabledCatalog(),
      focusTarget: { kind: "edge", edge_id: "transition-1" },
    });
    expect(
      edgeProjection.edges.find((edge) => edge.id === "transition-1")?.selected,
    ).toBe(true);
  });

  it("centers node and port targets and fits edge/document targets through the live instance", async () => {
    const setCenter = rs.fn(async () => true);
    const fitView = rs.fn(async () => true);
    const instance = {
      fitView,
      getEdges: () => [
        {
          id: "transition-1",
          source: "node-source",
          target: "node-target",
        },
      ],
      getInternalNode: (id: string) => ({
        id,
        internals: { positionAbsolute: { x: 100, y: 200 } },
        measured: { width: 80, height: 40 },
      }),
      setCenter,
    };

    await focusWorkflowValidationTarget(instance, {
      kind: "node",
      node_id: CONDITION_ID,
    });
    expect(setCenter).toHaveBeenLastCalledWith(140, 220, {
      duration: 240,
      zoom: 1.2,
    });
    await focusWorkflowValidationTarget(instance, {
      kind: "port",
      node_id: CONDITION_ID,
      port_id: "long",
    });
    expect(setCenter).toHaveBeenCalledTimes(2);

    await focusWorkflowValidationTarget(instance, {
      kind: "edge",
      edge_id: "transition-1",
    });
    expect(fitView).toHaveBeenLastCalledWith({
      duration: 240,
      maxZoom: 1.2,
      nodes: [{ id: "node-source" }, { id: "node-target" }],
      padding: 0.35,
    });
    await focusWorkflowValidationTarget(instance, { kind: "document" });
    expect(fitView).toHaveBeenLastCalledWith({
      duration: 240,
      maxZoom: 1.2,
      padding: 0.2,
    });
  });

  it("focuses exact node, edge, port, and document targets inside one Canvas root only", async () => {
    const calls: string[] = [];
    const focusable = (
      kind: "edge" | "node" | "port",
      coordinates: {
        edgeId?: string;
        nodeId?: string;
        portId?: string;
      },
    ) => ({
      dataset: {
        workflowEdgeId: coordinates.edgeId,
        workflowFocusKind: kind,
        workflowNodeId: coordinates.nodeId,
        workflowPortId: coordinates.portId,
      },
      focus: rs.fn(() => calls.push(`focus:${kind}`)),
    });
    const node = focusable("node", { nodeId: CONDITION_ID });
    const edge = focusable("edge", { edgeId: "transition-1" });
    const port = focusable("port", {
      nodeId: CONDITION_ID,
      portId: "long",
    });
    const root = {
      focus: rs.fn(() => calls.push("focus:document")),
      querySelectorAll: rs.fn(() => [node, edge, port]),
    };

    expect(
      focusWorkflowTargetElement(root, {
        kind: "node",
        node_id: CONDITION_ID,
      }),
    ).toBe(true);
    expect(node.focus).toHaveBeenCalledTimes(1);
    expect(
      focusWorkflowTargetElement(root, {
        kind: "edge",
        edge_id: "transition-1",
      }),
    ).toBe(true);
    expect(edge.focus).toHaveBeenCalledTimes(1);
    expect(
      focusWorkflowTargetElement(root, {
        kind: "port",
        node_id: CONDITION_ID,
        port_id: "long",
      }),
    ).toBe(true);
    expect(port.focus).toHaveBeenCalledTimes(1);
    expect(focusWorkflowTargetElement(root, { kind: "document" })).toBe(true);
    expect(root.focus).toHaveBeenCalledTimes(1);

    const beforeMissing = [...calls];
    expect(
      focusWorkflowTargetElement(root, {
        kind: "port",
        node_id: CONDITION_ID,
        port_id: "missing",
      }),
    ).toBe(false);
    expect(calls).toEqual(beforeMissing);

    const orderedRoot = {
      focus: rs.fn(() => calls.push("focus:document-after-camera")),
      querySelectorAll: rs.fn(() => []),
    };
    await focusWorkflowValidationTargetInCanvas(
      {
        fitView: rs.fn(async () => {
          calls.push("camera");
          return true;
        }),
        getEdges: () => [],
        getInternalNode: () => undefined,
        setCenter: rs.fn(async () => true),
      },
      orderedRoot,
      { kind: "document" },
    );
    expect(calls.slice(-2)).toEqual(["camera", "focus:document-after-camera"]);
  });

  it("does not steal focus after an async camera target becomes stale", async () => {
    let releaseCamera: ((value: boolean) => void) | undefined;
    const camera = new Promise<boolean>((resolve) => {
      releaseCamera = resolve;
    });
    const root = {
      focus: rs.fn(),
      querySelectorAll: rs.fn(() => []),
    };
    let current = true;
    const focusResult = focusWorkflowValidationTargetInCanvas(
      {
        fitView: rs.fn(() => camera),
        getEdges: () => [],
        getInternalNode: () => undefined,
        setCenter: rs.fn(async () => true),
      },
      root,
      { kind: "document" },
      () => current,
    );

    current = false;
    releaseCamera?.(true);
    expect(await focusResult).toBe(false);
    expect(root.focus).not.toHaveBeenCalled();
    expect(root.querySelectorAll).not.toHaveBeenCalled();
  });

  it("activates a focusable Handle with Enter or Space only when enabled", () => {
    const activate = rs.fn();
    const port = {
      id: "long",
      label: "Long",
      kind: "control" as const,
      cardinality: "one" as const,
      direction: "output" as const,
    };
    const keyboardEvent = (key: string) => ({
      key,
      preventDefault: rs.fn(),
      stopPropagation: rs.fn(),
    });

    const ignored = keyboardEvent("Escape");
    expect(
      handleWorkflowPortKeyDown(ignored, CONDITION_ID, port, true, activate),
    ).toBe(false);
    const enter = keyboardEvent("Enter");
    expect(
      handleWorkflowPortKeyDown(enter, CONDITION_ID, port, true, activate),
    ).toBe(true);
    expect(enter.preventDefault).toHaveBeenCalledTimes(1);
    expect(enter.stopPropagation).toHaveBeenCalledTimes(1);
    expect(activate).toHaveBeenLastCalledWith(CONDITION_ID, port);

    const space = keyboardEvent(" ");
    expect(
      handleWorkflowPortKeyDown(space, CONDITION_ID, port, true, activate),
    ).toBe(true);
    const disabled = keyboardEvent("Enter");
    expect(
      handleWorkflowPortKeyDown(disabled, CONDITION_ID, port, false, activate),
    ).toBe(false);
    expect(disabled.preventDefault).not.toHaveBeenCalled();
    expect(activate).toHaveBeenCalledTimes(2);
  });

  it("renders readable node, port, and non-color-only state labels", () => {
    const node = projectWorkflowFlow(specFixture, canvasFixture, {
      catalog: enabledCatalog(),
    }).nodes.find((candidate) => candidate.data.nodeKind === "condition");
    if (!node) throw new Error("condition projection missing");

    const html = renderToStaticMarkup(
      <ReactFlowProvider>
        <WorkflowNodeCard
          dragging={false}
          draggable={node.draggable ?? false}
          deletable={node.deletable ?? false}
          id={node.id}
          isConnectable
          positionAbsoluteX={0}
          positionAbsoluteY={0}
          selected={false}
          selectable={node.selectable ?? true}
          type={node.type}
          zIndex={0}
          data={node.data}
        />
      </ReactFlowProvider>,
    );

    expect(html).toContain('aria-label="工作流节点：条件分支"');
    expect(html).toContain("状态：可用");
    expect(html).toContain("输出端口：long");
    expect(html).toContain("输出端口：short");
    expect(html).toContain('tabindex="0"');
    expect(html).toContain('data-workflow-focus-kind="node"');
    expect(html).toContain(`data-workflow-node-id="${CONDITION_ID}"`);
    expect(html).toContain('data-workflow-focus-kind="port"');
    expect(html).toContain('data-workflow-port-id="long"');
  });

  it("keeps renderer registries module-level and refreshes internals from a port signature", () => {
    const canvasSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/workflows/canvas/workflow-canvas.tsx",
      ),
      "utf8",
    );
    const nodeSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/workflows/nodes/workflow-node.tsx",
      ),
      "utf8",
    );

    expect(canvasSource).toContain("export const WORKFLOW_NODE_TYPES");
    expect(canvasSource).toContain("export const WORKFLOW_EDGE_TYPES");
    expect(canvasSource).toContain("useCallback");
    expect(nodeSource).toContain("useUpdateNodeInternals");
    expect(nodeSource).toContain("data.portSignature");
    expect(canvasSource).toContain("focusWorkflowValidationTarget");
    expect(canvasSource).toContain("focusWorkflowValidationTargetInCanvas");
    expect(nodeSource).toContain("handleWorkflowPortKeyDown");
  });
});

import { describe, expect, it } from "@rstest/core";

import {
  createWorkflowConnectCommand,
  createWorkflowNextStepCommand,
  createWorkflowPaletteNodeCommand,
} from "@/components/projects/workflows/node-config/workflow-node-commands";
import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import { createWorkflowEditorStore } from "@/core/project-workflows/editor/store";

const START_ID = "00000000-0000-4000-8000-000000000001";
const LOOP_ID = "00000000-0000-4000-8000-000000000002";
const BODY_ID = "00000000-0000-4000-8000-000000000003";
const NEW_ID = "10000000-0000-4000-8000-000000000001";
const EDGE_ID = "edge-next-1";

const document = (): WorkflowPersistedDocumentV1 =>
  ({
    spec: {
      schema_version: 1,
      entry_node_id: START_ID,
      nodes: [
        {
          id: START_ID,
          type: "start",
          type_version: 1,
          scope: { kind: "root" },
          config: {},
        },
        {
          id: LOOP_ID,
          type: "loop",
          type_version: 1,
          scope: { kind: "root" },
          config: {},
        },
        {
          id: BODY_ID,
          type: "transform",
          type_version: 1,
          scope: { kind: "loop_body", loop_node_id: LOOP_ID },
          config: {},
        },
      ],
      transitions: [],
    },
    canvas: {
      schema_version: 1,
      node_layouts: [
        { node_id: START_ID, position: { x: 0, y: 0 } },
        { node_id: LOOP_ID, position: { x: 320, y: 0 } },
        {
          node_id: BODY_ID,
          position: { x: 40, y: 80 },
          parent_node_id: LOOP_ID,
        },
      ],
      edge_layouts: [],
    },
  }) as WorkflowPersistedDocumentV1;

const ids = () => {
  const values = [NEW_ID, EDGE_ID];
  return () => values.shift() ?? "unused";
};

describe("G20 Workflow palette and next-step commands", () => {
  it("turns a complete React Flow connection into one closed control command", () => {
    expect(
      createWorkflowConnectCommand({
        connection: {
          source: START_ID,
          sourceHandle: "next",
          target: LOOP_ID,
          targetHandle: "in",
        },
        edgeId: EDGE_ID,
      }),
    ).toEqual({
      type: "connect",
      transition: {
        id: EDGE_ID,
        source: { node_id: START_ID, port_id: "next" },
        target: { node_id: LOOP_ID, port_id: "in" },
      },
      routing: "smoothstep",
    });
    expect(
      createWorkflowConnectCommand({
        connection: {
          source: START_ID,
          sourceHandle: null,
          target: LOOP_ID,
          targetHandle: "in",
        },
        edgeId: EDGE_ID,
      }),
    ).toBeNull();
  });

  it("creates one root node and authored transition with stable identities", () => {
    const value = document();
    const command = createWorkflowNextStepCommand({
      sourceNodeId: START_ID,
      sourcePortId: "next",
      candidate: {
        nodeType: "llm",
        targetPortId: "in",
        title: "大模型",
      },
      document: value,
      nextId: ids(),
    });

    expect(command).toMatchObject({
      type: "add_next_step",
      source: { node_id: START_ID, port_id: "next" },
      node: {
        id: NEW_ID,
        type: "llm",
        type_version: 1,
        scope: { kind: "root" },
      },
      transition: { id: EDGE_ID, target_port_id: "in" },
    });
  });

  it("creates a Loop body entry atomically without an authored body edge", () => {
    const value = document();
    value.spec.nodes = value.spec.nodes?.filter((node) => node.id !== BODY_ID);
    value.canvas.node_layouts = value.canvas.node_layouts?.filter(
      (layout) => layout.node_id !== BODY_ID,
    );
    const command = createWorkflowNextStepCommand({
      sourceNodeId: LOOP_ID,
      sourcePortId: "body",
      candidate: {
        nodeType: "transform",
        targetPortId: "in",
        title: "模板转换",
      },
      document: value,
      nextId: ids(),
    });
    expect(command?.type).toBe("add_loop_body_entry");

    const store = createWorkflowEditorStore({ document: value });
    const result = command ? store.dispatch(command) : null;
    expect(result?.applied).toBe(true);
    expect(store.getState().current.spec.transitions).toEqual([]);
    expect(store.getState().current.spec.nodes).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: NEW_ID,
          scope: { kind: "loop_body", loop_node_id: LOOP_ID },
        }),
      ]),
    );
    expect(
      store.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.config,
    ).toEqual(
      expect.objectContaining({
        body_entry_node_id: NEW_ID,
        body_exit_node_id: NEW_ID,
      }),
    );
  });

  it("keeps a normal next step inside the existing Loop body scope", () => {
    const command = createWorkflowNextStepCommand({
      sourceNodeId: BODY_ID,
      sourcePortId: "next",
      candidate: {
        nodeType: "python_code",
        targetPortId: "in",
        title: "代码执行",
      },
      document: document(),
      nextId: ids(),
    });

    expect(command).toMatchObject({
      type: "add_next_step",
      node: {
        scope: { kind: "loop_body", loop_node_id: LOOP_ID },
      },
      layout: { parent_node_id: LOOP_ID },
    });
  });

  it("rejects nested Loop and root-only Start/End inside a Loop body", () => {
    for (const nodeType of ["start", "end", "loop"] as const) {
      expect(
        createWorkflowNextStepCommand({
          sourceNodeId: BODY_ID,
          sourcePortId: "next",
          candidate: { nodeType, targetPortId: "in", title: nodeType },
          document: document(),
          nextId: ids(),
        }),
      ).toBeNull();
    }
  });

  it("creates a root palette node without inventing runtime authority", () => {
    expect(
      createWorkflowPaletteNodeCommand({
        nodeType: "http_request",
        position: { x: 10, y: 20 },
        nodeId: NEW_ID,
      }),
    ).toEqual({
      type: "add_node",
      node: {
        id: NEW_ID,
        type: "http_request",
        type_version: 1,
        scope: { kind: "root" },
        config: {},
      },
      layout: { node_id: NEW_ID, position: { x: 10, y: 20 } },
    });
  });
});

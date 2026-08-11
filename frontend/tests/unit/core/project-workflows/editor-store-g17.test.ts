import { describe, expect, it, rs } from "@rstest/core";

import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import {
  analyzeWorkflowNodeDeletion,
  createWorkflowEditorStore,
  selectStrictWorkflowDocument,
} from "@/core/project-workflows/editor/store";
import type { WorkflowEditorSessionV1 } from "@/core/project-workflows/editor-contracts";
import type { JsonValue } from "@/core/project-workflows/types";

import canvasFixture from "../../../fixtures/workflows/canvas-document-v1.json";
import specFixture from "../../../fixtures/workflows/workflow-spec-v1.json";

const START_ID = "00000000-0000-4000-8000-000000000001";
const END_ID = "00000000-0000-4000-8000-000000000002";
const MIDDLE_ID = "00000000-0000-4000-8000-000000000003";
const NEXT_ID = "00000000-0000-4000-8000-000000000004";
const LOOP_ID = "00000000-0000-4000-8000-000000000005";
const RUN_ID = "10000000-0000-4000-8000-000000000001";
const VERSION_ID = "20000000-0000-4000-8000-000000000001";
const WORKFLOW_ID = "30000000-0000-4000-8000-000000000001";
const PROJECT_ID = "40000000-0000-4000-8000-000000000001";
const ACCOUNT_ID = "50000000-0000-4000-8000-000000000001";

type DraftNode = NonNullable<
  WorkflowPersistedDocumentV1["spec"]["nodes"]
>[number];

function rootNode(
  id: string,
  type: string,
  config: Record<string, JsonValue> = {},
): DraftNode {
  return {
    id,
    type,
    type_version: 1,
    scope: { kind: "root" },
    config,
  };
}

function partialDocument(): WorkflowPersistedDocumentV1 {
  return {
    spec: {
      schema_version: 1,
      entry_node_id: START_ID,
      nodes: [rootNode(START_ID, "start"), rootNode(END_ID, "end")],
      transitions: [
        {
          id: "edge-start-end",
          source: { node_id: START_ID, port_id: "next" },
          target: { node_id: END_ID, port_id: "in" },
        },
      ],
    },
    canvas: {
      schema_version: 1,
      node_layouts: [
        { node_id: START_ID, position: { x: 0, y: 0 } },
        { node_id: END_ID, position: { x: 400, y: 0 } },
      ],
      edge_layouts: [{ edge_id: "edge-start-end", routing: "smoothstep" }],
    },
  };
}

function session(nodeId: string | null = null): WorkflowEditorSessionV1 {
  return {
    schema_version: 1,
    viewport: { x: 0, y: 0, zoom: 1 },
    selection: { node_ids: nodeId === null ? [] : [nodeId], edge_ids: [] },
    inspector: {
      open: nodeId !== null,
      node_id: nodeId,
      tab: "settings",
      width_px: 480,
      expanded_section_ids: [],
      scroll_top: 0,
    },
    palette: { open: false, anchor: null },
    interaction: { kind: "idle" },
  };
}

function runtimeProjection() {
  return {
    schema_version: 1 as const,
    scope: {
      account_id: ACCOUNT_ID,
      project_id: PROJECT_ID,
      workflow_id: WORKFLOW_ID,
      run_id: RUN_ID,
      workflow_version_id: VERSION_ID,
    },
    cursor: "1",
    run_status: "running" as const,
    progress: null,
    node_attempts: [],
    output_preview: null,
    error: null,
    wait: null,
  };
}

describe("G17 per-Workbench Workflow editor store", () => {
  it("creates isolated instances and never exposes a module singleton", () => {
    const first = createWorkflowEditorStore({ document: partialDocument() });
    const second = createWorkflowEditorStore({ document: partialDocument() });

    expect(first).not.toBe(second);
    expect(
      first.dispatch({
        type: "commit_node_position",
        positions: { [START_ID]: { x: 10, y: 20 } },
      }).applied,
    ).toBe(true);

    expect(first.getState().dirty).toBe(true);
    expect(second.getState().dirty).toBe(false);
    expect(
      second.getState().current.canvas.node_layouts?.[0]?.position,
    ).toEqual({
      x: 0,
      y: 0,
    });
  });

  it("keeps a valid incomplete Draft loadable while exposing complete projection explicitly", () => {
    const partial = createWorkflowEditorStore({ document: partialDocument() });
    expect(selectStrictWorkflowDocument(partial.getState())).toBeNull();

    const complete = createWorkflowEditorStore({
      document: {
        spec: specFixture,
        canvas: canvasFixture,
      } as unknown as WorkflowPersistedDocumentV1,
    });
    expect(selectStrictWorkflowDocument(complete.getState())).toEqual({
      spec: specFixture,
      canvas: canvasFixture,
    });
  });

  it("applies closed structural commands atomically across partial Spec and Canvas", () => {
    const store = createWorkflowEditorStore({ document: partialDocument() });

    expect(
      store.dispatch({
        type: "add_node",
        node: rootNode(MIDDLE_ID, "transform"),
        layout: {
          node_id: MIDDLE_ID,
          position: { x: 200, y: 120 },
        },
      }).applied,
    ).toBe(true);
    expect(
      store.dispatch({
        type: "connect",
        transition: {
          id: "edge-middle-end",
          source: { node_id: MIDDLE_ID, port_id: "next" },
          target: { node_id: END_ID, port_id: "in" },
        },
        routing: "bezier",
      }).applied,
    ).toBe(true);
    expect(
      store.dispatch({
        type: "update_node_config",
        node_id: MIDDLE_ID,
        config: { mode: "text" },
      }).applied,
    ).toBe(true);
    expect(
      store.dispatch({
        type: "disconnect",
        edge_ids: ["edge-middle-end"],
      }).applied,
    ).toBe(true);

    const state = store.getState();
    const middle = state.current.spec.nodes?.find(
      (node) => node.id === MIDDLE_ID,
    );
    expect(middle?.config).toEqual({ mode: "text" });
    expect(
      state.current.canvas.node_layouts?.some(
        (layout) => layout.node_id === MIDDLE_ID,
      ),
    ).toBe(true);
    expect(
      state.current.canvas.edge_layouts?.some(
        (layout) => layout.edge_id === "edge-middle-end",
      ),
    ).toBe(false);
  });

  it("reparents semantics and layout together and creates next-step node plus edge in one history entry", () => {
    const document = partialDocument();
    document.spec.nodes!.splice(1, 0, rootNode(LOOP_ID, "loop"));
    document.canvas.node_layouts!.splice(1, 0, {
      node_id: LOOP_ID,
      position: { x: 100, y: 100 },
    });
    const store = createWorkflowEditorStore({ document });

    expect(
      store.dispatch({
        type: "add_node",
        node: rootNode(MIDDLE_ID, "transform"),
        layout: { node_id: MIDDLE_ID, position: { x: 20, y: 20 } },
      }).applied,
    ).toBe(true);
    expect(
      store.dispatch({
        type: "reparent_node",
        node_id: MIDDLE_ID,
        parent_node_id: LOOP_ID,
      }).applied,
    ).toBe(true);

    const historyBefore = store.getState().history.past.length;
    expect(
      store.dispatch({
        type: "add_next_step",
        source: { node_id: MIDDLE_ID, port_id: "next" },
        node: {
          ...rootNode(NEXT_ID, "transform"),
          scope: { kind: "loop_body", loop_node_id: LOOP_ID },
        },
        layout: {
          node_id: NEXT_ID,
          parent_node_id: LOOP_ID,
          position: { x: 220, y: 20 },
        },
        transition: {
          id: "edge-next-step",
          target_port_id: "in",
          routing: "smoothstep",
        },
      }).applied,
    ).toBe(true);

    const current = store.getState().current;
    expect(
      current.spec.nodes?.find((node) => node.id === MIDDLE_ID)?.scope,
    ).toEqual({ kind: "loop_body", loop_node_id: LOOP_ID });
    expect(
      current.canvas.node_layouts?.find(
        (layout) => layout.node_id === MIDDLE_ID,
      )?.parent_node_id,
    ).toBe(LOOP_ID);
    expect(current.spec.transitions?.at(-1)).toEqual({
      id: "edge-next-step",
      source: { node_id: MIDDLE_ID, port_id: "next" },
      target: { node_id: NEXT_ID, port_id: "in" },
    });
    expect(store.getState().history.past.length).toBe(historyBefore + 1);
  });

  it("computes deletion impact, requires confirmation, and clears affected Spec/Canvas references", () => {
    const store = createWorkflowEditorStore({ document: partialDocument() });

    const blocked = store.dispatch({
      type: "delete_nodes",
      node_ids: [START_ID],
    });
    expect(blocked.applied).toBe(false);
    expect(blocked.requires_confirmation).toBe(true);
    expect(blocked.deletion_impact).toEqual(
      expect.objectContaining({
        deleted_node_ids: [START_ID],
        transition_ids: ["edge-start-end"],
      }),
    );
    expect(store.getState().dirty).toBe(false);

    const applied = store.dispatch({
      type: "delete_nodes",
      node_ids: [START_ID],
      confirmed: true,
    });
    expect(applied.applied).toBe(true);
    expect(store.getState().current.spec.entry_node_id).toBeNull();
    expect(store.getState().current.spec.transitions).toEqual([]);
    expect(
      store.getState().current.canvas.node_layouts?.map((item) => item.node_id),
    ).toEqual([END_ID]);
    expect(store.getState().current.canvas.edge_layouts).toEqual([]);
  });

  it("bounds snapshot history, recalculates dirty, clears redo, and starts a new publish epoch", () => {
    const store = createWorkflowEditorStore({
      document: partialDocument(),
      historyLimit: 2,
    });

    for (const x of [1, 2, 3]) {
      expect(
        store.dispatch({
          type: "commit_node_position",
          positions: { [START_ID]: { x, y: 0 } },
        }).applied,
      ).toBe(true);
    }
    expect(store.getState().history.past).toHaveLength(2);
    expect(store.undo()).toBe(true);
    expect(store.getState().history.future).toHaveLength(1);

    store.markSaved();
    expect(store.getState().dirty).toBe(false);
    expect(store.undo()).toBe(true);
    expect(store.getState().dirty).toBe(true);
    expect(
      store.dispatch({
        type: "commit_node_position",
        positions: { [START_ID]: { x: 9, y: 0 } },
      }).applied,
    ).toBe(true);
    expect(store.getState().history.future).toEqual([]);

    const epoch = store.getState().history.epoch;
    store.beginPublishEpoch();
    expect(store.getState().history).toEqual({
      past: [],
      future: [],
      epoch: epoch + 1,
    });
    expect(store.getState().dirty).toBe(false);
  });

  it("marks only the submitted save snapshot when editing continues during a request", () => {
    const store = createWorkflowEditorStore({ document: partialDocument() });
    expect(
      store.dispatch({
        type: "commit_node_position",
        positions: { [START_ID]: { x: 10, y: 0 } },
      }).applied,
    ).toBe(true);
    const submitted = store.getState().current;
    expect(
      store.dispatch({
        type: "commit_node_position",
        positions: { [START_ID]: { x: 20, y: 0 } },
      }).applied,
    ).toBe(true);

    expect(store.markSaved(submitted)).toBe(true);
    expect(store.getState().baseline).toEqual(submitted);
    expect(store.getState().dirty).toBe(true);
    expect(store.getState().current.canvas.node_layouts?.[0]?.position?.x).toBe(
      20,
    );
  });

  it("keeps drag transient until stop and commits all positions as one history snapshot", () => {
    const store = createWorkflowEditorStore({ document: partialDocument() });
    const initialHistory = store.getState().history.past.length;

    expect(store.beginNodeDrag([START_ID])).toBe(true);
    expect(store.updateNodeDragPosition(START_ID, { x: 50, y: 60 })).toBe(true);
    expect(store.getState().dirty).toBe(false);
    expect(store.getState().history.past).toHaveLength(initialHistory);
    const interaction = store.getState().editorSession.interaction;
    expect(
      interaction.kind === "node_drag"
        ? interaction.transient_positions[START_ID]
        : null,
    ).toEqual({ x: 50, y: 60 });

    expect(store.commitNodeDrag()).toBe(true);
    expect(store.getState().history.past).toHaveLength(initialHistory + 1);
    expect(store.getState().current.canvas.node_layouts?.[0]?.position).toEqual(
      {
        x: 50,
        y: 60,
      },
    );
    expect(store.getState().editorSession.interaction).toEqual({
      kind: "idle",
    });

    expect(store.beginNodeDrag([START_ID])).toBe(true);
    expect(store.commitNodeDrag()).toBe(true);
    expect(store.getState().history.past).toHaveLength(initialHistory + 1);
    expect(store.getState().editorSession.interaction).toEqual({
      kind: "idle",
    });
  });

  it("updates session/runtime without changing persisted dirty/history and stops late writes after dispose", () => {
    const store = createWorkflowEditorStore({ document: partialDocument() });
    const listener = rs.fn();
    const unsubscribe = store.subscribe(listener);
    const history = store.getState().history;

    expect(store.setEditorSession(session(START_ID))).toBe(true);
    expect(store.setRuntimeProjection(runtimeProjection())).toBe(true);
    expect(store.getState().dirty).toBe(false);
    expect(store.getState().history).toEqual(history);
    expect(store.getState().editorSession.inspector.node_id).toBe(START_ID);
    expect(store.getState().runtimeProjection?.cursor).toBe("1");

    const notificationsBeforeDispose = listener.mock.calls.length;
    store.dispose();
    expect(store.setRuntimeProjection(null)).toBe(false);
    expect(
      store.dispatch({
        type: "commit_node_position",
        positions: { [START_ID]: { x: 99, y: 99 } },
      }).applied,
    ).toBe(false);
    expect(listener).toHaveBeenCalledTimes(notificationsBeforeDispose);
    unsubscribe();
  });

  it("projects server validation issues without changing Draft history and clears stale issues on edit", () => {
    const store = createWorkflowEditorStore({ document: partialDocument() });
    const history = store.getState().history;
    const issues = [
      {
        severity: "error" as const,
        code: "WORKFLOW_PORT_CARDINALITY",
        message: "Start output requires one path",
        path: ["nodes", "0"],
        node_id: START_ID,
        port_id: "next",
      },
    ];

    expect(store.setValidationIssues(issues)).toBe(true);
    expect(store.getState().validationIssues).toEqual(issues);
    expect(Object.isFrozen(store.getState().validationIssues)).toBe(true);
    expect(store.getState().dirty).toBe(false);
    expect(store.getState().history).toBe(history);

    expect(
      store.dispatch({
        type: "commit_node_position",
        positions: { [START_ID]: { x: 2, y: 3 } },
      }).applied,
    ).toBe(true);
    expect(store.getState().validationIssues).not.toEqual(issues);
  });

  it("defensively freezes every authoritative snapshot and ignores caller-owned nested mutation", () => {
    const callerDocument = partialDocument();
    const store = createWorkflowEditorStore({ document: callerDocument });
    callerDocument.canvas.node_layouts![0]!.position!.x = 777;

    const snapshot = store.getState();
    expect(snapshot.current.canvas.node_layouts?.[0]?.position?.x).toBe(0);
    expect(Object.isFrozen(snapshot)).toBe(true);
    expect(Object.isFrozen(snapshot.current)).toBe(true);
    expect(Object.isFrozen(snapshot.current.spec.nodes?.[0])).toBe(true);
    expect(
      Object.isFrozen(snapshot.baseline.canvas.node_layouts?.[0]?.position),
    ).toBe(true);
    expect(Reflect.set(snapshot.current.spec.nodes![0]!, "type", "end")).toBe(
      false,
    );
    expect(store.getState().current.spec.nodes?.[0]?.type).toBe("start");
  });

  it("uses canonical object semantics for dirty/history instead of identity or key order", () => {
    const initial = partialDocument();
    initial.spec.nodes![0]!.type = "legacy_unknown";
    initial.spec.nodes![0]!.config = { alpha: 1, beta: 2 };
    const store = createWorkflowEditorStore({ document: initial });

    const result = store.dispatch({
      type: "update_node_config",
      node_id: START_ID,
      config: { beta: 2, alpha: 1 },
    });
    expect(result.applied).toBe(false);
    expect(store.getState().dirty).toBe(false);
    expect(store.getState().history.past).toEqual([]);
  });

  it("uses canonical NFC semantics and rejects normalization-colliding Draft keys atomically", () => {
    const initial = partialDocument();
    initial.spec.nodes![0]!.custom_label = "Caf\u00e9";
    initial.spec.nodes![0]!.description = null;
    initial.spec.nodes![0]!.type = "legacy_unknown";
    const store = createWorkflowEditorStore({ document: initial });

    expect(
      store.dispatch({
        type: "update_node_presentation",
        node_id: START_ID,
        custom_label: "Cafe\u0301",
        description: null,
      }).applied,
    ).toBe(false);
    expect(store.getState().dirty).toBe(false);
    expect(store.getState().history.past).toEqual([]);

    const before = store.getState();
    const rejected = store.dispatch({
      type: "update_node_config",
      node_id: START_ID,
      config: { "\u00e9": 1, "e\u0301": 2 },
    });
    expect(rejected).toEqual(
      expect.objectContaining({
        applied: false,
        issues: [expect.objectContaining({ code: "WORKFLOW_DRAFT_INVALID" })],
      }),
    );
    expect(store.getState()).toBe(before);
  });

  it("updates node presentation through one closed atomic history command", () => {
    const store = createWorkflowEditorStore({ document: partialDocument() });

    expect(
      store.dispatch({
        type: "update_node_presentation",
        node_id: START_ID,
        custom_label: "入口",
        description: "接收工作流输入",
      }).applied,
    ).toBe(true);
    expect(store.getState().dirty).toBe(true);
    expect(store.getState().history.past).toHaveLength(1);
    expect(store.getState().current.spec.nodes?.[0]).toEqual(
      expect.objectContaining({
        custom_label: "入口",
        description: "接收工作流输入",
      }),
    );
    expect(store.undo()).toBe(true);
    expect(
      store.getState().current.spec.nodes?.[0]?.custom_label,
    ).toBeUndefined();
    expect(store.getState().dirty).toBe(false);
  });

  it("discovers and safely removes recursive config bindings during confirmed deletion", () => {
    const sourceId = MIDDLE_ID;
    const conditionId = NEXT_ID;
    const httpId = "00000000-0000-4000-8000-000000000006";
    const messageId = "00000000-0000-4000-8000-000000000007";
    const binding = {
      kind: "node_output",
      node_id: sourceId,
      output_id: "text",
    } as const;
    const value = partialDocument();
    value.spec.nodes = [
      rootNode(sourceId, "llm"),
      rootNode(messageId, "llm", {
        messages: [
          {
            id: "message",
            role: "user",
            content: {
              version: 1,
              segments: [{ kind: "binding", value: binding }],
            },
          },
        ],
      }),
      rootNode(conditionId, "condition", {
        branches: [
          {
            id: "if",
            output_port_id: "branch",
            label: null,
            predicate: {
              op: "and",
              items: [
                {
                  left: binding,
                  operator: "eq",
                  right: { kind: "literal", value: "ok" },
                },
              ],
            },
          },
        ],
        else_output_port_id: "fallback",
      }),
      rootNode(httpId, "http_request", {
        query: [{ id: "query", name: "q", value: binding }],
      }),
    ];
    value.spec.transitions = [];
    value.canvas.node_layouts = value.spec.nodes.map((item, index) => ({
      node_id: item.id,
      position: { x: index * 100, y: 0 },
    }));
    value.canvas.edge_layouts = [];
    const store = createWorkflowEditorStore({ document: value });

    const impact = analyzeWorkflowNodeDeletion(store.getState().current, [
      sourceId,
    ]);
    expect(impact.binding_references).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ owner: "node_config", node_id: messageId }),
        expect.objectContaining({
          owner: "node_config",
          node_id: conditionId,
        }),
        expect.objectContaining({ owner: "node_config", node_id: httpId }),
      ]),
    );
    expect(
      impact.binding_references
        .filter((item) => item.owner === "node_config")
        .every((item) => item.path.length > 1),
    ).toBe(true);

    expect(
      store.dispatch({ type: "delete_nodes", node_ids: [sourceId] }),
    ).toEqual(
      expect.objectContaining({
        applied: false,
        requires_confirmation: true,
      }),
    );
    expect(
      store.dispatch({
        type: "delete_nodes",
        node_ids: [sourceId],
        confirmed: true,
      }),
    ).toEqual(expect.objectContaining({ applied: true }));
    expect(JSON.stringify(store.getState().current)).not.toContain(sourceId);
  });

  it("keeps failed commands atomic and isolates subscriber exceptions and reentrant updates", () => {
    const store = createWorkflowEditorStore({ document: partialDocument() });
    const before = store.getState();
    const observer = rs.fn();
    let reentered = false;
    store.subscribe(() => {
      if (!reentered) {
        reentered = true;
        store.setEditorSession(session(START_ID));
      }
      throw new Error("subscriber failure must stay isolated");
    });
    store.subscribe(observer);

    const rejected = store.dispatch({
      type: "connect",
      transition: {
        id: "edge-self",
        source: { node_id: START_ID, port_id: "next" },
        target: { node_id: START_ID, port_id: "next" },
      },
      routing: "smoothstep",
    });
    expect(rejected.applied).toBe(false);
    expect(store.getState()).toBe(before);
    expect(observer).not.toHaveBeenCalled();

    expect(store.dispatch({ type: "future_command" } as never)).toEqual(
      expect.objectContaining({
        applied: false,
        issues: [
          expect.objectContaining({ code: "WORKFLOW_EDITOR_COMMAND_UNKNOWN" }),
        ],
      }),
    );
    expect(store.getState()).toBe(before);
    expect(observer).not.toHaveBeenCalled();

    expect(
      store.dispatch({
        type: "commit_node_position",
        positions: { [START_ID]: { x: 5, y: 6 } },
      }).applied,
    ).toBe(true);
    expect(store.getState().current.canvas.node_layouts?.[0]?.position).toEqual(
      {
        x: 5,
        y: 6,
      },
    );
    expect(store.getState().editorSession.inspector.node_id).toBe(START_ID);
    expect(store.getState().history.past).toHaveLength(1);
    expect(observer).toHaveBeenCalled();
  });

  it("falls back to the bounded default when a caller supplies a non-finite history limit", () => {
    const store = createWorkflowEditorStore({
      document: partialDocument(),
      historyLimit: Number.NaN,
    });
    for (const x of [1, 2, 3]) {
      expect(
        store.dispatch({
          type: "commit_node_position",
          positions: { [START_ID]: { x, y: 0 } },
        }).applied,
      ).toBe(true);
    }
    expect(store.getState().history.past).toHaveLength(3);
  });
});

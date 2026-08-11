import { describe, expect, it } from "@rstest/core";

import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import { createWorkflowEditorStore } from "@/core/project-workflows/editor/store";
import type { JsonValue } from "@/core/project-workflows/types";

const START_ID = "00000000-0000-4000-8000-000000000001";
const LOOP_ID = "00000000-0000-4000-8000-000000000002";
const END_ID = "00000000-0000-4000-8000-000000000003";
const ENTRY_ID = "00000000-0000-4000-8000-000000000004";
const EXIT_ID = "00000000-0000-4000-8000-000000000005";

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

function loopDocument(): WorkflowPersistedDocumentV1 {
  return {
    spec: {
      schema_version: 1,
      entry_node_id: START_ID,
      nodes: [
        rootNode(START_ID, "start"),
        rootNode(LOOP_ID, "loop", { mode: "do_until" }),
        rootNode(END_ID, "end"),
      ],
      transitions: [],
    },
    canvas: {
      schema_version: 1,
      node_layouts: [
        { node_id: START_ID, position: { x: 0, y: 0 } },
        { node_id: LOOP_ID, position: { x: 200, y: 0 } },
        { node_id: END_ID, position: { x: 600, y: 0 } },
      ],
      edge_layouts: [],
    },
  };
}

function documentWithLoopChildren(): WorkflowPersistedDocumentV1 {
  const document = loopDocument();
  const loop = document.spec.nodes?.find((node) => node.id === LOOP_ID);
  loop!.config = {
    mode: "do_until",
    body_entry_node_id: ENTRY_ID,
    body_exit_node_id: ENTRY_ID,
  };
  document.spec.nodes!.splice(
    2,
    0,
    {
      ...rootNode(ENTRY_ID, "transform"),
      scope: { kind: "loop_body", loop_node_id: LOOP_ID },
    },
    {
      ...rootNode(EXIT_ID, "python_code"),
      scope: { kind: "loop_body", loop_node_id: LOOP_ID },
    },
  );
  document.canvas.node_layouts!.splice(
    2,
    0,
    {
      node_id: ENTRY_ID,
      parent_node_id: LOOP_ID,
      position: { x: 20, y: 20 },
    },
    {
      node_id: EXIT_ID,
      parent_node_id: LOOP_ID,
      position: { x: 220, y: 20 },
    },
  );
  return document;
}

describe("G20 shared Workflow editor commands", () => {
  it("replaces authored declarations and node fields as closed single-history commands", () => {
    const store = createWorkflowEditorStore({ document: loopDocument() });
    const commands = [
      {
        type: "replace_workflow_inputs" as const,
        workflow_inputs: [{ id: "prompt", name: "Prompt" }],
      },
      {
        type: "replace_workflow_outputs" as const,
        workflow_outputs: [{ id: "answer", name: "answer", source: null }],
      },
      {
        type: "replace_credential_slots" as const,
        credential_slots: [
          {
            id: "http_auth",
            name: "HTTP auth",
            purpose: "http_auth" as const,
            payload_schema: { type: "object" },
            required: true as const,
          },
        ],
      },
      {
        type: "update_node_input_bindings" as const,
        node_id: LOOP_ID,
        input_bindings: { initial: null },
      },
      {
        type: "update_node_execution_policy" as const,
        node_id: LOOP_ID,
        execution_policy: {
          retry: { mode: "none" },
          on_error: { mode: "fail_workflow" },
        },
      },
    ];

    for (const [index, command] of commands.entries()) {
      const before = store.getState().history.past.length;
      expect(store.dispatch(command).applied).toBe(true);
      expect(store.getState().history.past).toHaveLength(before + 1);
      expect(store.getState().history.future).toEqual([]);
      expect(store.getState().dirty).toBe(true);
      expect(index).toBe(before);
    }

    const current = store.getState().current;
    expect(current.spec.workflow_inputs).toEqual([
      { id: "prompt", name: "Prompt" },
    ]);
    expect(current.spec.workflow_outputs).toEqual([
      { id: "answer", name: "answer", source: null },
    ]);
    expect(current.spec.credential_slots?.[0]?.id).toBe("http_auth");
    expect(
      current.spec.nodes?.find((node) => node.id === LOOP_ID)?.input_bindings,
    ).toEqual({ initial: null });
    expect(
      current.spec.nodes?.find((node) => node.id === LOOP_ID)?.execution_policy,
    ).toEqual({
      retry: { mode: "none" },
      on_error: { mode: "fail_workflow" },
    });

    expect(store.undo()).toBe(true);
    expect(
      store.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.execution_policy,
    ).toBeUndefined();
    expect(store.redo()).toBe(true);
    expect(
      store.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.execution_policy,
    ).toEqual(commands.at(-1)?.execution_policy);
  });

  it("strictly rejects malformed partial Draft payloads without state or history changes", () => {
    const store = createWorkflowEditorStore({ document: loopDocument() });
    const invalidCommands = [
      {
        type: "replace_workflow_inputs",
        workflow_inputs: [{ id: "prompt", unexpected: true }],
      },
      {
        type: "replace_workflow_outputs",
        workflow_outputs: [{ id: "answer", default: Number.POSITIVE_INFINITY }],
      },
      {
        type: "replace_credential_slots",
        credential_slots: [{ id: "bad slot" }],
      },
      {
        type: "update_node_input_bindings",
        node_id: LOOP_ID,
        input_bindings: { initial: undefined },
      },
      {
        type: "update_node_execution_policy",
        node_id: LOOP_ID,
        execution_policy: { credential_id: ENTRY_ID },
      },
    ];

    for (const command of invalidCommands) {
      const before = store.getState();
      expect(store.dispatch(command as never)).toEqual(
        expect.objectContaining({
          applied: false,
          issues: [expect.objectContaining({ code: "WORKFLOW_DRAFT_INVALID" })],
        }),
      );
      expect(store.getState()).toBe(before);
      expect(store.getState().history.past).toEqual([]);
    }
  });

  it("atomically creates the first Loop body child and optional exit without an authored body edge", () => {
    const store = createWorkflowEditorStore({ document: loopDocument() });
    const result = store.dispatch({
      type: "add_loop_body_entry",
      loop_node_id: LOOP_ID,
      node: rootNode(ENTRY_ID, "transform"),
      layout: { node_id: ENTRY_ID, position: { x: 20, y: 20 } },
    });

    expect(result.applied).toBe(true);
    expect(store.getState().history.past).toHaveLength(1);
    const current = store.getState().current;
    expect(
      current.spec.nodes?.find((node) => node.id === ENTRY_ID)?.scope,
    ).toEqual({ kind: "loop_body", loop_node_id: LOOP_ID });
    expect(
      current.canvas.node_layouts?.find((layout) => layout.node_id === ENTRY_ID)
        ?.parent_node_id,
    ).toBe(LOOP_ID);
    expect(
      current.spec.nodes?.find((node) => node.id === LOOP_ID)?.config,
    ).toEqual({
      mode: "do_until",
      body_entry_node_id: ENTRY_ID,
      body_exit_node_id: ENTRY_ID,
    });
    expect(current.spec.transitions).toEqual([]);
    expect(current.canvas.edge_layouts).toEqual([]);

    expect(store.undo()).toBe(true);
    expect(
      store.getState().current.spec.nodes?.some((node) => node.id === ENTRY_ID),
    ).toBe(false);
    expect(
      store.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.config,
    ).toEqual({ mode: "do_until" });
    expect(store.redo()).toBe(true);
    expect(
      store.getState().current.spec.nodes?.find((node) => node.id === ENTRY_ID)
        ?.scope,
    ).toEqual({ kind: "loop_body", loop_node_id: LOOP_ID });

    const entryOnly = createWorkflowEditorStore({ document: loopDocument() });
    expect(
      entryOnly.dispatch({
        type: "add_loop_body_entry",
        loop_node_id: LOOP_ID,
        node: rootNode(ENTRY_ID, "transform"),
        layout: { node_id: ENTRY_ID, position: { x: 20, y: 20 } },
        set_as_exit: false,
      }).applied,
    ).toBe(true);
    expect(
      entryOnly
        .getState()
        .current.spec.nodes?.find((node) => node.id === LOOP_ID)?.config,
    ).toEqual({ mode: "do_until", body_entry_node_id: ENTRY_ID });
  });

  it("sets Loop exit only to a projected child and leaves a failed attempt atomic", () => {
    const store = createWorkflowEditorStore({
      document: documentWithLoopChildren(),
    });
    const beforeHistory = store.getState().history.past.length;

    expect(
      store.dispatch({
        type: "set_loop_body_exit",
        loop_node_id: LOOP_ID,
        node_id: EXIT_ID,
      }).applied,
    ).toBe(true);
    expect(store.getState().history.past).toHaveLength(beforeHistory + 1);
    expect(
      store.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.config?.body_exit_node_id,
    ).toBe(EXIT_ID);

    expect(store.undo()).toBe(true);
    expect(
      store.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.config?.body_exit_node_id,
    ).toBe(ENTRY_ID);
    expect(store.redo()).toBe(true);

    const historyBeforeClear = store.getState().history.past.length;
    expect(
      store.dispatch({
        type: "set_loop_body_exit",
        loop_node_id: LOOP_ID,
        node_id: null,
      }).applied,
    ).toBe(true);
    expect(store.getState().history.past).toHaveLength(historyBeforeClear + 1);
    expect(
      store.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.config?.body_exit_node_id,
    ).toBeUndefined();
    expect(store.undo()).toBe(true);
    expect(
      store.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.config?.body_exit_node_id,
    ).toBe(EXIT_ID);

    const beforeFailure = store.getState();
    expect(
      store.dispatch({
        type: "set_loop_body_exit",
        loop_node_id: LOOP_ID,
        node_id: END_ID,
      }),
    ).toEqual(
      expect.objectContaining({
        applied: false,
        issues: [
          expect.objectContaining({ code: "WORKFLOW_LOOP_CHILD_INVALID" }),
        ],
      }),
    );
    expect(store.getState()).toBe(beforeFailure);

    const missingConfigDocument = loopDocument();
    const loop = missingConfigDocument.spec.nodes?.find(
      (node) => node.id === LOOP_ID,
    );
    delete loop!.config;
    const missingConfig = createWorkflowEditorStore({
      document: missingConfigDocument,
    });
    const beforeNoop = missingConfig.getState();
    expect(
      missingConfig.dispatch({
        type: "set_loop_body_exit",
        loop_node_id: LOOP_ID,
        node_id: null,
      }).applied,
    ).toBe(false);
    expect(missingConfig.getState()).toBe(beforeNoop);
  });

  it("clears Loop entry and exit atomically when the referenced child leaves or is deleted", () => {
    const moved = createWorkflowEditorStore({
      document: documentWithLoopChildren(),
    });
    expect(
      moved.dispatch({
        type: "reparent_node",
        node_id: ENTRY_ID,
        parent_node_id: null,
      }).applied,
    ).toBe(true);
    expect(
      moved.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.config,
    ).toEqual({ mode: "do_until" });
    expect(moved.getState().history.past).toHaveLength(1);
    expect(moved.undo()).toBe(true);
    expect(
      moved.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.config,
    ).toEqual({
      mode: "do_until",
      body_entry_node_id: ENTRY_ID,
      body_exit_node_id: ENTRY_ID,
    });

    const deleted = createWorkflowEditorStore({
      document: documentWithLoopChildren(),
    });
    const impact = deleted.dispatch({
      type: "delete_nodes",
      node_ids: [ENTRY_ID],
      confirmed: true,
    });
    expect(impact.applied).toBe(true);
    expect(impact.deletion_impact?.binding_references).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          owner: "loop_config",
          binding_id: "body_entry_node_id",
        }),
        expect.objectContaining({
          owner: "loop_config",
          binding_id: "body_exit_node_id",
        }),
      ]),
    );
    expect(
      deleted.getState().current.spec.nodes?.find((node) => node.id === LOOP_ID)
        ?.config,
    ).toEqual({ mode: "do_until" });
  });
});

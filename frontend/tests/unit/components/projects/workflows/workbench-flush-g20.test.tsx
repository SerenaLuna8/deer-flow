import { describe, expect, it, rs } from "@rstest/core";

import {
  runWorkflowSavedDraftAction,
  WORKFLOW_SAVED_DRAFT_REQUIRED_MESSAGE,
} from "@/components/projects/workflows/definitions/detail/workflow-definition-detail";
import { createPythonSourceController } from "@/components/projects/workflows/node-config/code";
import { flushWorkflowEditorBeforeAction } from "@/components/projects/workflows/workbench/workbench-flush-context";
import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import type { WorkflowDraftResponseV1 } from "@/core/project-workflows/definition-contracts";
import { createWorkflowEditorFlushRegistry } from "@/core/project-workflows/editor/flush-registry";
import { createWorkflowEditorStore } from "@/core/project-workflows/editor/store";

const WORKFLOW_ID = "10000000-0000-4000-8000-000000000001";
const NODE_ID = "10000000-0000-4000-8000-000000000002";
const ORIGINAL_SOURCE = "def main(inputs):\n    return {'value': 'old'}\n";
const LATEST_SOURCE = "def main(inputs):\n    return {'value': 'latest'}\n";

const baselineDocument: WorkflowPersistedDocumentV1 = {
  spec: {
    schema_version: 1,
    nodes: [
      {
        id: NODE_ID,
        type: "python_code",
        type_version: 1,
        scope: { kind: "root" },
        config: {
          source: ORIGINAL_SOURCE,
          input_variables: [],
          output_schema: { type: "object" },
          timeout_ms: null,
        },
      },
    ],
  },
  canvas: { schema_version: 1 },
};

const serverDraft: WorkflowDraftResponseV1 = {
  workflow_id: WORKFLOW_ID,
  revision: 7,
  spec: baselineDocument.spec,
  canvas: baselineDocument.canvas,
  draft_checksum: "a".repeat(64),
  updated_at: "2026-08-10T00:00:00Z",
};

function actionHarness() {
  const flushRegistry = createWorkflowEditorFlushRegistry();
  const store = createWorkflowEditorStore({ document: baselineDocument });
  const editor = { draft: serverDraft, flushRegistry, store };
  const source = createPythonSourceController({
    debounceMs: Number.POSITIVE_INFINITY,
    flushKey: `workflow-python-source:${NODE_ID}`,
    initialSource: ORIGINAL_SOURCE,
    maxBytes: 4_096,
    registry: flushRegistry,
    commitSource: (nextSource) => {
      const result = store.dispatch({
        type: "update_node_config",
        node_id: NODE_ID,
        config: {
          source: nextSource,
          input_variables: [],
          output_schema: { type: "object" },
          timeout_ms: null,
        },
      });
      return result.applied
        ? { applied: true as const }
        : { applied: false as const, safeMessage: result.issues[0]?.message };
    },
  });
  return { editor, flushRegistry, source, store };
}

describe("G20 Workbench editor flush barrier", () => {
  it.each(["save", "validate", "publish"] as const)(
    "flushes controlled editor state before %s reads its snapshot",
    (action) => {
      const registry = createWorkflowEditorFlushRegistry();
      let source = "old source";
      registry.register("python:node-1", () => {
        source = `latest source for ${action}`;
      });

      expect(
        flushWorkflowEditorBeforeAction(registry, () => ({ action, source })),
      ).toEqual({ action, source: `latest source for ${action}` });
      expect(registry.hasPending()).toBe(false);
    },
  );

  it("does not read or mutate the action snapshot after a flush failure", () => {
    const registry = createWorkflowEditorFlushRegistry();
    let reads = 0;
    registry.register("python:node-1", () => {
      throw new Error("invalid editor state");
    });

    expect(() =>
      flushWorkflowEditorBeforeAction(registry, () => {
        reads += 1;
        return "unreachable";
      }),
    ).toThrow(AggregateError);
    expect(reads).toBe(0);
    expect(registry.hasPending()).toBe(true);
  });

  it.each(["validate", "publish"] as const)(
    "blocks %s when a pre-debounce Python edit becomes an unsaved Store change during flush",
    (action) => {
      const { editor, flushRegistry, source, store } = actionHarness();
      const request = rs.fn((_request: unknown) => {
        if (action === "publish") store.beginPublishEpoch();
      });
      source.edit(LATEST_SOURCE);

      expect(source.getState().dirty).toBe(true);
      expect(store.getState().dirty).toBe(false);

      const result = runWorkflowSavedDraftAction(
        editor,
        () => editor,
        ({ draft, submitted }) =>
          request({
            expectedRevision: draft.revision,
            expectedChecksum: draft.draft_checksum,
            submitted,
          }),
      );

      expect(result).toEqual({
        status: "unsaved_changes",
        message: WORKFLOW_SAVED_DRAFT_REQUIRED_MESSAGE,
      });
      expect(request).not.toHaveBeenCalled();
      expect(flushRegistry.hasPending()).toBe(false);
      expect(source.getState().dirty).toBe(false);
      expect(store.getState().dirty).toBe(true);
      expect(store.getState().history.epoch).toBe(0);
      expect(store.getState().current.spec.nodes?.[0]?.config).toMatchObject({
        source: LATEST_SOURCE,
      });
    },
  );

  it.each(["validate", "publish"] as const)(
    "submits %s exactly once from the current clean server Draft authority",
    (action) => {
      const { editor, store } = actionHarness();
      const request = rs.fn((_request: unknown) => {
        if (action === "publish") store.beginPublishEpoch();
      });

      const result = runWorkflowSavedDraftAction(
        editor,
        () => editor,
        ({ draft, submitted }) =>
          request({
            expectedRevision: draft.revision,
            expectedChecksum: draft.draft_checksum,
            submitted,
          }),
      );

      expect(result.status).toBe("submitted");
      expect(request).toHaveBeenCalledTimes(1);
      expect(request).toHaveBeenCalledWith({
        expectedRevision: 7,
        expectedChecksum: "a".repeat(64),
        submitted: store.getState().current,
      });
      expect(store.getState().dirty).toBe(false);
      expect(store.getState().history.epoch).toBe(action === "publish" ? 1 : 0);
    },
  );
});

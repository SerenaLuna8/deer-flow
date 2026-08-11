import {
  workflowPersistedDocumentV1Schema,
  type WorkflowPersistedDocumentV1,
} from "@/core/project-workflows/boundaries";
import { serializeCanonicalJsonValue } from "@/core/project-workflows/canonical";
import {
  workflowEditorSessionV1Schema,
  workflowRuntimeProjectionV1Schema,
  type WorkflowEditorSessionV1,
  type WorkflowRuntimeProjectionV1,
} from "@/core/project-workflows/editor-contracts";
import {
  workflowValidationIssueV1Schema,
  type WorkflowValidationIssueV1,
} from "@/core/project-workflows/transport";
import {
  canvasDocumentV1Schema,
  workflowSpecV1Schema,
  type CanvasDocumentV1,
  type JsonValue,
  type WorkflowSpecV1,
} from "@/core/project-workflows/types";

import {
  validateWorkflowControlConnectionV1,
  validateWorkflowDraftStructureV1,
} from "./validation";

type DraftNode = NonNullable<
  WorkflowPersistedDocumentV1["spec"]["nodes"]
>[number];
type DraftTransition = NonNullable<
  WorkflowPersistedDocumentV1["spec"]["transitions"]
>[number];
type DraftNodeLayout = NonNullable<
  WorkflowPersistedDocumentV1["canvas"]["node_layouts"]
>[number];
type DraftPosition = { x: number; y: number };
type DraftWorkflowInputs = NonNullable<
  WorkflowPersistedDocumentV1["spec"]["workflow_inputs"]
>;
type DraftWorkflowOutputs = NonNullable<
  WorkflowPersistedDocumentV1["spec"]["workflow_outputs"]
>;
type DraftCredentialSlots = NonNullable<
  WorkflowPersistedDocumentV1["spec"]["credential_slots"]
>;
type DraftNodeInputBindings = NonNullable<DraftNode["input_bindings"]>;
type DraftNodeExecutionPolicy = NonNullable<DraftNode["execution_policy"]>;

export type WorkflowDeletionBindingReference = {
  owner: "node_input" | "node_config" | "workflow_output" | "loop_config";
  node_id?: string;
  binding_id: string;
  path: string[];
};

export type WorkflowDeletionImpact = {
  requested_node_ids: string[];
  deleted_node_ids: string[];
  transition_ids: string[];
  binding_references: WorkflowDeletionBindingReference[];
};

export type WorkflowEditorCommand =
  | {
      type: "add_node";
      node: DraftNode | Record<string, unknown>;
      layout: DraftNodeLayout | Record<string, unknown>;
    }
  | {
      type: "delete_nodes";
      node_ids: string[];
      confirmed?: boolean;
    }
  | {
      type: "connect";
      transition: DraftTransition | Record<string, unknown>;
      routing: "bezier" | "smoothstep";
    }
  | { type: "disconnect"; edge_ids: string[] }
  | {
      type: "reparent_node";
      node_id: string;
      parent_node_id: string | null;
    }
  | {
      type: "commit_node_position";
      positions: Record<string, DraftPosition>;
    }
  | {
      type: "update_node_config";
      node_id: string;
      config: Record<string, unknown>;
    }
  | {
      type: "replace_workflow_inputs";
      workflow_inputs: DraftWorkflowInputs;
    }
  | {
      type: "replace_workflow_outputs";
      workflow_outputs: DraftWorkflowOutputs;
    }
  | {
      type: "replace_credential_slots";
      credential_slots: DraftCredentialSlots;
    }
  | {
      type: "update_node_input_bindings";
      node_id: string;
      input_bindings: DraftNodeInputBindings;
    }
  | {
      type: "update_node_execution_policy";
      node_id: string;
      execution_policy: DraftNodeExecutionPolicy;
    }
  | {
      type: "update_node_presentation";
      node_id: string;
      custom_label: string | null;
      description: string | null;
    }
  | {
      type: "add_next_step";
      source: { node_id: string; port_id: string };
      node: DraftNode | Record<string, unknown>;
      layout: DraftNodeLayout | Record<string, unknown>;
      transition: {
        id: string;
        target_port_id: string;
        routing: "bezier" | "smoothstep";
      };
    }
  | {
      type: "add_loop_body_entry";
      loop_node_id: string;
      node: DraftNode | Record<string, unknown>;
      layout: DraftNodeLayout | Record<string, unknown>;
      set_as_exit?: boolean;
    }
  | {
      type: "set_loop_body_exit";
      loop_node_id: string;
      node_id: string | null;
    };

export type WorkflowEditorCommandResult = {
  applied: boolean;
  issues: WorkflowValidationIssueV1[];
  requires_confirmation?: boolean;
  deletion_impact?: WorkflowDeletionImpact;
};

export type WorkflowEditorHistory = {
  past: WorkflowPersistedDocumentV1[];
  future: WorkflowPersistedDocumentV1[];
  epoch: number;
};

export type WorkflowEditorState = {
  baseline: WorkflowPersistedDocumentV1;
  current: WorkflowPersistedDocumentV1;
  dirty: boolean;
  history: WorkflowEditorHistory;
  editorSession: WorkflowEditorSessionV1;
  runtimeProjection: WorkflowRuntimeProjectionV1 | null;
  validationIssues: WorkflowValidationIssueV1[];
};

export type StrictWorkflowEditorDocument = {
  spec: WorkflowSpecV1;
  canvas: CanvasDocumentV1;
};

export type WorkflowEditorStore = {
  getState(): WorkflowEditorState;
  subscribe(listener: () => void): () => void;
  dispatch(command: WorkflowEditorCommand): WorkflowEditorCommandResult;
  undo(): boolean;
  redo(): boolean;
  markSaved(baseline?: WorkflowPersistedDocumentV1): boolean;
  beginPublishEpoch(): void;
  setEditorSession(session: WorkflowEditorSessionV1): boolean;
  setRuntimeProjection(runtime: WorkflowRuntimeProjectionV1 | null): boolean;
  setValidationIssues(issues: readonly WorkflowValidationIssueV1[]): boolean;
  beginNodeDrag(nodeIds: string[]): boolean;
  updateNodeDragPosition(nodeId: string, position: DraftPosition): boolean;
  commitNodeDrag(): boolean;
  cancelNodeDrag(): boolean;
  dispose(): void;
};

export type CreateWorkflowEditorStoreInput = {
  document: WorkflowPersistedDocumentV1;
  editorSession?: WorkflowEditorSessionV1;
  runtimeProjection?: WorkflowRuntimeProjectionV1 | null;
  historyLimit?: number;
};

const MAX_HISTORY_LIMIT = 100;

const defaultEditorSession = (): WorkflowEditorSessionV1 => ({
  schema_version: 1,
  viewport: { x: 0, y: 0, zoom: 1 },
  selection: { node_ids: [], edge_ids: [] },
  inspector: {
    open: false,
    node_id: null,
    tab: "settings",
    width_px: 480,
    expanded_section_ids: [],
    scroll_top: 0,
  },
  palette: { open: false, anchor: null },
  interaction: { kind: "idle" },
});

function deepFreeze<T>(value: T, seen = new WeakSet<object>()): T {
  if (value === null || typeof value !== "object") return value;
  const object = value as object;
  if (seen.has(object)) return value;
  seen.add(object);
  for (const nested of Object.values(value as Record<string, unknown>)) {
    deepFreeze(nested, seen);
  }
  return Object.freeze(value);
}

function canonicalJsonEqual(left: unknown, right: unknown): boolean {
  try {
    return (
      serializeCanonicalJsonValue(left as JsonValue) ===
      serializeCanonicalJsonValue(right as JsonValue)
    );
  } catch {
    return false;
  }
}

const safeDocument = (
  value: WorkflowPersistedDocumentV1,
): WorkflowPersistedDocumentV1 => {
  const parsed = workflowPersistedDocumentV1Schema.parse(
    structuredClone(value),
  );
  // This is both the dirty-comparison representation and a fail-closed check
  // for Unicode-scalar/NFC-key collisions before any Draft enters authority.
  serializeCanonicalJsonValue(parsed as unknown as JsonValue);
  return deepFreeze(parsed);
};

const commandIssue = (
  code: string,
  message: string,
  coordinates: {
    node_id?: string;
    edge_id?: string;
    port_id?: string;
  } = {},
): WorkflowValidationIssueV1 => ({
  severity: "error",
  code,
  message,
  path: [],
  ...coordinates,
});

const rejected = (
  code: string,
  message: string,
  coordinates?: Parameters<typeof commandIssue>[2],
): WorkflowEditorCommandResult => ({
  applied: false,
  issues: [commandIssue(code, message, coordinates)],
});

const applied = (
  issues: WorkflowValidationIssueV1[],
): WorkflowEditorCommandResult => ({ applied: true, issues });

const objectValue = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

const stringValue = (value: unknown): string | null =>
  typeof value === "string" && value.length > 0 ? value : null;

function referencedDeletedNode(value: unknown, deleted: Set<string>): boolean {
  if (Array.isArray(value)) {
    return value.some((item) => referencedDeletedNode(item, deleted));
  }
  const object = objectValue(value);
  if (object === null) return false;
  if (
    (object.kind === "node_output" &&
      typeof object.node_id === "string" &&
      deleted.has(object.node_id)) ||
    (object.kind === "loop_variable" &&
      typeof object.loop_node_id === "string" &&
      deleted.has(object.loop_node_id))
  ) {
    return true;
  }
  return Object.values(object).some((item) =>
    referencedDeletedNode(item, deleted),
  );
}

type LocatedBinding = {
  binding: Record<string, unknown>;
  path: string[];
};

function visitLocatedBindings(
  value: unknown,
  path: string[],
  visitor: (located: LocatedBinding) => void,
): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      visitLocatedBindings(item, [...path, String(index)], visitor),
    );
    return;
  }
  const object = objectValue(value);
  if (object === null) return;
  if (object.kind === "node_output" || object.kind === "loop_variable") {
    visitor({ binding: object, path });
    return;
  }
  for (const [key, nested] of Object.entries(object)) {
    visitLocatedBindings(nested, [...path, key], visitor);
  }
}

const REMOVED_CONFIG_BINDING = Symbol("removed-config-binding");

function removeDeletedConfigBindings(
  value: unknown,
  deleted: Set<string>,
): JsonValue | typeof REMOVED_CONFIG_BINDING {
  if (Array.isArray(value)) {
    return value
      .map((item) => removeDeletedConfigBindings(item, deleted))
      .filter((item): item is JsonValue => item !== REMOVED_CONFIG_BINDING);
  }
  const object = objectValue(value);
  if (object === null) return value as JsonValue;
  if (
    (object.kind === "node_output" &&
      typeof object.node_id === "string" &&
      deleted.has(object.node_id)) ||
    (object.kind === "loop_variable" &&
      typeof object.loop_node_id === "string" &&
      deleted.has(object.loop_node_id))
  ) {
    return REMOVED_CONFIG_BINDING;
  }
  const removedKeys = new Set<string>();
  const cleanedEntries: [string, JsonValue][] = [];
  for (const [key, nested] of Object.entries(object)) {
    const cleaned = removeDeletedConfigBindings(nested, deleted);
    if (cleaned === REMOVED_CONFIG_BINDING) {
      removedKeys.add(key);
    } else {
      cleanedEntries.push([key, cleaned]);
    }
  }

  // A binding can be the required value of a larger semantic item. Removing
  // only that property would leave a present-but-invalid partial Draft. Drop
  // the nearest repeatable item instead; its containing array will filter it.
  if (
    removedKeys.size > 0 &&
    (object.kind === "binding" ||
      ("operator" in object && ("left" in object || "right" in object)) ||
      ("id" in object && "name" in object && "value" in object))
  ) {
    return REMOVED_CONFIG_BINDING;
  }

  return Object.fromEntries(cleanedEntries);
}

export function analyzeWorkflowNodeDeletion(
  document: WorkflowPersistedDocumentV1,
  requestedNodeIds: readonly string[],
): WorkflowDeletionImpact {
  const requested = [...new Set(requestedNodeIds)].sort();
  const deleted = new Set(requested);
  let changed = true;
  while (changed) {
    changed = false;
    for (const node of document.spec.nodes ?? []) {
      const nodeId = stringValue(node.id);
      const scope = objectValue(node.scope);
      if (
        nodeId !== null &&
        scope?.kind === "loop_body" &&
        typeof scope.loop_node_id === "string" &&
        deleted.has(scope.loop_node_id) &&
        !deleted.has(nodeId)
      ) {
        deleted.add(nodeId);
        changed = true;
      }
    }
  }
  const transitionIds = (document.spec.transitions ?? [])
    .filter((transition) => {
      const source = objectValue(transition.source);
      const target = objectValue(transition.target);
      return (
        (typeof source?.node_id === "string" && deleted.has(source.node_id)) ||
        (typeof target?.node_id === "string" && deleted.has(target.node_id))
      );
    })
    .map((transition) => stringValue(transition.id))
    .filter((id): id is string => id !== null)
    .sort();
  const bindingReferences: WorkflowDeletionBindingReference[] = [];
  for (const node of document.spec.nodes ?? []) {
    const nodeId = stringValue(node.id);
    if (nodeId === null || deleted.has(nodeId)) continue;
    const bindings = objectValue(node.input_bindings);
    if (bindings !== null) {
      for (const [bindingId, binding] of Object.entries(bindings)) {
        if (referencedDeletedNode(binding, deleted)) {
          bindingReferences.push({
            owner: "node_input",
            node_id: nodeId,
            binding_id: bindingId,
            path: ["input_bindings", bindingId],
          });
        }
      }
    }
    if (node.type === "loop") {
      const config = objectValue(node.config);
      for (const field of ["body_entry_node_id", "body_exit_node_id"]) {
        if (typeof config?.[field] === "string" && deleted.has(config[field])) {
          bindingReferences.push({
            owner: "loop_config",
            node_id: nodeId,
            binding_id: field,
            path: ["config", field],
          });
        }
      }
    }
    visitLocatedBindings(node.config, ["config"], ({ binding, path }) => {
      if (referencedDeletedNode(binding, deleted)) {
        bindingReferences.push({
          owner: "node_config",
          node_id: nodeId,
          binding_id: path.at(-1) ?? "config",
          path,
        });
      }
    });
  }
  for (const output of document.spec.workflow_outputs ?? []) {
    const outputId = stringValue(output.id);
    if (outputId !== null && referencedDeletedNode(output.source, deleted)) {
      bindingReferences.push({
        owner: "workflow_output",
        binding_id: outputId,
        path: ["workflow_outputs", outputId, "source"],
      });
    }
  }
  return deepFreeze({
    requested_node_ids: requested,
    deleted_node_ids: [...deleted].sort(),
    transition_ids: transitionIds,
    binding_references: bindingReferences.sort((left, right) =>
      JSON.stringify(left).localeCompare(JSON.stringify(right)),
    ),
  });
}

function applyDeletion(
  document: WorkflowPersistedDocumentV1,
  impact: WorkflowDeletionImpact,
): WorkflowPersistedDocumentV1 {
  const next = structuredClone(document);
  const deleted = new Set(impact.deleted_node_ids);
  const deletedEdges = new Set(impact.transition_ids);
  next.spec.nodes = (next.spec.nodes ?? [])
    .filter((node) => typeof node.id !== "string" || !deleted.has(node.id))
    .map((node) => {
      const bindings = node.input_bindings;
      const config = node.config;
      const cleanedConfig = removeDeletedConfigBindings(config, deleted);
      return {
        ...node,
        ...(bindings == null
          ? {}
          : {
              input_bindings: Object.fromEntries(
                Object.entries(bindings).map(([bindingId, binding]) => [
                  bindingId,
                  referencedDeletedNode(binding, deleted) ? null : binding,
                ]),
              ),
            }),
        ...(config == null
          ? {}
          : {
              config: Object.fromEntries(
                Object.entries(
                  cleanedConfig === REMOVED_CONFIG_BINDING
                    ? {}
                    : (cleanedConfig as Record<string, JsonValue>),
                ).filter(
                  ([field, value]) =>
                    !(
                      node.type === "loop" &&
                      ["body_entry_node_id", "body_exit_node_id"].includes(
                        field,
                      ) &&
                      typeof value === "string" &&
                      deleted.has(value)
                    ),
                ),
              ),
            }),
      };
    });
  next.spec.transitions = (next.spec.transitions ?? []).filter(
    (transition) =>
      typeof transition.id !== "string" || !deletedEdges.has(transition.id),
  );
  if (
    typeof next.spec.entry_node_id === "string" &&
    deleted.has(next.spec.entry_node_id)
  ) {
    next.spec.entry_node_id = null;
  }
  next.spec.workflow_outputs = (next.spec.workflow_outputs ?? []).map(
    (output) => ({
      ...output,
      ...(referencedDeletedNode(output.source, deleted)
        ? { source: null }
        : {}),
    }),
  );
  next.canvas.node_layouts = (next.canvas.node_layouts ?? []).filter(
    (layout) =>
      typeof layout.node_id !== "string" || !deleted.has(layout.node_id),
  );
  next.canvas.edge_layouts = (next.canvas.edge_layouts ?? []).filter(
    (layout) =>
      typeof layout.edge_id !== "string" || !deletedEdges.has(layout.edge_id),
  );
  return next;
}

function clearLoopEndpointReferencesForNode(
  document: WorkflowPersistedDocumentV1,
  nodeId: string,
  retainedLoopId: string | null,
): void {
  for (const loop of document.spec.nodes ?? []) {
    if (loop.type !== "loop" || loop.id === retainedLoopId) continue;
    const config = objectValue(loop.config);
    if (config === null) continue;
    for (const field of ["body_entry_node_id", "body_exit_node_id"]) {
      if (config[field] === nodeId) {
        delete config[field];
      }
    }
  }
}

/**
 * Create one isolated vanilla editor store. No module-level state or client is
 * retained; the owning Workbench must dispose this instance on scope teardown.
 */
export function createWorkflowEditorStore(
  input: CreateWorkflowEditorStoreInput,
): WorkflowEditorStore {
  const initialDocument = safeDocument(input.document);
  const initialSession = deepFreeze(
    workflowEditorSessionV1Schema.parse(
      structuredClone(input.editorSession ?? defaultEditorSession()),
    ),
  );
  const initialRuntime =
    input.runtimeProjection === undefined || input.runtimeProjection === null
      ? null
      : deepFreeze(
          workflowRuntimeProjectionV1Schema.parse(
            structuredClone(input.runtimeProjection),
          ),
        );
  const requestedHistoryLimit = input.historyLimit ?? MAX_HISTORY_LIMIT;
  const historyLimit = Number.isFinite(requestedHistoryLimit)
    ? Math.min(
        MAX_HISTORY_LIMIT,
        Math.max(1, Math.trunc(requestedHistoryLimit)),
      )
    : MAX_HISTORY_LIMIT;
  let state = deepFreeze<WorkflowEditorState>({
    baseline: initialDocument,
    current: initialDocument,
    dirty: false,
    history: { past: [], future: [], epoch: 0 },
    editorSession: initialSession,
    runtimeProjection: initialRuntime,
    validationIssues: validateWorkflowDraftStructureV1(initialDocument),
  });
  let disposed = false;
  let notifying = false;
  let pendingNotification = false;
  const listeners = new Set<() => void>();

  const notify = () => {
    if (disposed) return;
    if (notifying) {
      pendingNotification = true;
      return;
    }
    notifying = true;
    try {
      do {
        pendingNotification = false;
        for (const listener of [...listeners]) {
          try {
            listener();
          } catch {
            // One view subscriber is never allowed to corrupt editor authority
            // or prevent other subscribers from observing the committed state.
          }
        }
      } while (pendingNotification && !disposed);
    } finally {
      notifying = false;
    }
  };

  const replaceState = (next: WorkflowEditorState) => {
    state = deepFreeze(next);
    notify();
  };

  const replaceDocument = (
    candidate: WorkflowPersistedDocumentV1,
    editorSession = state.editorSession,
  ): WorkflowEditorCommandResult => {
    if (disposed) {
      return rejected("WORKFLOW_EDITOR_DISPOSED", "Editor scope is inactive");
    }
    let next: WorkflowPersistedDocumentV1;
    try {
      next = safeDocument(candidate);
    } catch {
      return rejected(
        "WORKFLOW_DRAFT_INVALID",
        "Command would create an invalid partial Draft shape",
      );
    }
    const documentChanged = !canonicalJsonEqual(next, state.current);
    const sessionChanged = !canonicalJsonEqual(
      editorSession,
      state.editorSession,
    );
    if (!documentChanged && !sessionChanged) {
      return { applied: false, issues: state.validationIssues };
    }
    if (!documentChanged) {
      replaceState({ ...state, editorSession });
      return { applied: false, issues: state.validationIssues };
    }
    const past = [...state.history.past, state.current].slice(-historyLimit);
    const validationIssues = validateWorkflowDraftStructureV1(next);
    replaceState({
      ...state,
      current: next,
      dirty: !canonicalJsonEqual(next, state.baseline),
      history: { ...state.history, past, future: [] },
      editorSession,
      validationIssues,
    });
    return applied(validationIssues);
  };

  const setSessionOnly = (session: WorkflowEditorSessionV1): boolean => {
    if (disposed) return false;
    let parsed: WorkflowEditorSessionV1;
    try {
      parsed = deepFreeze(
        workflowEditorSessionV1Schema.parse(structuredClone(session)),
      );
    } catch {
      return false;
    }
    if (canonicalJsonEqual(parsed, state.editorSession)) return false;
    replaceState({ ...state, editorSession: parsed });
    return true;
  };

  const applyCommand = (
    command: WorkflowEditorCommand,
  ): WorkflowEditorCommandResult => {
    if (disposed) {
      return rejected("WORKFLOW_EDITOR_DISPOSED", "Editor scope is inactive");
    }
    if (command.type === "add_node") {
      const next = structuredClone(state.current);
      const node = structuredClone(command.node) as DraftNode;
      const layout = structuredClone(command.layout) as DraftNodeLayout;
      const nodeId = stringValue(node.id);
      if (nodeId === null) {
        return rejected(
          "WORKFLOW_NODE_ID_MISSING",
          "New node identity is required",
        );
      }
      if ((next.spec.nodes ?? []).some((item) => item.id === nodeId)) {
        return rejected(
          "WORKFLOW_NODE_ID_DUPLICATE",
          "Node identity already exists",
          { node_id: nodeId },
        );
      }
      if (layout.node_id !== nodeId) {
        return rejected(
          "WORKFLOW_CANVAS_SCOPE_MISMATCH",
          "New node and Canvas layout identities must agree",
          { node_id: nodeId },
        );
      }
      next.spec.nodes = [...(next.spec.nodes ?? []), node];
      next.canvas.node_layouts = [...(next.canvas.node_layouts ?? []), layout];
      return replaceDocument(next);
    }

    if (command.type === "delete_nodes") {
      const existing = new Set(
        (state.current.spec.nodes ?? [])
          .map((node) => stringValue(node.id))
          .filter((id): id is string => id !== null),
      );
      const requested = command.node_ids.filter((id) => existing.has(id));
      if (requested.length === 0) {
        return rejected("WORKFLOW_NODE_NOT_FOUND", "No selected node exists");
      }
      const impact = analyzeWorkflowNodeDeletion(state.current, requested);
      const hasImpact =
        impact.transition_ids.length > 0 ||
        impact.binding_references.length > 0 ||
        impact.deleted_node_ids.length > impact.requested_node_ids.length;
      if (hasImpact && command.confirmed !== true) {
        return {
          applied: false,
          issues: [],
          requires_confirmation: true,
          deletion_impact: impact,
        };
      }
      return {
        ...replaceDocument(applyDeletion(state.current, impact)),
        deletion_impact: impact,
      };
    }

    if (command.type === "connect") {
      const transition = structuredClone(command.transition) as DraftTransition;
      const edgeId = stringValue(transition.id);
      if (edgeId === null) {
        return rejected(
          "WORKFLOW_TRANSITION_ID_MISSING",
          "Transition identity is required",
        );
      }
      const connectionIssues = validateWorkflowControlConnectionV1(
        state.current,
        transition,
      );
      if (connectionIssues.some((item) => item.severity === "error")) {
        return { applied: false, issues: connectionIssues };
      }
      const next = structuredClone(state.current);
      next.spec.transitions = [...(next.spec.transitions ?? []), transition];
      next.canvas.edge_layouts = [
        ...(next.canvas.edge_layouts ?? []),
        { edge_id: edgeId, routing: command.routing },
      ];
      return replaceDocument(next);
    }

    if (command.type === "disconnect") {
      const edgeIds = new Set(command.edge_ids);
      const next = structuredClone(state.current);
      const previousLength = (next.spec.transitions ?? []).length;
      next.spec.transitions = (next.spec.transitions ?? []).filter(
        (transition) =>
          typeof transition.id !== "string" || !edgeIds.has(transition.id),
      );
      if (next.spec.transitions.length === previousLength) {
        return rejected("WORKFLOW_EDGE_NOT_FOUND", "No selected edge exists");
      }
      next.canvas.edge_layouts = (next.canvas.edge_layouts ?? []).filter(
        (layout) =>
          typeof layout.edge_id !== "string" || !edgeIds.has(layout.edge_id),
      );
      return replaceDocument(next);
    }

    if (command.type === "reparent_node") {
      const next = structuredClone(state.current);
      const node = (next.spec.nodes ?? []).find(
        (item) => item.id === command.node_id,
      );
      const layout = (next.canvas.node_layouts ?? []).find(
        (item) => item.node_id === command.node_id,
      );
      if (node === undefined || layout === undefined) {
        return rejected(
          "WORKFLOW_NODE_NOT_FOUND",
          "Node and its Canvas layout must both exist",
          { node_id: command.node_id },
        );
      }
      if (command.parent_node_id === null) {
        node.scope = { kind: "root" };
        delete layout.parent_node_id;
      } else {
        const parent = (next.spec.nodes ?? []).find(
          (item) => item.id === command.parent_node_id,
        );
        const parentScope = objectValue(parent?.scope);
        if (
          parent?.type !== "loop" ||
          parentScope?.kind !== "root" ||
          ["start", "end", "loop"].includes(stringValue(node.type) ?? "")
        ) {
          return rejected(
            "WORKFLOW_REPARENT_FORBIDDEN",
            "Only a non-terminal, non-Loop node may enter a root Loop body",
            { node_id: command.node_id },
          );
        }
        node.scope = {
          kind: "loop_body",
          loop_node_id: command.parent_node_id,
        };
        layout.parent_node_id = command.parent_node_id;
      }
      clearLoopEndpointReferencesForNode(
        next,
        command.node_id,
        command.parent_node_id,
      );
      return replaceDocument(next);
    }

    if (command.type === "commit_node_position") {
      const entries = Object.entries(command.positions);
      if (
        entries.length === 0 ||
        entries.some(
          ([, position]) =>
            !Number.isFinite(position.x) || !Number.isFinite(position.y),
        )
      ) {
        return rejected(
          "WORKFLOW_CANVAS_POSITION_INVALID",
          "Committed Canvas positions must be finite",
        );
      }
      const next = structuredClone(state.current);
      const layouts = new Map(
        (next.canvas.node_layouts ?? [])
          .filter((layout) => typeof layout.node_id === "string")
          .map((layout) => [layout.node_id!, layout]),
      );
      if (entries.some(([nodeId]) => !layouts.has(nodeId))) {
        return rejected(
          "WORKFLOW_CANVAS_NODE_LAYOUT_MISSING",
          "Every moved node must have a persisted Canvas layout",
        );
      }
      for (const [nodeId, position] of entries) {
        layouts.get(nodeId)!.position = structuredClone(position);
      }
      return replaceDocument(next);
    }

    if (command.type === "update_node_config") {
      const next = structuredClone(state.current);
      const node = (next.spec.nodes ?? []).find(
        (item) => item.id === command.node_id,
      );
      if (node === undefined) {
        return rejected("WORKFLOW_NODE_NOT_FOUND", "Node does not exist", {
          node_id: command.node_id,
        });
      }
      node.config = structuredClone(command.config) as DraftNode["config"];
      return replaceDocument(next);
    }

    if (command.type === "replace_workflow_inputs") {
      const next = structuredClone(state.current);
      next.spec.workflow_inputs = structuredClone(command.workflow_inputs);
      return replaceDocument(next);
    }

    if (command.type === "replace_workflow_outputs") {
      const next = structuredClone(state.current);
      next.spec.workflow_outputs = structuredClone(command.workflow_outputs);
      return replaceDocument(next);
    }

    if (command.type === "replace_credential_slots") {
      const next = structuredClone(state.current);
      next.spec.credential_slots = structuredClone(command.credential_slots);
      return replaceDocument(next);
    }

    if (command.type === "update_node_input_bindings") {
      const next = structuredClone(state.current);
      const node = (next.spec.nodes ?? []).find(
        (item) => item.id === command.node_id,
      );
      if (node === undefined) {
        return rejected("WORKFLOW_NODE_NOT_FOUND", "Node does not exist", {
          node_id: command.node_id,
        });
      }
      node.input_bindings = structuredClone(command.input_bindings);
      return replaceDocument(next);
    }

    if (command.type === "update_node_execution_policy") {
      const next = structuredClone(state.current);
      const node = (next.spec.nodes ?? []).find(
        (item) => item.id === command.node_id,
      );
      if (node === undefined) {
        return rejected("WORKFLOW_NODE_NOT_FOUND", "Node does not exist", {
          node_id: command.node_id,
        });
      }
      node.execution_policy = structuredClone(command.execution_policy);
      return replaceDocument(next);
    }

    if (command.type === "update_node_presentation") {
      const next = structuredClone(state.current);
      const node = (next.spec.nodes ?? []).find(
        (item) => item.id === command.node_id,
      );
      if (node === undefined) {
        return rejected("WORKFLOW_NODE_NOT_FOUND", "Node does not exist", {
          node_id: command.node_id,
        });
      }
      node.custom_label = command.custom_label;
      node.description = command.description;
      return replaceDocument(next);
    }

    if (command.type === "add_loop_body_entry") {
      const next = structuredClone(state.current);
      const loop = (next.spec.nodes ?? []).find(
        (item) => item.id === command.loop_node_id,
      );
      const loopLayout = (next.canvas.node_layouts ?? []).find(
        (item) => item.node_id === command.loop_node_id,
      );
      if (loop?.type !== "loop" || objectValue(loop.scope)?.kind !== "root") {
        return rejected(
          "WORKFLOW_LOOP_NOT_FOUND",
          "Loop body entry requires an existing root Loop",
          { node_id: command.loop_node_id },
        );
      }
      if (loopLayout === undefined) {
        return rejected(
          "WORKFLOW_CANVAS_NODE_LAYOUT_MISSING",
          "Loop body entry requires the Loop Canvas layout",
          { node_id: command.loop_node_id },
        );
      }
      const config = objectValue(loop.config) ?? {};
      if (stringValue(config.body_entry_node_id) !== null) {
        return rejected(
          "WORKFLOW_LOOP_ENTRY_EXISTS",
          "Loop already has a body entry",
          { node_id: command.loop_node_id },
        );
      }
      const node = structuredClone(command.node) as DraftNode;
      const layout = structuredClone(command.layout) as DraftNodeLayout;
      const nodeId = stringValue(node.id);
      if (nodeId === null || layout.node_id !== nodeId) {
        return rejected(
          "WORKFLOW_NODE_ID_MISSING",
          "Loop body node and Canvas layout need one matching identity",
        );
      }
      if (
        (next.spec.nodes ?? []).some((item) => item.id === nodeId) ||
        (next.canvas.node_layouts ?? []).some((item) => item.node_id === nodeId)
      ) {
        return rejected(
          "WORKFLOW_NODE_ID_DUPLICATE",
          "Loop body node identity already exists",
          { node_id: nodeId },
        );
      }
      if (["start", "end", "loop"].includes(stringValue(node.type) ?? "")) {
        return rejected(
          "WORKFLOW_REPARENT_FORBIDDEN",
          "Start, End, and Loop nodes cannot enter a Loop body",
          { node_id: nodeId },
        );
      }
      const setAsExit = command.set_as_exit !== false;
      if (setAsExit && stringValue(config.body_exit_node_id) !== null) {
        return rejected(
          "WORKFLOW_LOOP_EXIT_EXISTS",
          "Loop already has a body exit",
          { node_id: command.loop_node_id },
        );
      }
      node.scope = {
        kind: "loop_body",
        loop_node_id: command.loop_node_id,
      };
      layout.parent_node_id = command.loop_node_id;
      config.body_entry_node_id = nodeId;
      if (setAsExit) config.body_exit_node_id = nodeId;
      loop.config = config as DraftNode["config"];
      next.spec.nodes = [...(next.spec.nodes ?? []), node];
      next.canvas.node_layouts = [...(next.canvas.node_layouts ?? []), layout];
      return replaceDocument(next);
    }

    if (command.type === "set_loop_body_exit") {
      const next = structuredClone(state.current);
      const loop = (next.spec.nodes ?? []).find(
        (item) => item.id === command.loop_node_id,
      );
      if (loop?.type !== "loop" || objectValue(loop.scope)?.kind !== "root") {
        return rejected(
          "WORKFLOW_LOOP_NOT_FOUND",
          "Loop body exit requires an existing root Loop",
          { node_id: command.loop_node_id },
        );
      }
      const existingConfig = objectValue(loop.config);
      if (command.node_id === null) {
        if (existingConfig === null) return replaceDocument(next);
        delete existingConfig.body_exit_node_id;
        loop.config = existingConfig as DraftNode["config"];
      } else {
        const config = existingConfig ?? {};
        const child = (next.spec.nodes ?? []).find(
          (item) => item.id === command.node_id,
        );
        const childLayout = (next.canvas.node_layouts ?? []).find(
          (item) => item.node_id === command.node_id,
        );
        const childScope = objectValue(child?.scope);
        if (
          child === undefined ||
          childScope?.kind !== "loop_body" ||
          childScope.loop_node_id !== command.loop_node_id ||
          childLayout?.parent_node_id !== command.loop_node_id
        ) {
          return rejected(
            "WORKFLOW_LOOP_CHILD_INVALID",
            "Loop exit must identify a child projected in this Loop body",
            { node_id: command.loop_node_id },
          );
        }
        config.body_exit_node_id = command.node_id;
        loop.config = config as DraftNode["config"];
      }
      return replaceDocument(next);
    }

    if ((command as { type: string }).type !== "add_next_step") {
      return rejected(
        "WORKFLOW_EDITOR_COMMAND_UNKNOWN",
        "Editor command type is unsupported",
      );
    }

    const next = structuredClone(state.current);
    const node = structuredClone(command.node) as DraftNode;
    const layout = structuredClone(command.layout) as DraftNodeLayout;
    const nodeId = stringValue(node.id);
    if (nodeId === null || layout.node_id !== nodeId) {
      return rejected(
        "WORKFLOW_NODE_ID_MISSING",
        "Next-step node and Canvas layout need one matching identity",
      );
    }
    if ((next.spec.nodes ?? []).some((item) => item.id === nodeId)) {
      return rejected(
        "WORKFLOW_NODE_ID_DUPLICATE",
        "Next-step node identity already exists",
        { node_id: nodeId },
      );
    }
    next.spec.nodes = [...(next.spec.nodes ?? []), node];
    next.canvas.node_layouts = [...(next.canvas.node_layouts ?? []), layout];
    const transition: DraftTransition = {
      id: command.transition.id,
      source: command.source,
      target: {
        node_id: nodeId,
        port_id: command.transition.target_port_id,
      },
    };
    const connectionIssues = validateWorkflowControlConnectionV1(
      next,
      transition,
    );
    if (connectionIssues.some((item) => item.severity === "error")) {
      return { applied: false, issues: connectionIssues };
    }
    next.spec.transitions = [...(next.spec.transitions ?? []), transition];
    next.canvas.edge_layouts = [
      ...(next.canvas.edge_layouts ?? []),
      {
        edge_id: command.transition.id,
        routing: command.transition.routing,
      },
    ];
    return replaceDocument(next);
  };

  const dispatch = (
    command: WorkflowEditorCommand,
  ): WorkflowEditorCommandResult => {
    try {
      return applyCommand(command);
    } catch {
      return rejected(
        "WORKFLOW_EDITOR_COMMAND_INVALID",
        "Editor command payload is invalid",
      );
    }
  };

  return {
    getState: () => state,
    subscribe(listener) {
      if (disposed) return () => undefined;
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispatch,
    undo() {
      if (disposed || state.history.past.length === 0) return false;
      const previous = state.history.past.at(-1)!;
      const past = state.history.past.slice(0, -1);
      replaceState({
        ...state,
        current: previous,
        dirty: !canonicalJsonEqual(previous, state.baseline),
        history: {
          ...state.history,
          past,
          future: [state.current, ...state.history.future].slice(
            0,
            historyLimit,
          ),
        },
        validationIssues: validateWorkflowDraftStructureV1(previous),
      });
      return true;
    },
    redo() {
      if (disposed || state.history.future.length === 0) return false;
      const [next, ...future] = state.history.future;
      replaceState({
        ...state,
        current: next!,
        dirty: !canonicalJsonEqual(next, state.baseline),
        history: {
          ...state.history,
          past: [...state.history.past, state.current].slice(-historyLimit),
          future,
        },
        validationIssues: validateWorkflowDraftStructureV1(next!),
      });
      return true;
    },
    markSaved(baseline = state.current) {
      if (disposed) return false;
      let saved: WorkflowPersistedDocumentV1;
      try {
        saved = safeDocument(baseline);
      } catch {
        return false;
      }
      const dirty = !canonicalJsonEqual(state.current, saved);
      if (canonicalJsonEqual(state.baseline, saved) && state.dirty === dirty) {
        return false;
      }
      replaceState({ ...state, baseline: saved, dirty });
      return true;
    },
    beginPublishEpoch() {
      if (disposed) return;
      replaceState({
        ...state,
        baseline: state.current,
        dirty: false,
        history: {
          past: [],
          future: [],
          epoch: state.history.epoch + 1,
        },
      });
    },
    setEditorSession: setSessionOnly,
    setRuntimeProjection(runtime) {
      if (disposed) return false;
      let parsed: WorkflowRuntimeProjectionV1 | null;
      try {
        parsed =
          runtime === null
            ? null
            : deepFreeze(
                workflowRuntimeProjectionV1Schema.parse(
                  structuredClone(runtime),
                ),
              );
      } catch {
        return false;
      }
      if (canonicalJsonEqual(parsed, state.runtimeProjection)) return false;
      replaceState({ ...state, runtimeProjection: parsed });
      return true;
    },
    setValidationIssues(issues) {
      if (disposed) return false;
      let parsed: WorkflowValidationIssueV1[];
      try {
        parsed = deepFreeze(
          workflowValidationIssueV1Schema
            .array()
            .max(1_024)
            .parse(structuredClone(issues)),
        );
      } catch {
        return false;
      }
      if (canonicalJsonEqual(parsed, state.validationIssues)) return false;
      replaceState({ ...state, validationIssues: parsed });
      return true;
    },
    beginNodeDrag(nodeIds) {
      if (disposed || state.editorSession.interaction.kind !== "idle") {
        return false;
      }
      const uniqueNodeIds = [...new Set(nodeIds)];
      if (uniqueNodeIds.length === 0) return false;
      const layouts = new Map(
        (state.current.canvas.node_layouts ?? [])
          .filter(
            (layout) =>
              typeof layout.node_id === "string" &&
              typeof layout.position?.x === "number" &&
              typeof layout.position.y === "number" &&
              Number.isFinite(layout.position.x) &&
              Number.isFinite(layout.position.y),
          )
          .map((layout) => [
            layout.node_id!,
            { x: layout.position!.x!, y: layout.position!.y! },
          ]),
      );
      if (uniqueNodeIds.some((nodeId) => !layouts.has(nodeId))) return false;
      return setSessionOnly({
        ...state.editorSession,
        interaction: {
          kind: "node_drag",
          node_ids: uniqueNodeIds,
          transient_positions: Object.fromEntries(
            uniqueNodeIds.map((nodeId) => [nodeId, layouts.get(nodeId)!]),
          ),
        },
      });
    },
    updateNodeDragPosition(nodeId, position) {
      if (
        disposed ||
        state.editorSession.interaction.kind !== "node_drag" ||
        !state.editorSession.interaction.node_ids.includes(nodeId) ||
        !Number.isFinite(position.x) ||
        !Number.isFinite(position.y)
      ) {
        return false;
      }
      return setSessionOnly({
        ...state.editorSession,
        interaction: {
          ...state.editorSession.interaction,
          transient_positions: {
            ...state.editorSession.interaction.transient_positions,
            [nodeId]: position,
          },
        },
      });
    },
    commitNodeDrag() {
      if (disposed || state.editorSession.interaction.kind !== "node_drag") {
        return false;
      }
      const next = structuredClone(state.current);
      const positions = state.editorSession.interaction.transient_positions;
      const layouts = new Map(
        (next.canvas.node_layouts ?? [])
          .filter((layout) => typeof layout.node_id === "string")
          .map((layout) => [layout.node_id!, layout]),
      );
      if (Object.keys(positions).some((nodeId) => !layouts.has(nodeId))) {
        return false;
      }
      for (const [nodeId, position] of Object.entries(positions)) {
        layouts.get(nodeId)!.position = structuredClone(position);
      }
      const idleSession = deepFreeze(
        workflowEditorSessionV1Schema.parse({
          ...state.editorSession,
          interaction: { kind: "idle" },
        }),
      );
      if (canonicalJsonEqual(next, state.current)) {
        setSessionOnly(idleSession);
        return true;
      }
      return replaceDocument(next, idleSession).applied;
    },
    cancelNodeDrag() {
      if (disposed || state.editorSession.interaction.kind !== "node_drag") {
        return false;
      }
      return setSessionOnly({
        ...state.editorSession,
        interaction: { kind: "idle" },
      });
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      listeners.clear();
      pendingNotification = false;
    },
  };
}

/** Full published-shape view for adapters that cannot project a partial Draft. */
export function selectStrictWorkflowDocument(
  state: Pick<WorkflowEditorState, "current">,
): StrictWorkflowEditorDocument | null {
  const spec = workflowSpecV1Schema.safeParse(state.current.spec);
  const canvas = canvasDocumentV1Schema.safeParse(state.current.canvas);
  return spec.success && canvas.success
    ? { spec: spec.data, canvas: canvas.data }
    : null;
}

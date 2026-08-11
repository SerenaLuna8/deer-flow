import { describe, expect, it, rs } from "@rstest/core";
import { ReactFlowProvider } from "@xyflow/react";
import { renderToStaticMarkup } from "react-dom/server";

import type { WorkflowFlowNode } from "@/components/projects/workflows/canvas/workflow-canvas-adapter";
import {
  PYTHON_CODE_WORKFLOW_NODE_CONFIG_PANELS,
  PYTHON_WORKFLOW_EDITOR_POLICY,
  PythonCodeNodeConfigPanel,
  WorkflowPythonEditor,
  appendPythonInputVariable,
  buildPythonCodeConfigUpdate,
  createPythonSourceController,
  movePythonInputVariable,
  parsePythonOutputSchema,
  removePythonInputVariable,
  utf8ByteLength,
} from "@/components/projects/workflows/node-config/code";
import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import {
  PYTHON_CODE_WORKFLOW_NODE_CARDS,
  PythonCodeWorkflowNode,
} from "@/components/projects/workflows/nodes/python-code-node-card";
import { WorkflowWorkbenchFlushProvider } from "@/components/projects/workflows/workbench/workbench-flush-context";
import {
  WorkflowWorkbenchStoreProvider,
  type WorkflowWorkbenchStorePort,
  type WorkflowWorkbenchStoreSnapshot,
} from "@/components/projects/workflows/workbench/workbench-store-context";
import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import {
  workflowNodeRegistryV1,
  type NodeCatalogEntry,
} from "@/core/project-workflows/catalog";
import { createWorkflowEditorFlushRegistry } from "@/core/project-workflows/editor/flush-registry";
import type { PythonCodeNodeConfigV1 } from "@/core/project-workflows/types";

const NODE_ID = "30000000-0000-4000-8000-000000000001";
const VARIABLE_ID = "30000000-0000-4000-8000-000000000002";
const SECOND_VARIABLE_ID = "30000000-0000-4000-8000-000000000003";

const CONFIG: PythonCodeNodeConfigV1 = {
  source: "def main(inputs):\n    return {'ok': True}\n",
  input_variables: [
    {
      id: VARIABLE_ID,
      name: "prompt",
      value_type: { kind: "string", collection: false, nullable: false },
    },
  ],
  output_schema: {
    type: "object",
    properties: { ok: { type: "boolean" } },
    required: ["ok"],
    additionalProperties: false,
  },
  timeout_ms: 2_000,
};

function catalogEntry(
  availability: "enabled" | "disabled" = "enabled",
  withLimits = true,
): NodeCatalogEntry {
  const definition = workflowNodeRegistryV1.find(
    (candidate) => candidate.type === "python_code",
  );
  if (!definition) throw new Error("missing python_code registry entry");
  return {
    definition,
    availability:
      availability === "enabled"
        ? { state: "enabled" }
        : {
            state: "disabled",
            reason_code: "WORKFLOW_CODE_PROFILE_UNAVAILABLE",
          },
    ...(withLimits
      ? { public_limits: { max_source_bytes: 64, max_timeout_ms: 5_000 } }
      : {}),
  };
}

function documentFor(
  config: NonNullable<WorkflowNodeConfigPanelProps["node"]["config"]>,
): WorkflowPersistedDocumentV1 {
  return {
    spec: {
      schema_version: 1,
      workflow_inputs: [
        {
          id: "question",
          name: "question",
          label: "问题",
          description: null,
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
          id: NODE_ID,
          type: "python_code",
          type_version: 1,
          scope: { kind: "root" },
          custom_label: null,
          description: null,
          input_bindings: {
            [VARIABLE_ID]: { kind: "workflow_input", input_id: "question" },
          },
          execution_policy: {
            retry: { mode: "none" },
            on_error: { mode: "fail_workflow" },
          },
          config,
        },
      ],
    },
    canvas: {
      schema_version: 1,
      node_layouts: [{ node_id: NODE_ID, position: { x: 0, y: 0 } }],
    },
  };
}

function storeFor(document: WorkflowPersistedDocumentV1) {
  const snapshot: WorkflowWorkbenchStoreSnapshot = {
    baseline: document,
    current: document,
    dirty: false,
    history: { past: [], future: [], epoch: 0 },
    editorSession: {
      schema_version: 1,
      viewport: { x: 0, y: 0, zoom: 1 },
      selection: { node_ids: [NODE_ID], edge_ids: [] },
      inspector: {
        open: true,
        node_id: NODE_ID,
        tab: "settings",
        width_px: 480,
        expanded_section_ids: [],
        scroll_top: 0,
      },
      palette: { open: false, anchor: null },
      interaction: { kind: "idle" },
    },
    runtimeProjection: null,
    validationIssues: [],
  };
  const dispatch = rs.fn(() => ({ applied: true, issues: [] }));
  const store: WorkflowWorkbenchStorePort = {
    dispatch,
    getState: () => snapshot,
    redo: rs.fn(() => false),
    setEditorSession: rs.fn(() => true),
    subscribe: () => () => undefined,
    undo: rs.fn(() => false),
  };
  return { dispatch, store };
}

function renderPanel({
  availability = "enabled",
  capabilities = ["workflow.read", "workflow.edit", "workflow.code.use"],
  config = CONFIG,
  disabled = false,
  readOnly = false,
  withLimits = true,
}: {
  availability?: "enabled" | "disabled";
  capabilities?: WorkflowNodeConfigPanelProps["capabilities"];
  config?: NonNullable<WorkflowNodeConfigPanelProps["node"]["config"]>;
  disabled?: boolean;
  readOnly?: boolean;
  withLimits?: boolean;
} = {}): string {
  const document = documentFor(config);
  const node = document.spec.nodes?.[0];
  if (!node) throw new Error("node fixture missing");
  const flushRegistry = createWorkflowEditorFlushRegistry();
  return renderToStaticMarkup(
    <WorkflowWorkbenchStoreProvider store={storeFor(document).store}>
      <WorkflowWorkbenchFlushProvider registry={flushRegistry}>
        <PythonCodeNodeConfigPanel
          capabilities={capabilities}
          catalogEntry={catalogEntry(availability, withLimits)}
          disabled={disabled}
          document={document}
          locale="zh-CN"
          node={node}
          nodeId={NODE_ID}
          readOnly={readOnly}
        />
      </WorkflowWorkbenchFlushProvider>
    </WorkflowWorkbenchStoreProvider>,
  );
}

describe("G20-E Python Code Workflow node", () => {
  it("freezes the dedicated panel and card exports", () => {
    expect(PYTHON_CODE_WORKFLOW_NODE_CONFIG_PANELS).toEqual({
      python_code: PythonCodeNodeConfigPanel,
    });
    expect(PYTHON_CODE_WORKFLOW_NODE_CARDS).toEqual({
      python_code: PythonCodeWorkflowNode,
    });
  });

  it("coalesces local transactions into one latest-source Workflow command on flush", () => {
    const registry = createWorkflowEditorFlushRegistry();
    const commits: string[] = [];
    const controller = createPythonSourceController({
      flushKey: `python:${NODE_ID}`,
      initialSource: "old",
      maxBytes: 64,
      registry,
      commitSource: (source) => {
        commits.push(source);
        return { applied: true };
      },
    });

    controller.edit("first transaction");
    controller.edit("latest transaction");
    expect(controller.getState()).toMatchObject({
      buffer: "latest transaction",
      dirty: true,
      generation: 2,
    });
    expect(registry.hasPending()).toBe(true);

    registry.flushAll();
    expect(commits).toEqual(["latest transaction"]);
    expect(controller.getState()).toMatchObject({
      buffer: "latest transaction",
      dirty: false,
      issue: null,
    });
  });

  it("debounces multiple editor transactions into one Workflow command", async () => {
    const registry = createWorkflowEditorFlushRegistry();
    const commits: string[] = [];
    const controller = createPythonSourceController({
      debounceMs: 0,
      flushKey: `python:${NODE_ID}`,
      initialSource: "old",
      maxBytes: 64,
      registry,
      commitSource: (source) => {
        commits.push(source);
        return { applied: true };
      },
    });

    controller.edit("first transaction");
    controller.edit("latest transaction");
    await new Promise((resolve) => setTimeout(resolve, 5));

    expect(commits).toEqual(["latest transaction"]);
    expect(controller.getState().dirty).toBe(false);
    expect(registry.hasPending()).toBe(false);
  });

  it("does not let a stale editor debounce commit across a newer same-key generation", async () => {
    const registry = createWorkflowEditorFlushRegistry();
    const commits: string[] = [];
    const commitSource = (source: string) => {
      commits.push(source);
      return { applied: true as const };
    };
    const stale = createPythonSourceController({
      debounceMs: 0,
      flushKey: `python:${NODE_ID}`,
      initialSource: "old",
      maxBytes: 64,
      registry,
      commitSource,
    });
    stale.edit("stale buffer");

    const current = createPythonSourceController({
      debounceMs: Number.POSITIVE_INFINITY,
      flushKey: `python:${NODE_ID}`,
      initialSource: "old",
      maxBytes: 64,
      registry,
      commitSource,
    });
    current.edit("current buffer");
    await new Promise((resolve) => setTimeout(resolve, 5));

    expect(commits).toEqual([]);
    registry.flushAll();
    expect(commits).toEqual(["current buffer"]);
  });

  it("keeps failed source pending and retries without losing the local buffer", () => {
    const registry = createWorkflowEditorFlushRegistry();
    let succeeds = false;
    const commitSource = rs.fn((source: string) => ({
      applied: succeeds && source === "latest",
      safeMessage: succeeds ? undefined : "Draft command rejected",
    }));
    const controller = createPythonSourceController({
      flushKey: `python:${NODE_ID}`,
      initialSource: "old",
      maxBytes: 64,
      registry,
      commitSource,
    });
    controller.edit("latest");

    expect(() => registry.flushAll()).toThrow(AggregateError);
    expect(registry.hasPending()).toBe(true);
    expect(controller.getState()).toMatchObject({
      buffer: "latest",
      dirty: true,
      issue: "Draft command rejected",
    });

    succeeds = true;
    registry.flushAll();
    expect(commitSource).toHaveBeenCalledTimes(2);
    expect(controller.getState().dirty).toBe(false);
  });

  it("does not let external Store updates overwrite a dirty editor generation", () => {
    const registry = createWorkflowEditorFlushRegistry();
    const controller = createPythonSourceController({
      flushKey: `python:${NODE_ID}`,
      initialSource: "persisted-v1",
      maxBytes: 64,
      registry,
      commitSource: () => ({ applied: true }),
    });
    controller.edit("local-dirty");
    controller.receiveExternalSource("persisted-v2");
    expect(controller.getState()).toMatchObject({
      buffer: "local-dirty",
      persistedSource: "persisted-v2",
      dirty: true,
    });

    controller.commit();
    controller.receiveExternalSource("persisted-v3");
    expect(controller.getState()).toMatchObject({
      buffer: "persisted-v3",
      persistedSource: "persisted-v3",
      dirty: false,
    });
  });

  it("counts UTF-8 bytes and blocks an oversized generation without dispatch", () => {
    expect(utf8ByteLength("a中")).toBe(4);
    const registry = createWorkflowEditorFlushRegistry();
    const commitSource = rs.fn(() => ({ applied: true as const }));
    const controller = createPythonSourceController({
      flushKey: `python:${NODE_ID}`,
      initialSource: "",
      maxBytes: 3,
      registry,
      commitSource,
    });
    controller.edit("a中");
    expect(() => controller.commit()).toThrow(/UTF-8/iu);
    expect(commitSource).not.toHaveBeenCalled();
    expect(controller.getState().dirty).toBe(true);
    expect(registry.hasPending()).toBe(true);
  });

  it("preserves stable input IDs/order and builds only strict Python config", () => {
    const node = documentFor(CONFIG).spec.nodes![0]!;
    const added = appendPythonInputVariable(CONFIG, {
      id: SECOND_VARIABLE_ID,
      name: "count",
    });
    expect(added.input_variables.map((variable) => variable.id)).toEqual([
      VARIABLE_ID,
      SECOND_VARIABLE_ID,
    ]);
    const moved = movePythonInputVariable(added, 1, 0);
    expect(moved.input_variables.map((variable) => variable.id)).toEqual([
      SECOND_VARIABLE_ID,
      VARIABLE_ID,
    ]);
    expect(removePythonInputVariable(moved, 0).input_variables[0]?.id).toBe(
      VARIABLE_ID,
    );

    expect(
      buildPythonCodeConfigUpdate(node, {
        source: "def main(inputs):\n    return {}\n",
        timeout_ms: null,
      }),
    ).toMatchObject({
      type: "update_node_config",
      node_id: NODE_ID,
      config: {
        source: "def main(inputs):\n    return {}\n",
        input_variables: [{ id: VARIABLE_ID, name: "prompt" }],
        timeout_ms: null,
      },
    });

    const injectedNode = documentFor({
      ...CONFIG,
      runtime_profile: "must-not-survive",
      credential_id: "must-not-survive",
      secret: "must-not-survive",
    }).spec.nodes![0]!;
    const strictUpdate = buildPythonCodeConfigUpdate(injectedNode, {
      source: "def main(inputs):\n    return {}\n",
    });
    expect(Object.keys(strictUpdate.config).sort()).toEqual([
      "input_variables",
      "output_schema",
      "source",
      "timeout_ms",
    ]);
    expect(JSON.stringify(strictUpdate.config)).not.toContain(
      "must-not-survive",
    );
  });

  it("accepts only a strict JSON object output schema", () => {
    expect(parsePythonOutputSchema('{"type":"object"}')).toEqual({
      success: true,
      schema: { type: "object" },
    });
    expect(parsePythonOutputSchema("[]")).toEqual({
      success: false,
      issue: "输出 Schema 必须是 JSON object。",
    });
    expect(parsePythonOutputSchema('{"type":"object",}')).toEqual({
      success: false,
      issue: "输出 Schema 不是合法 JSON。",
    });
    for (const invalid of [
      '{"type":"array","items":{"type":"string"}}',
      '{"type":["object","null"]}',
      '{"type":"object","unknownKeyword":true}',
      '{"type":"object","required":["missing"]}',
    ]) {
      expect(parsePythonOutputSchema(invalid)).toEqual({
        success: false,
        issue: "输出 Schema 必须是受支持的 non-null object JSON Schema。",
      });
    }
  });

  it("renders one Python-only controlled editor without execution or network controls", () => {
    const html = renderToStaticMarkup(
      <WorkflowPythonEditor
        disabled={false}
        error={null}
        maxBytes={64}
        onBlurCommit={() => undefined}
        onChange={() => undefined}
        onExplicitCommit={() => undefined}
        readOnly={false}
        value={CONFIG.source}
      />,
    );
    expect(PYTHON_WORKFLOW_EDITOR_POLICY).toEqual({
      language: "python",
      executesCode: false,
      networkAccess: false,
    });
    expect(html).toContain('data-workflow-python-editor="true"');
    expect(html).toContain("Python only");
    expect(html).not.toContain("JavaScript");
    expect(html).not.toContain("Shell");
    expect(html).not.toContain("运行代码");
  });

  it("renders partial Draft issues and bounds source/timeout from Catalog", () => {
    const html = renderPanel({ config: {} });
    expect(html).toContain("Python 3.12");
    expect(html).toContain("source 尚未配置");
    expect(html).toContain("64 UTF-8 bytes");
    expect(html).toContain('max="5000"');
    expect(html).toContain("main(inputs)");
    for (const forbidden of [
      "language selector",
      "command",
      "packages",
      "network",
      "mounts",
      "executor",
      "import path",
    ]) {
      expect(html).not.toContain(forbidden);
    }
  });

  it.each([
    { readOnly: true },
    { disabled: true },
    { capabilities: ["workflow.read"] as const },
    { availability: "disabled" as const },
    { withLimits: false },
  ])("fails closed and preserves source for $readOnly", (lock) => {
    const html = renderPanel(lock);
    expect(html).toMatch(/<fieldset[^>]*disabled/u);
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain("def main(inputs)");
  });

  it("renders a safe Python card without source, inputs, runtime IDs, or secrets", () => {
    const data: WorkflowFlowNode["data"] = {
      nodeId: NODE_ID,
      nodeKind: "python_code",
      originalType: "python_code",
      title: "代码执行",
      supportState: "supported",
      statusLabel: "状态：可用",
      availabilityReason: null,
      readOnly: false,
      disabled: false,
      inputPorts: [],
      outputPorts: [],
      focusedPortId: null,
      portSignature: "safe-signature",
      source: "must-not-render",
      inputs: "must-not-render",
      provider_id: "must-not-render",
      sandbox_path: "must-not-render",
      secret: "must-not-render",
    };
    const html = renderToStaticMarkup(
      <ReactFlowProvider>
        <PythonCodeWorkflowNode
          data={data}
          deletable
          draggable
          dragging={false}
          id={NODE_ID}
          isConnectable
          positionAbsoluteX={0}
          positionAbsoluteY={0}
          selectable
          selected={false}
          type="python_code"
          zIndex={0}
        />
      </ReactFlowProvider>,
    );
    expect(html).toContain('data-python-code-node-card="true"');
    expect(html).toContain("Python 3.12");
    expect(html).toContain("隔离 Sandbox");
    expect(html).toContain('aria-label="工作流节点：代码执行"');
    expect(html).not.toContain("must-not-render");
  });
});

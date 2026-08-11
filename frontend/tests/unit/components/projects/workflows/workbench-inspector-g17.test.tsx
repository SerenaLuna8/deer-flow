import { describe, expect, rs, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  commitWorkflowNodePresentation,
  focusWorkflowInspectorIssue,
  WorkflowInspector,
  WorkflowInspectorSection,
} from "@/components/projects/workflows/inspector/workflow-inspector";
import {
  commitWorkflowNextStep,
  filterWorkflowNextStepCandidates,
  updateWorkflowInspectorWidth,
  workflowHistoryShortcutAction,
  WorkflowWorkbench,
  WorkflowWorkbenchStoreProvider,
  type WorkflowWorkbenchStorePort,
  type WorkflowWorkbenchStoreSnapshot,
} from "@/components/projects/workflows/workbench/workflow-workbench";
import {
  workflowNodeRegistryV1,
  type NodeCatalogEntry,
  type PortDefinition,
} from "@/core/project-workflows/catalog";
import {
  createWorkflowEditorStore,
  type WorkflowEditorCommandResult,
} from "@/core/project-workflows/editor/store";
import type { WorkflowNodeLastRunV1 } from "@/core/project-workflows/transport";
import type { Capability } from "@/core/projects/types";

const NODE_ID = "11111111-1111-4111-8111-111111111111";
const RUN_ID = "22222222-2222-4222-8222-222222222222";

function createSnapshot(
  overrides: Partial<WorkflowWorkbenchStoreSnapshot> = {},
): WorkflowWorkbenchStoreSnapshot {
  const current = {
    spec: {
      schema_version: 1 as const,
      nodes: [
        {
          id: NODE_ID,
          type: "llm",
          type_version: 1,
          custom_label: "生成摘要",
          description: "把输入压缩为一段安全摘要",
          config: {},
        },
      ],
    },
    canvas: { schema_version: 1 as const },
  };

  return {
    baseline: current,
    current,
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
        expanded_section_ids: ["inputs"],
        scroll_top: 0,
      },
      palette: { open: false, anchor: null },
      interaction: { kind: "idle" },
    },
    runtimeProjection: null,
    validationIssues: [],
    ...overrides,
  };
}

function createStore(
  snapshot = createSnapshot(),
  dispatchResult: WorkflowEditorCommandResult = { applied: true, issues: [] },
) {
  let state = snapshot;
  const listeners = new Set<() => void>();
  const commands: unknown[] = [];
  const undo = rs.fn();
  const redo = rs.fn();
  const dispatch = rs.fn((command: unknown) => {
    commands.push(command);
    return dispatchResult;
  });
  const setEditorSession = rs.fn(
    (editorSession: WorkflowWorkbenchStoreSnapshot["editorSession"]) => {
      state = { ...state, editorSession };
      listeners.forEach((listener) => listener());
      return true;
    },
  );

  const store: WorkflowWorkbenchStorePort = {
    getState: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispatch,
    undo,
    redo,
    setEditorSession,
  };

  return { commands, dispatch, redo, setEditorSession, store, undo };
}

function catalogEntry(
  type: (typeof workflowNodeRegistryV1)[number]["type"],
  enabled = true,
): NodeCatalogEntry {
  const definition = workflowNodeRegistryV1.find(
    (candidate) => candidate.type === type,
  );
  if (!definition) throw new Error(`missing registry entry: ${type}`);
  return {
    definition,
    availability: enabled
      ? { state: "enabled" }
      : {
          state: "disabled",
          reason_code: "WORKFLOW_NODE_NOT_ALLOWED",
        },
  };
}

const CONTROL_OUTPUT: PortDefinition = {
  id: "next",
  title_i18n: { "zh-CN": "下一步", "en-US": "Next" },
  kind: "control",
  value_type: null,
  cardinality: "one",
  required: true,
};

function renderWorkbench(
  store: WorkflowWorkbenchStorePort,
  capabilities: readonly Capability[] = [
    "workflow.read",
    "workflow.edit",
    "workflow.publish",
    "workflow.execute",
  ],
) {
  return renderToStaticMarkup(
    <WorkflowWorkbench
      store={store}
      name="客户回访流程"
      versionLabel="草稿"
      capabilities={capabilities}
      onBack={() => undefined}
      onSave={() => undefined}
      onValidate={() => undefined}
      onPublish={() => undefined}
      onRun={() => undefined}
      palette={<div>节点目录</div>}
      canvas={<div>流程画布</div>}
      inspector={<div>节点检查器</div>}
      runPanel={<div>运行时间线</div>}
    />,
  );
}

describe("G17 Workflow Workbench and Inspector shell", () => {
  test("renders the five-zone layout and accessible whole-workflow actions", () => {
    const snapshot = createSnapshot({ dirty: true });
    snapshot.history = {
      past: [snapshot.current],
      future: [snapshot.current],
      epoch: 2,
    };
    const { store } = createStore(snapshot);
    const html = renderWorkbench(store);

    for (const slot of [
      "workflow-workbench-header",
      "workflow-workbench-palette",
      "workflow-workbench-canvas",
      "workflow-workbench-inspector",
      "workflow-workbench-run-panel",
    ]) {
      expect(html).toContain(`data-slot="${slot}"`);
    }
    expect(html).toContain("未保存");
    expect(html).toContain('aria-label="撤销"');
    expect(html).toContain('aria-label="重做"');
    expect(html).toContain('aria-label="保存草稿"');
    expect(html).toContain('aria-label="校验工作流"');
    expect(html).toContain('aria-label="发布工作流"');
    expect(html).toContain('aria-label="运行工作流"');
    expect(html).not.toContain("运行节点");
    expect(html).not.toContain("单节点执行");
  });

  test("requires a saved Draft before validation or publication", () => {
    const dirty = renderWorkbench(
      createStore(createSnapshot({ dirty: true })).store,
    );
    expect(dirty).toMatch(/aria-label="保存草稿"[^>]*>/);
    expect(dirty).toMatch(/aria-label="校验工作流"[^>]*disabled/);
    expect(dirty).toMatch(/aria-label="发布工作流"[^>]*disabled/);

    const saved = renderWorkbench(
      createStore(createSnapshot({ dirty: false })).store,
    );
    expect(saved).toMatch(/aria-label="保存草稿"[^>]*disabled/);
    expect(saved).not.toMatch(/aria-label="校验工作流"[^>]*disabled/);
  });

  test("keeps publish independent from Draft edit capability", () => {
    const html = renderWorkbench(
      createStore(createSnapshot({ dirty: false })).store,
      ["workflow.read", "workflow.publish"],
    );

    expect(html).toMatch(/aria-label="保存草稿"[^>]*disabled/);
    expect(html).toMatch(/aria-label="校验工作流"[^>]*disabled/);
    expect(html).not.toMatch(/aria-label="发布工作流"[^>]*disabled/);
  });

  test("keeps each provider instance isolated", () => {
    const saved = createStore(createSnapshot({ dirty: false }));
    const dirty = createStore(createSnapshot({ dirty: true }));

    const html = renderToStaticMarkup(
      <div>
        <WorkflowWorkbench
          store={saved.store}
          name="A"
          capabilities={["workflow.read"]}
        />
        <WorkflowWorkbench
          store={dirty.store}
          name="B"
          capabilities={["workflow.read"]}
        />
      </div>,
    );

    expect(html.match(/data-save-state="saved"/gu)).toHaveLength(1);
    expect(html.match(/data-save-state="dirty"/gu)).toHaveLength(1);
  });

  test("mounts the editor-core store API directly without a singleton adapter", () => {
    const snapshot = createSnapshot();
    const first = createWorkflowEditorStore({
      document: snapshot.current,
      editorSession: snapshot.editorSession,
    });
    const second = createWorkflowEditorStore({
      document: snapshot.current,
      editorSession: snapshot.editorSession,
    });

    expect(
      first.dispatch({
        type: "update_node_config",
        node_id: NODE_ID,
        config: { model_ref: "default" },
      }).applied,
    ).toBe(true);

    const html = renderToStaticMarkup(
      <div>
        <WorkflowWorkbench
          store={first}
          name="A"
          capabilities={["workflow.read", "workflow.edit"]}
        />
        <WorkflowWorkbench
          store={second}
          name="B"
          capabilities={["workflow.read", "workflow.edit"]}
        />
      </div>,
    );

    expect(html.match(/data-save-state="dirty"/gu)).toHaveLength(1);
    expect(html.match(/data-save-state="saved"/gu)).toHaveLength(1);
    first.dispose();
    second.dispose();
  });

  test("loads a legal partial Draft without publish-complete parsing", () => {
    const partial = createSnapshot();
    partial.current = {
      spec: {
        schema_version: 1,
        nodes: [{ id: NODE_ID, type: "llm", config: {} }],
      },
      canvas: { schema_version: 1 },
    };
    partial.validationIssues = [
      {
        severity: "error",
        code: "WORKFLOW_NODE_INCOMPLETE",
        message: "请选择模型并填写消息模板",
        path: ["nodes", "0", "config"],
        node_id: NODE_ID,
      },
    ];
    const { store } = createStore(partial);

    const html = renderToStaticMarkup(
      <WorkflowWorkbenchStoreProvider store={store}>
        <WorkflowInspector
          capabilities={["workflow.read", "workflow.edit"]}
          catalog={[catalogEntry("llm")]}
          sourcePorts={[CONTROL_OUTPUT]}
        >
          <WorkflowInspectorSection
            id="inputs"
            title="输入"
            required
            help="配置节点输入"
          >
            <label>
              模型
              <input name="model" />
            </label>
          </WorkflowInspectorSection>
        </WorkflowInspector>
      </WorkflowWorkbenchStoreProvider>,
    );

    expect(html).toContain("大模型");
    expect(html).toContain("配置不完整");
    expect(html).toContain("请选择模型并填写消息模板");
    expect(html).toContain("设置");
    expect(html).toContain("上次运行");
    expect(html).toContain('data-inspector-width="480"');
    expect(html).toMatch(/<fieldset[^>]*disabled/u);
  });

  test("keeps read-only settings viewable while disabling authored fields", () => {
    const { store } = createStore();

    const html = renderToStaticMarkup(
      <WorkflowWorkbenchStoreProvider store={store}>
        <WorkflowInspector
          capabilities={["workflow.read"]}
          catalog={[catalogEntry("llm")]}
          readOnly
          sourcePorts={[CONTROL_OUTPUT]}
        >
          <WorkflowInspectorSection id="inputs" title="输入">
            <label>
              模型
              <input name="model" />
            </label>
          </WorkflowInspectorSection>
        </WorkflowInspector>
      </WorkflowWorkbenchStoreProvider>,
    );

    expect(html).toContain("只读");
    expect(html).toMatch(/<fieldset[^>]*disabled/u);
    expect(html).toMatch(
      /<button[^>]*data-slot="collapsible-trigger"(?![^>]*disabled)/u,
    );
    expect(html).toContain("生成摘要");
  });

  test("renders the closed specialized node panel when no override is supplied", () => {
    const { store } = createStore();

    const html = renderToStaticMarkup(
      <WorkflowWorkbenchStoreProvider store={store}>
        <WorkflowInspector
          capabilities={["workflow.read", "workflow.edit"]}
          catalog={[catalogEntry("llm")]}
          modelCatalog={{
            status: "ready",
            models: [
              {
                name: "primary-chat",
                display_name: "Primary Chat",
                supports_thinking: true,
                supports_reasoning_effort: true,
                workflow_authoring: {
                  modes: ["chat"],
                  supports_streaming: true,
                  parameters: [],
                },
              },
            ],
          }}
          sourcePorts={[CONTROL_OUTPUT]}
        />
      </WorkflowWorkbenchStoreProvider>,
    );

    expect(html).toContain("大模型配置");
    expect(html).toContain('aria-label="逻辑模型引用"');
    expect(html).toContain("Primary Chat");
    expect(html).not.toContain("专用配置将在节点模块中提供");
  });

  test("opens known v1 nodes with omitted or null config as editable partial panels", () => {
    const omittedConfig = createSnapshot();
    omittedConfig.current = {
      spec: {
        schema_version: 1,
        nodes: [{ id: NODE_ID, type: "llm", type_version: 1 }],
      },
      canvas: { schema_version: 1 },
    };
    const nullConfig = createSnapshot();
    nullConfig.current = {
      spec: {
        schema_version: 1,
        nodes: [
          { id: NODE_ID, type: "transform", type_version: 1, config: null },
        ],
      },
      canvas: { schema_version: 1 },
    };

    const renderKnownPartial = (
      snapshot: WorkflowWorkbenchStoreSnapshot,
      entry: NodeCatalogEntry,
      disabled = false,
    ) =>
      renderToStaticMarkup(
        <WorkflowWorkbenchStoreProvider store={createStore(snapshot).store}>
          <WorkflowInspector
            capabilities={["workflow.read", "workflow.edit"]}
            catalog={[entry]}
            disabled={disabled}
            modelCatalog={{
              status: "ready",
              models: [
                {
                  name: "primary-chat",
                  display_name: "Primary Chat",
                  supports_thinking: true,
                  supports_reasoning_effort: true,
                  workflow_authoring: {
                    modes: ["chat"],
                    supports_streaming: true,
                    parameters: [],
                  },
                },
              ],
            }}
            sourcePorts={[]}
          />
        </WorkflowWorkbenchStoreProvider>,
      );

    const llmHtml = renderKnownPartial(omittedConfig, catalogEntry("llm"));
    const transformHtml = renderKnownPartial(
      nullConfig,
      catalogEntry("transform"),
    );
    const disabledHtml = renderKnownPartial(
      omittedConfig,
      catalogEntry("llm"),
      true,
    );

    for (const html of [llmHtml, transformHtml]) {
      expect(html).toContain("配置不完整");
      expect(html).not.toContain("只读");
      expect(html).toContain('aria-disabled="false"');
      expect(html).not.toMatch(/aria-label="节点实例名称"[^>]*disabled/u);
    }
    expect(llmHtml).toContain('aria-label="大模型配置"');
    expect(llmHtml).toContain('aria-label="逻辑模型引用"');
    expect(transformHtml).toContain('aria-label="模板转换配置"');
    expect(disabledHtml).toContain("只读");
    expect(disabledHtml).toContain('aria-disabled="true"');
    expect(disabledHtml).toMatch(/aria-label="节点实例名称"[^>]*disabled/u);
  });

  test("fails closed for an unsupported node type", () => {
    const snapshot = createSnapshot();
    snapshot.current = {
      spec: {
        schema_version: 1,
        nodes: [{ id: NODE_ID, type: "future_secret_node", config: {} }],
      },
      canvas: { schema_version: 1 },
    };
    const { store } = createStore(snapshot);

    const html = renderToStaticMarkup(
      <WorkflowWorkbenchStoreProvider store={store}>
        <WorkflowInspector
          capabilities={["workflow.read", "workflow.edit"]}
          catalog={[]}
          sourcePorts={[CONTROL_OUTPUT]}
        />
      </WorkflowWorkbenchStoreProvider>,
    );

    expect(html).toContain("不支持的节点类型");
    expect(html).toContain("只读");
    expect(html).not.toContain('data-slot="workflow-next-step-candidate"');
  });

  test("renders Last Run previews as escaped, bounded plain text", () => {
    const snapshot = createSnapshot();
    snapshot.editorSession.inspector.tab = "last_run";
    const { store } = createStore(snapshot);
    const lastRun: WorkflowNodeLastRunV1 = {
      run_id: RUN_ID,
      node_id: NODE_ID,
      activation_id: "activation-1",
      iteration_path: [],
      attempt: 1,
      status: "succeeded",
      output_preview: {
        format: "text",
        text: '<img src=x onerror="steal()">',
        truncated: true,
        redacted: true,
        original_byte_count: 4096,
      },
      retry_count: 0,
      truncated: true,
    };

    const html = renderToStaticMarkup(
      <WorkflowWorkbenchStoreProvider store={store}>
        <WorkflowInspector
          capabilities={["workflow.read"]}
          catalog={[catalogEntry("llm")]}
          lastRun={lastRun}
          sourcePorts={[CONTROL_OUTPUT]}
        />
      </WorkflowWorkbenchStoreProvider>,
    );

    expect(html).toContain("运行成功");
    expect(html).toContain("已脱敏");
    expect(html).toContain("已截断");
    expect(html).toContain("&lt;img src=x onerror=&quot;steal()&quot;&gt;");
    expect(html).not.toContain('<img src="x"');
    expect(html).not.toContain("dangerouslySetInnerHTML");
  });

  test("filters next-step candidates by edit capability, availability, and port", () => {
    const document = createSnapshot().current;
    const catalog = [
      catalogEntry("llm"),
      catalogEntry("http_request"),
      catalogEntry("python_code", false),
      catalogEntry("start"),
    ];

    const candidates = filterWorkflowNextStepCandidates({
      capabilities: ["workflow.read", "workflow.edit"],
      catalog,
      document,
      sourceNodeId: NODE_ID,
      sourceNodeType: "condition",
      sourcePort: CONTROL_OUTPUT,
    });

    expect(candidates.map((candidate) => candidate.nodeType)).toEqual(["llm"]);
    expect(
      filterWorkflowNextStepCandidates({
        capabilities: ["workflow.read", "workflow.edit"],
        catalog,
        document,
        locale: "en-US",
        sourceNodeId: NODE_ID,
        sourceNodeType: "condition",
        sourcePort: CONTROL_OUTPUT,
      })[0]?.title,
    ).toBe("LLM");
    expect(
      filterWorkflowNextStepCandidates({
        capabilities: ["workflow.read"],
        catalog,
        document,
        sourceNodeId: NODE_ID,
        sourceNodeType: "condition",
        sourcePort: CONTROL_OUTPUT,
      }),
    ).toEqual([]);
    expect(
      filterWorkflowNextStepCandidates({
        capabilities: ["workflow.read", "workflow.edit"],
        catalog,
        document,
        sourceNodeId: NODE_ID,
        sourceNodeType: "end",
        sourcePort: CONTROL_OUTPUT,
      }),
    ).toEqual([]);
  });

  test("adds a next step through exactly one atomic domain command", () => {
    const { commands, dispatch, store } = createStore();
    const document = createSnapshot().current;
    const candidate = filterWorkflowNextStepCandidates({
      capabilities: ["workflow.read", "workflow.edit"],
      catalog: [catalogEntry("llm")],
      document,
      sourceNodeId: NODE_ID,
      sourceNodeType: "start",
      sourcePort: CONTROL_OUTPUT,
    })[0];
    if (!candidate) throw new Error("expected one next-step candidate");

    commitWorkflowNextStep(store, {
      type: "add_next_step",
      source: { node_id: NODE_ID, port_id: "next" },
      node: {
        id: "33333333-3333-4333-8333-333333333333",
        type: candidate.nodeType,
        type_version: 1,
        scope: { kind: "root" },
        config: {},
      },
      layout: {
        node_id: "33333333-3333-4333-8333-333333333333",
        position: { x: 320, y: 160 },
      },
      transition: {
        id: "edge-next-step",
        target_port_id: candidate.targetPortId,
        routing: "smoothstep",
      },
    });

    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(commands).toEqual([
      {
        type: "add_next_step",
        source: { node_id: NODE_ID, port_id: "next" },
        node: {
          id: "33333333-3333-4333-8333-333333333333",
          type: "llm",
          type_version: 1,
          scope: { kind: "root" },
          config: {},
        },
        layout: {
          node_id: "33333333-3333-4333-8333-333333333333",
          position: { x: 320, y: 160 },
        },
        transition: {
          id: "edge-next-step",
          target_port_id: "in",
          routing: "smoothstep",
        },
      },
    ]);
  });

  test("hides next-step candidates when a one-cardinality source is full", () => {
    const document = createSnapshot().current;
    document.spec.transitions = [
      {
        id: "edge-existing",
        source: { node_id: NODE_ID, port_id: "next" },
        target: {
          node_id: "33333333-3333-4333-8333-333333333333",
          port_id: "in",
        },
      },
    ];

    expect(
      filterWorkflowNextStepCandidates({
        capabilities: ["workflow.read", "workflow.edit"],
        catalog: [catalogEntry("llm")],
        document,
        sourceNodeId: NODE_ID,
        sourceNodeType: "start",
        sourcePort: CONTROL_OUTPUT,
      }),
    ).toEqual([]);
  });

  test("keeps a failed atomic next-step command observable", () => {
    const { store } = createStore(createSnapshot(), {
      applied: false,
      issues: [
        {
          severity: "error",
          code: "WORKFLOW_PORT_CARDINALITY",
          message: "Source port is full",
          path: [],
          node_id: NODE_ID,
          port_id: "next",
        },
      ],
    });
    const result = commitWorkflowNextStep(store, {
      type: "add_next_step",
      source: { node_id: NODE_ID, port_id: "next" },
      node: {
        id: "33333333-3333-4333-8333-333333333333",
        type: "llm",
        type_version: 1,
        scope: { kind: "root" },
        config: {},
      },
      layout: {
        node_id: "33333333-3333-4333-8333-333333333333",
        position: { x: 320, y: 160 },
      },
      transition: {
        id: "edge-next-step",
        target_port_id: "in",
        routing: "smoothstep",
      },
    });

    expect(result.applied).toBe(false);
    expect(result.issues[0]?.code).toBe("WORKFLOW_PORT_CARDINALITY");
  });

  test("routes presentation edits through the closed editor command channel", () => {
    const { commands, store } = createStore();

    commitWorkflowNodePresentation(
      store,
      {
        id: NODE_ID,
        customLabel: "生成摘要",
        description: "把输入压缩为一段安全摘要",
      },
      "新版摘要",
    );

    expect(commands).toEqual([
      {
        type: "update_node_presentation",
        node_id: NODE_ID,
        custom_label: "新版摘要",
        description: "把输入压缩为一段安全摘要",
      },
    ]);
  });

  test("maps real undo and redo shortcuts but ignores editor targets", () => {
    const base = {
      altKey: false,
      ctrlKey: false,
      key: "z",
      metaKey: true,
      shiftKey: false,
    };
    expect(workflowHistoryShortcutAction(base)).toBe("undo");
    expect(workflowHistoryShortcutAction({ ...base, shiftKey: true })).toBe(
      "redo",
    );
    expect(
      workflowHistoryShortcutAction({ ...base, editingTarget: true }),
    ).toBeNull();
    expect(
      workflowHistoryShortcutAction({ ...base, isComposing: true }),
    ).toBeNull();
    expect(workflowHistoryShortcutAction({ ...base, altKey: true })).toBeNull();
  });

  test("focuses an issue through EditorSession and the Canvas target callback", () => {
    const { store } = createStore();
    const onFocus = rs.fn();
    const target = focusWorkflowInspectorIssue(
      store,
      {
        severity: "error",
        code: "WORKFLOW_PORT_INVALID",
        message: "端口无效",
        path: ["nodes", "0", "config"],
        node_id: NODE_ID,
        edge_id: "edge-invalid",
        port_id: "next",
      },
      onFocus,
    );

    expect(target).toEqual({
      kind: "port",
      node_id: NODE_ID,
      edge_id: "edge-invalid",
      port_id: "next",
    });
    expect(store.getState().editorSession.selection).toEqual({
      node_ids: [NODE_ID],
      edge_ids: ["edge-invalid"],
    });
    expect(onFocus).toHaveBeenCalledWith(target);
  });

  test("uses per-instance section error ids and restores Inspector scroll state", () => {
    const snapshot = createSnapshot();
    snapshot.editorSession.inspector.scroll_top = 321;
    const { store } = createStore(snapshot);
    const html = renderToStaticMarkup(
      <WorkflowWorkbenchStoreProvider store={store}>
        <WorkflowInspector
          capabilities={["workflow.read", "workflow.edit"]}
          catalog={[catalogEntry("llm")]}
          sourcePorts={[]}
        >
          <WorkflowInspectorSection id="inputs" title="输入 A" error="错误 A" />
          <WorkflowInspectorSection id="inputs" title="输入 B" error="错误 B" />
        </WorkflowInspector>
      </WorkflowWorkbenchStoreProvider>,
    );
    const describedBy = [...html.matchAll(/aria-describedby="([^"]+)"/gu)].map(
      (match) => match[1],
    );

    expect(describedBy).toHaveLength(2);
    expect(new Set(describedBy).size).toBe(2);
    expect(html).toContain('data-restored-scroll-top="321"');
  });

  test("distinguishes incomplete known nodes from unsupported future versions", () => {
    const missingVersion = createSnapshot();
    missingVersion.current = {
      spec: {
        schema_version: 1,
        nodes: [{ id: NODE_ID, type: "llm", config: {} }],
      },
      canvas: { schema_version: 1 },
    };
    const nullVersion = createSnapshot();
    nullVersion.current = {
      spec: {
        schema_version: 1,
        nodes: [{ id: NODE_ID, type: "llm", type_version: null, config: {} }],
      },
      canvas: { schema_version: 1 },
    };
    const futureVersion = createSnapshot();
    futureVersion.current = {
      spec: {
        schema_version: 1,
        nodes: [{ id: NODE_ID, type: "llm", type_version: 2, config: {} }],
      },
      canvas: { schema_version: 1 },
    };

    const renderInspector = (snapshot: WorkflowWorkbenchStoreSnapshot) =>
      renderToStaticMarkup(
        <WorkflowWorkbenchStoreProvider store={createStore(snapshot).store}>
          <WorkflowInspector
            capabilities={["workflow.read", "workflow.edit"]}
            catalog={[catalogEntry("llm")]}
            sourcePorts={[]}
          />
        </WorkflowWorkbenchStoreProvider>,
      );
    const incompleteHtml = renderInspector(missingVersion);
    const nullVersionHtml = renderInspector(nullVersion);
    const unsupportedHtml = renderInspector(futureVersion);

    expect(incompleteHtml).toContain("配置不完整");
    expect(incompleteHtml).not.toContain("版本暂不受支持");
    expect(incompleteHtml).toContain("只读");
    expect(incompleteHtml).not.toContain('aria-label="大模型配置"');
    expect(nullVersionHtml).toContain("配置不完整");
    expect(nullVersionHtml).not.toContain("版本暂不受支持");
    expect(nullVersionHtml).toContain("只读");
    expect(nullVersionHtml).not.toContain('aria-label="大模型配置"');
    expect(unsupportedHtml).toContain("版本暂不受支持");
    expect(unsupportedHtml).toContain("只读");
    expect(unsupportedHtml).not.toContain('aria-label="大模型配置"');
  });

  test("clamps Inspector width in EditorSession without dirtying persisted state", () => {
    const snapshot = createSnapshot();
    const current = snapshot.current;
    const baseline = snapshot.baseline;
    const { dispatch, setEditorSession, store } = createStore(snapshot);

    updateWorkflowInspectorWidth(store, 999);

    expect(setEditorSession).toHaveBeenCalledTimes(1);
    expect(store.getState().editorSession.inspector.width_px).toBe(600);
    expect(store.getState().current).toBe(current);
    expect(store.getState().baseline).toBe(baseline);
    expect(store.getState().dirty).toBe(false);
    expect(dispatch).not.toHaveBeenCalled();
  });
});

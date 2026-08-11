import { describe, expect, it, rs } from "@rstest/core";
import { ReactFlowProvider } from "@xyflow/react";
import { renderToStaticMarkup } from "react-dom/server";

import type { WorkflowFlowNode } from "@/components/projects/workflows/canvas/workflow-canvas-adapter";
import {
  BASIC_WORKFLOW_NODE_CONFIG_PANELS,
  EndNodeConfigPanel,
  LlmNodeConfigPanel,
  StartNodeConfigPanel,
  TransformNodeConfigPanel,
  buildLlmNodeConfigUpdate,
  buildTransformNodeConfigUpdate,
  buildWorkflowInputReplacement,
  buildWorkflowOutputReplacement,
} from "@/components/projects/workflows/node-config/basic";
import type {
  WorkflowModelCatalogProjection,
  WorkflowNodeConfigPanelProps,
} from "@/components/projects/workflows/node-config/contracts";
import {
  BASIC_WORKFLOW_NODE_CARDS,
  EndWorkflowNode,
  LlmWorkflowNode,
  StartWorkflowNode,
  TransformWorkflowNode,
} from "@/components/projects/workflows/nodes/basic-node-cards";
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
import type { JsonValue } from "@/core/project-workflows/types";

const NODE_ID = "10000000-0000-4000-8000-000000000001";
const SECOND_ID = "10000000-0000-4000-8000-000000000002";
const MODEL_CATALOG = {
  status: "ready",
  models: [
    {
      name: "primary-chat",
      display_name: "Primary Chat",
      supports_thinking: true,
      supports_reasoning_effort: true,
      workflow_authoring: {
        modes: ["chat", "completion"],
        supports_streaming: true,
        parameters: [
          {
            name: "temperature",
            kind: "number",
            minimum: -2,
            maximum: 2,
          },
          {
            name: "max_tokens",
            kind: "integer",
            minimum: 1,
            maximum: 2_000_000,
          },
        ],
      },
    },
    {
      name: "fast-chat",
      display_name: "Fast Chat",
      supports_thinking: false,
      supports_reasoning_effort: false,
      workflow_authoring: {
        modes: ["chat"],
        supports_streaming: false,
        parameters: [],
      },
    },
  ],
} as const satisfies WorkflowModelCatalogProjection;

function catalogEntry(
  type: "start" | "llm" | "transform" | "end",
): NodeCatalogEntry {
  const definition = workflowNodeRegistryV1.find(
    (candidate) => candidate.type === type,
  );
  if (!definition) throw new Error(`missing ${type} registry entry`);
  return { definition, availability: { state: "enabled" } };
}

function documentFor(
  type: "start" | "llm" | "transform" | "end",
  config: Record<string, JsonValue> = {},
): WorkflowPersistedDocumentV1 {
  return {
    spec: {
      schema_version: 1,
      nodes: [
        {
          id: NODE_ID,
          type,
          type_version: 1,
          scope: { kind: "root" },
          custom_label: null,
          description: null,
          input_bindings: {},
          execution_policy: {
            retry: { mode: "none" },
            on_error: { mode: "fail_workflow" },
          },
          config,
        },
      ],
      workflow_inputs: [
        {
          id: "question",
          name: "question",
          label: "问题",
          description: "用户问题",
          value_type: {
            kind: "string",
            collection: false,
            nullable: false,
          },
          required: true,
          constraints: { kind: "none" },
        },
        {
          id: "language",
          name: "language",
          label: "语言",
          description: null,
          value_type: {
            kind: "string",
            collection: false,
            nullable: false,
          },
          required: false,
          default: "zh-CN",
          constraints: { kind: "enum", options: ["zh-CN", "en-US"] },
        },
      ],
      workflow_outputs: [
        {
          id: "answer",
          name: "answer",
          description: "最终答案",
          value_type: {
            kind: "string",
            collection: false,
            nullable: false,
          },
          source: {
            kind: "node_output",
            node_id: SECOND_ID,
            output_id: "text",
          },
        },
      ],
    },
    canvas: { schema_version: 1 },
  };
}

function storeFor(
  document: WorkflowPersistedDocumentV1,
): WorkflowWorkbenchStorePort {
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
  return {
    dispatch: rs.fn(() => ({ applied: true, issues: [] })),
    getState: () => snapshot,
    redo: rs.fn(() => false),
    setEditorSession: rs.fn(() => true),
    subscribe: () => () => undefined,
    undo: rs.fn(() => false),
  };
}

function renderPanel(
  Panel: (props: WorkflowNodeConfigPanelProps) => React.ReactNode,
  type: "start" | "llm" | "transform" | "end",
  config: Record<string, JsonValue>,
  locked: {
    readOnly?: boolean;
    disabled?: boolean;
    editable?: boolean;
    catalogDisabled?: boolean;
    modelCatalog?: WorkflowModelCatalogProjection;
  } = {},
): string {
  const document = documentFor(type, config);
  const node = document.spec.nodes?.[0];
  if (!node) throw new Error("node fixture missing");
  return renderToStaticMarkup(
    <WorkflowWorkbenchStoreProvider store={storeFor(document)}>
      <Panel
        capabilities={
          locked.editable === false
            ? ["workflow.read"]
            : ["workflow.read", "workflow.edit"]
        }
        catalogEntry={
          locked.catalogDisabled
            ? {
                ...catalogEntry(type),
                availability: {
                  state: "disabled",
                  reason_code: "WORKFLOW_DISABLED",
                },
              }
            : catalogEntry(type)
        }
        disabled={locked.disabled ?? false}
        document={document}
        locale="zh-CN"
        modelCatalog={locked.modelCatalog}
        node={node}
        nodeId={NODE_ID}
        readOnly={locked.readOnly ?? false}
      />
    </WorkflowWorkbenchStoreProvider>,
  );
}

describe("G20-A basic Workflow node configuration", () => {
  it("freezes the four specialized panel/card registries", () => {
    expect(BASIC_WORKFLOW_NODE_CONFIG_PANELS).toEqual({
      start: StartNodeConfigPanel,
      llm: LlmNodeConfigPanel,
      transform: TransformNodeConfigPanel,
      end: EndNodeConfigPanel,
    });
    expect(BASIC_WORKFLOW_NODE_CARDS).toEqual({
      start: StartWorkflowNode,
      llm: LlmWorkflowNode,
      transform: TransformWorkflowNode,
      end: EndWorkflowNode,
    });
  });

  it("edits Start declarations without changing stable IDs or order", () => {
    const document = documentFor("start");
    const command = buildWorkflowInputReplacement(document, 0, {
      name: "prompt",
      label: "提示词",
    });

    expect(command.type).toBe("replace_workflow_inputs");
    expect(command.workflow_inputs.map((item) => item.id)).toEqual([
      "question",
      "language",
    ]);
    expect(command.workflow_inputs[0]).toMatchObject({
      id: "question",
      name: "prompt",
      label: "提示词",
      required: true,
      constraints: { kind: "none" },
    });
  });

  it("edits End declarations without changing stable IDs, order, or source", () => {
    const document = documentFor("end");
    const command = buildWorkflowOutputReplacement(document, 0, {
      name: "final_answer",
    });

    expect(command.type).toBe("replace_workflow_outputs");
    expect(command.workflow_outputs[0]).toMatchObject({
      id: "answer",
      name: "final_answer",
      source: {
        kind: "node_output",
        node_id: SECOND_ID,
        output_id: "text",
      },
    });
  });

  it("merges only authored LLM/Transform config and keeps stable row IDs", () => {
    const llmNode = documentFor("llm", {
      model_ref: "primary-model",
      mode: "chat",
      runtime_profile: "must-be-dropped",
      messages: [
        {
          id: "message-1",
          role: "user",
          content: { version: 1, segments: [] },
        },
      ],
    }).spec.nodes![0]!;
    const llm = buildLlmNodeConfigUpdate(llmNode, {
      mode: "completion",
    });
    expect(llm).toMatchObject({
      type: "update_node_config",
      node_id: NODE_ID,
      config: {
        model_ref: "primary-model",
        mode: "completion",
        messages: [{ id: "message-1" }],
      },
    });
    expect(llm.config).not.toHaveProperty("runtime_profile");

    const transformNode = documentFor("transform", {
      mode: "text",
      source: "must-be-dropped",
      input_variables: [
        {
          id: "variable-1",
          name: "question",
          value_type: {
            kind: "string",
            collection: false,
            nullable: false,
          },
        },
      ],
    }).spec.nodes![0]!;
    const transform = buildTransformNodeConfigUpdate(transformNode, {
      missing_variable: "null",
    });
    expect(transform).toMatchObject({
      type: "update_node_config",
      config: {
        input_variables: [{ id: "variable-1", name: "question" }],
        missing_variable: "null",
      },
    });
    expect(transform.config).not.toHaveProperty("source");
  });

  it("renders partial Drafts with inline issues instead of throwing", () => {
    const start = renderPanel(StartNodeConfigPanel, "start", {});
    const llm = renderPanel(LlmNodeConfigPanel, "llm", { mode: "chat" });
    const transform = renderPanel(TransformNodeConfigPanel, "transform", {
      mode: "json",
    });
    const end = renderPanel(EndNodeConfigPanel, "end", {});

    expect(start).toContain("工作流输入");
    expect(start).toContain("question");
    expect(llm).toContain("模型引用未绑定");
    expect(llm).toContain("受限消息模板");
    expect(transform).toContain("JSON 输出 Schema 未配置");
    expect(transform).toContain("输入变量与绑定");
    expect(end).toContain("工作流输出");
    expect(end).toContain("最终答案");
  });

  it("selects logical Model refs only from the safe authenticated catalog", () => {
    const modelCatalog = MODEL_CATALOG;
    const html = renderPanel(
      LlmNodeConfigPanel,
      "llm",
      {
        model_ref: "primary-chat",
        mode: "chat",
        context_input_ids: [],
        messages: [
          {
            id: "message-1",
            role: "user",
            content: { version: 1, segments: [] },
          },
        ],
        model_parameters: {},
        stream: false,
        reasoning_output: "omit",
        structured_output: { enabled: false, schema: null },
      },
      { modelCatalog },
    );

    expect(html).toMatch(/<select[^>]*aria-label="逻辑模型引用"/);
    expect(html).toContain('value="primary-chat" selected');
    expect(html).toContain("Primary Chat");
    expect(html).toContain("Fast Chat");
    expect(html).not.toMatch(/<input[^>]*aria-label="逻辑模型引用"/);

    const unavailable = renderPanel(
      LlmNodeConfigPanel,
      "llm",
      { model_ref: "legacy-model" },
      {
        modelCatalog: { status: "unavailable", models: [] },
      },
    );
    expect(unavailable).toMatch(
      /<select[^>]*aria-label="逻辑模型引用"[^>]*disabled/,
    );
    expect(unavailable).toContain("模型目录当前不可用");
    expect(unavailable).toContain("legacy-model（当前不可用）");

    const unsupportedReasoning = renderPanel(
      LlmNodeConfigPanel,
      "llm",
      {
        model_ref: "fast-chat",
        mode: "chat",
        context_input_ids: [],
        messages: [
          {
            id: "message-1",
            role: "user",
            content: { version: 1, segments: [] },
          },
        ],
        model_parameters: {},
        stream: false,
        reasoning_output: "provider_summary",
        structured_output: { enabled: false, schema: null },
      },
      { modelCatalog },
    );
    expect(unsupportedReasoning).toContain("未声明推理能力");
    expect(unsupportedReasoning).toMatch(
      /<option[^>]*disabled[^>]*value="provider_summary"/,
    );

    const unsupportedMode = renderPanel(
      LlmNodeConfigPanel,
      "llm",
      {
        model_ref: "fast-chat",
        mode: "completion",
        context_input_ids: [],
        messages: [
          {
            id: "message-1",
            role: "user",
            content: { version: 1, segments: [] },
          },
        ],
        model_parameters: { temperature: 0.2 },
        stream: true,
        reasoning_output: "omit",
        structured_output: { enabled: false, schema: null },
      },
      { modelCatalog },
    );
    expect(unsupportedMode).toContain("不支持当前调用模式");
    expect(unsupportedMode).toContain("completion（当前模型不支持）");
    expect(unsupportedMode).toContain(
      '<input disabled="" type="checkbox" checked=""/>',
    );
    expect(unsupportedMode).not.toContain('aria-label="temperature"');

    const invalidParameter = renderPanel(
      LlmNodeConfigPanel,
      "llm",
      {
        model_ref: "primary-chat",
        mode: "chat",
        context_input_ids: [],
        messages: [
          {
            id: "message-1",
            role: "user",
            content: { version: 1, segments: [] },
          },
        ],
        model_parameters: { max_tokens: 1.5 },
        stream: false,
        reasoning_output: "omit",
        structured_output: { enabled: false, schema: null },
      },
      { modelCatalog },
    );
    expect(invalidParameter).toContain("超出服务端声明的类型或范围");
  });

  it.each([
    ["start", StartNodeConfigPanel],
    ["llm", LlmNodeConfigPanel],
    ["transform", TransformNodeConfigPanel],
    ["end", EndNodeConfigPanel],
  ] as const)(
    "disables every authored control for readOnly and disabled %s panels",
    (type, Panel) => {
      for (const locked of [
        { readOnly: true },
        { disabled: true },
        { editable: false },
        { catalogDisabled: true },
      ]) {
        const html = renderPanel(Panel, type, {}, locked);
        expect(html).toContain("<fieldset");
        expect(html).toMatch(/<fieldset[^>]*disabled/);
        expect(html).toContain('aria-disabled="true"');
      }
    },
  );

  it("shows only the approved LLM and Transform surface", () => {
    const llm = renderPanel(
      LlmNodeConfigPanel,
      "llm",
      {
        model_ref: "primary-chat",
        mode: "chat",
        model_parameters: { temperature: 0.2, top_p: 0.9 },
        source: "must-not-render",
        credential_id: "must-not-render",
        runtime_profile: "must-not-render",
        tools: ["must-not-render"],
      },
      { modelCatalog: MODEL_CATALOG },
    );
    expect(llm).toContain("temperature");
    expect(llm).toContain("max_tokens");
    expect(llm).not.toContain('aria-label="top_p"');
    expect(llm).toContain("服务端未声明的字段");
    expect(llm).toContain("固定无工具调用");
    expect(llm).not.toContain("must-not-render");
    expect(llm).not.toContain("credential_id");
    expect(llm).not.toContain("runtime_profile");

    const transform = renderPanel(TransformNodeConfigPanel, "transform", {
      mode: "text",
      template: { version: 1, segments: [] },
      source: "must-not-render",
      runtime: "must-not-render",
    });
    expect(transform).toContain("受限模板");
    expect(transform).not.toContain("must-not-render");
  });

  it("renders dedicated accessible cards without authored config or private data", () => {
    const data: WorkflowFlowNode["data"] = {
      nodeId: NODE_ID,
      nodeKind: "llm",
      originalType: "llm",
      title: "大模型",
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
      credential_id: "must-not-render",
    };
    const html = renderToStaticMarkup(
      <ReactFlowProvider>
        <LlmWorkflowNode
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
          type="llm"
          zIndex={0}
        />
      </ReactFlowProvider>,
    );
    expect(html).toContain('data-basic-node-card="llm"');
    expect(html).toContain("模型调用 · 固定无工具");
    expect(html).toContain('aria-label="工作流节点：大模型"');
    expect(html).not.toContain("must-not-render");
  });
});

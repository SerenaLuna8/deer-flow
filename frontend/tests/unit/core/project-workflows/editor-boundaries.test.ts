import { readFileSync } from "node:fs";

import { describe, expect, it } from "@rstest/core";

import {
  projectWorkflowPersistedDocumentV1,
  workflowWorkbenchLayersV1Schema,
} from "@/core/project-workflows/boundaries";
import {
  workflowEditorSessionV1Schema,
  workflowRuntimeProjectionV1Schema,
} from "@/core/project-workflows/editor-contracts";
import {
  canvasDocumentV1Schema,
  workflowNodeKindSchema,
  workflowSpecV1Schema,
} from "@/core/project-workflows/types";
import { WORKFLOW_CAPABILITIES } from "@/core/projects/types";

import canvasFixture from "../../../fixtures/workflows/canvas-document-v1.json";
import specFixture from "../../../fixtures/workflows/workflow-spec-v1.json";

const NODE_ID = "00000000-0000-4000-8000-000000000001";
const RUN_ID = "10000000-0000-4000-8000-000000000001";
const VERSION_ID = "20000000-0000-4000-8000-000000000001";
const WORKFLOW_ID = "30000000-0000-4000-8000-000000000001";
const PROJECT_ID = "40000000-0000-4000-8000-000000000001";
const ACCOUNT_ID = "50000000-0000-4000-8000-000000000001";

const editorSession = () => ({
  schema_version: 1,
  viewport: { x: 10, y: 20, zoom: 1 },
  selection: { node_ids: [NODE_ID], edge_ids: ["transition-1"] },
  inspector: {
    open: true,
    node_id: NODE_ID,
    tab: "settings",
    width_px: 480,
    expanded_section_ids: ["inputs"],
    scroll_top: 0,
  },
  palette: {
    open: false,
    anchor: null,
  },
  interaction: {
    kind: "node_drag",
    node_ids: [NODE_ID],
    transient_positions: { [NODE_ID]: { x: 30, y: 40 } },
  },
});

const runtimeProjection = () => ({
  schema_version: 1,
  scope: {
    account_id: ACCOUNT_ID,
    project_id: PROJECT_ID,
    workflow_id: WORKFLOW_ID,
    run_id: RUN_ID,
    workflow_version_id: VERSION_ID,
  },
  cursor: "42",
  run_status: "running",
  progress: { completed_nodes: 1, active_nodes: 1, total_nodes: 3 },
  node_attempts: [
    {
      node_id: NODE_ID,
      node_type: "start",
      activation_id: "activation-1",
      iteration_path: [],
      attempt: 1,
      status: "succeeded",
      output_preview: null,
      error: null,
    },
  ],
  output_preview: null,
  error: null,
  wait: null,
});

describe("Workflow editor four-layer boundaries", () => {
  it("keeps the first-batch node and Project capability enums exact and future-closed", () => {
    const expectedNodeTypes = [
      "start",
      "llm",
      "condition",
      "transform",
      "variable_aggregate",
      "loop",
      "http_request",
      "python_code",
      "end",
    ] as const;
    expect(workflowNodeKindSchema.options).toEqual(expectedNodeTypes);
    for (const nodeType of expectedNodeTypes) {
      expect(workflowNodeKindSchema.parse(nodeType)).toBe(nodeType);
    }
    expect(() => workflowNodeKindSchema.parse("agent")).toThrow();
    expect(() => workflowNodeKindSchema.parse("human_input")).toThrow();
    expect(() => workflowNodeKindSchema.parse("tool")).toThrow();
    expect(WORKFLOW_CAPABILITIES).toEqual([
      "workflow.read",
      "workflow.edit",
      "workflow.publish",
      "workflow.execute",
      "workflow.code.use",
      "workflow.http.use",
      "workflow.http.write",
      "workflow.credential.grant",
      "workflow.run.read_own",
      "workflow.run.cancel_own",
    ]);
  });

  it("keeps authored Spec and persisted Canvas strict and free of transient/runtime state", () => {
    expect(workflowSpecV1Schema.parse(specFixture)).toEqual(specFixture);
    expect(canvasDocumentV1Schema.parse(canvasFixture)).toEqual(canvasFixture);
    for (const forbidden of [
      "viewport",
      "selection",
      "dragging",
      "measured",
      "runtime",
    ]) {
      expect(() =>
        workflowSpecV1Schema.parse({ ...specFixture, [forbidden]: {} }),
      ).toThrow();
      expect(() =>
        canvasDocumentV1Schema.parse({ ...canvasFixture, [forbidden]: {} }),
      ).toThrow();
    }
  });

  it("accepts only transient Editor Session fields and rejects authored or runtime pollution", () => {
    expect(workflowEditorSessionV1Schema.parse(editorSession())).toEqual(
      editorSession(),
    );
    expect(() =>
      workflowEditorSessionV1Schema.parse({
        ...editorSession(),
        spec: specFixture,
      }),
    ).toThrow();
    expect(() =>
      workflowEditorSessionV1Schema.parse({
        ...editorSession(),
        run_status: "running",
      }),
    ).toThrow();

    const originalDrag = editorSession();
    const invalidDrag = {
      ...originalDrag,
      interaction: {
        ...originalDrag.interaction,
        transient_positions: {},
      },
    };
    expect(() => workflowEditorSessionV1Schema.parse(invalidDrag)).toThrow();
  });

  it("accepts only scope-bound safe Runtime Projection fields and rejects authored/private data", () => {
    expect(
      workflowRuntimeProjectionV1Schema.parse(runtimeProjection()),
    ).toEqual(runtimeProjection());
    for (const [field, value] of [
      ["spec", specFixture],
      ["canvas", canvasFixture],
      ["source", "print('secret')"],
      ["inputs", { raw: true }],
      ["origin_trace_id", "private-trace"],
    ] as const) {
      expect(() =>
        workflowRuntimeProjectionV1Schema.parse({
          ...runtimeProjection(),
          [field]: value,
        }),
      ).toThrow();
    }
    expect(() =>
      workflowRuntimeProjectionV1Schema.parse({
        ...runtimeProjection(),
        cursor: 42,
      }),
    ).toThrow();

    const retried = runtimeProjection();
    retried.node_attempts.push({
      ...retried.node_attempts[0]!,
      attempt: 2,
    });
    expect(
      workflowRuntimeProjectionV1Schema
        .parse(retried)
        .node_attempts.map(({ attempt }) => attempt),
    ).toEqual([1, 2]);
    const duplicate = runtimeProjection();
    duplicate.node_attempts.push(structuredClone(duplicate.node_attempts[0]!));
    expect(() => workflowRuntimeProjectionV1Schema.parse(duplicate)).toThrow();
    expect(() =>
      workflowRuntimeProjectionV1Schema.parse({
        ...runtimeProjection(),
        wait: { request_id: "future" },
      }),
    ).toThrow();
  });

  it("projects persistence as exact Spec plus Canvas and never carries session/runtime", () => {
    const layers = workflowWorkbenchLayersV1Schema.parse({
      authored: specFixture,
      persisted_layout: canvasFixture,
      transient: editorSession(),
      runtime: runtimeProjection(),
    });
    const persisted = projectWorkflowPersistedDocumentV1(layers);
    expect(persisted).toEqual({ spec: specFixture, canvas: canvasFixture });
    expect(Object.keys(persisted)).toEqual(["spec", "canvas"]);
    expect("transient" in persisted).toBe(false);
    expect("runtime" in persisted).toBe(false);
    expect(() =>
      workflowWorkbenchLayersV1Schema.parse({
        authored: { ...specFixture, runtime: runtimeProjection() },
        persisted_layout: canvasFixture,
        transient: editorSession(),
        runtime: runtimeProjection(),
      }),
    ).toThrow();
  });

  it("persists a structurally valid partial Draft before published semantics are complete", () => {
    const partialDraftSpec = {
      schema_version: 1,
      nodes: [{ type: "llm", config: {} }],
    };
    const partialDraftCanvas = { schema_version: 1 };
    const layers = workflowWorkbenchLayersV1Schema.parse({
      authored: partialDraftSpec,
      persisted_layout: partialDraftCanvas,
      transient: editorSession(),
      runtime: null,
    });

    expect(projectWorkflowPersistedDocumentV1(layers)).toEqual({
      spec: partialDraftSpec,
      canvas: partialDraftCanvas,
    });
    expect(workflowSpecV1Schema.safeParse(partialDraftSpec).success).toBe(
      false,
    );
    expect(canvasDocumentV1Schema.safeParse(partialDraftCanvas).success).toBe(
      false,
    );
  });

  it("keeps the pure boundary modules static-safe", () => {
    const source = ["editor-contracts.ts", "boundaries.ts"]
      .map((file) => readFileSync(`src/core/project-workflows/${file}`, "utf8"))
      .join("\n");
    for (const forbidden of [
      'from "react"',
      'from "next/',
      "@tanstack/react-query",
      "/api/",
      "fetch(",
      "localStorage",
      "sessionStorage",
      "window.",
      "@xyflow/react",
    ]) {
      expect(source).not.toContain(forbidden);
    }
  });

  it("pins the existing React Flow and Python editor libraries as direct dependencies without a store", () => {
    const packageJson = JSON.parse(readFileSync("package.json", "utf8")) as {
      dependencies: Record<string, string>;
      devDependencies: Record<string, string>;
    };
    expect(packageJson.dependencies["@xyflow/react"]).toBe("^12.10.0");
    expect(packageJson.dependencies["@uiw/react-codemirror"]).toBe("^4.25.4");
    expect(packageJson.dependencies["@codemirror/lang-python"]).toBe("^6.2.1");
    expect(packageJson.dependencies.zustand).toBeUndefined();
    expect(packageJson.devDependencies.zustand).toBeUndefined();
  });
});

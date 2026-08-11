import { describe, expect, it, rs } from "@rstest/core";
import { ReactFlowProvider } from "@xyflow/react";
import { renderToStaticMarkup } from "react-dom/server";

import type { WorkflowFlowNode } from "@/components/projects/workflows/canvas/workflow-canvas-adapter";
import {
  ConditionNodeConfigPanel,
  VariableAggregateNodeConfigPanel,
  appendAggregateCandidate,
  appendAggregateGroup,
  appendConditionBranch,
  moveAggregateCandidate,
  moveConditionBranch,
  removeConditionBranch,
} from "@/components/projects/workflows/node-config/branching";
import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import {
  LOOP_NATIVE_MAX_ITERATIONS,
  LoopNodeConfigPanel,
  appendLoopVariable,
  buildAddLoopBodyEntryCommand,
  buildLoopBindingUpdate,
  buildReparentLoopChildCommand,
  buildSetLoopBodyExitCommand,
} from "@/components/projects/workflows/node-config/loop";
import {
  ConditionWorkflowNode,
  LoopWorkflowNode,
  VariableAggregateWorkflowNode,
} from "@/components/projects/workflows/nodes/branch-loop-node-cards";
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
import type {
  ConditionNodeConfigV1,
  LoopNodeConfigV1,
  VariableAggregateNodeConfigV1,
} from "@/core/project-workflows/types";

const NODE_ID = "20000000-0000-4000-8000-000000000001";
const BODY_ID = "20000000-0000-4000-8000-000000000002";

const TRUE_PREDICATE = {
  op: "and" as const,
  items: [
    {
      left: { kind: "literal" as const, value: true },
      operator: "eq" as const,
      right: { kind: "literal" as const, value: true },
    },
  ],
};

function catalogEntry(
  type: "condition" | "variable_aggregate" | "loop",
): NodeCatalogEntry {
  const definition = workflowNodeRegistryV1.find(
    (candidate) => candidate.type === type,
  );
  if (!definition) throw new Error(`missing ${type} registry entry`);
  return {
    definition,
    availability: { state: "enabled" },
    public_limits:
      type === "variable_aggregate"
        ? { max_aggregate_groups: 2, max_aggregate_candidates: 2 }
        : type === "loop"
          ? { max_iterations: 8 }
          : undefined,
  };
}

function documentFor(
  type: "condition" | "variable_aggregate" | "loop",
  config: NonNullable<WorkflowNodeConfigPanelProps["node"]["config"]>,
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
          input_bindings: {
            left: { kind: "literal", value: "left" },
            right: { kind: "literal", value: "right" },
          },
          execution_policy: {
            retry: { mode: "none" },
            on_error: { mode: "fail_workflow" },
          },
          config,
        },
        {
          id: BODY_ID,
          type: "transform",
          type_version: 1,
          scope:
            type === "loop"
              ? { kind: "loop_body", loop_node_id: NODE_ID }
              : { kind: "root" },
          config: {},
        },
      ],
    },
    canvas: {
      schema_version: 1,
      node_layouts: [
        { node_id: NODE_ID, position: { x: 0, y: 0 } },
        {
          node_id: BODY_ID,
          ...(type === "loop" ? { parent_node_id: NODE_ID } : {}),
          position: { x: 24, y: 80 },
        },
      ],
    },
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
  type: "condition" | "variable_aggregate" | "loop",
  config: NonNullable<WorkflowNodeConfigPanelProps["node"]["config"]>,
  locked: { readOnly?: boolean; disabled?: boolean } = {},
): string {
  const document = documentFor(type, config);
  const node = document.spec.nodes?.[0];
  if (!node) throw new Error("node fixture missing");
  return renderToStaticMarkup(
    <WorkflowWorkbenchStoreProvider store={storeFor(document)}>
      <Panel
        capabilities={["workflow.read", "workflow.edit"]}
        catalogEntry={catalogEntry(type)}
        disabled={locked.disabled ?? false}
        document={document}
        locale="zh-CN"
        node={node}
        nodeId={NODE_ID}
        readOnly={locked.readOnly ?? false}
      />
    </WorkflowWorkbenchStoreProvider>,
  );
}

const CONDITION: ConditionNodeConfigV1 = {
  branches: [
    {
      id: "if-one",
      output_port_id: "if_one",
      label: "IF",
      predicate: TRUE_PREDICATE,
    },
  ],
  else_output_port_id: "fallback",
};

const AGGREGATE: VariableAggregateNodeConfigV1 = {
  strategy: "exclusive_branch",
  groups: [
    {
      id: "summary",
      name: "摘要",
      value_type: { kind: "string", collection: false, nullable: false },
      candidate_input_ids: ["left"],
    },
  ],
};

const LOOP: LoopNodeConfigV1 = {
  mode: "do_until",
  body_entry_node_id: BODY_ID,
  body_exit_node_id: BODY_ID,
  max_iterations: 3,
  termination_condition: TRUE_PREDICATE,
  variables: [
    {
      id: "count",
      name: "count",
      value_type: { kind: "number", collection: false, nullable: false },
      initial_input_id: "initial_count",
      next_input_id: "next_count",
      output_port_id: "count_value",
    },
  ],
};

describe("G20-B/C branch, aggregate, and Loop authoring", () => {
  it("keeps ordered Condition branch and port identities stable while ELSE is undeletable", () => {
    const added = appendConditionBranch(CONDITION, {
      branchId: "elif-two",
      outputPortId: "elif_two",
    });
    expect(
      added.branches.map(({ id, output_port_id }) => [id, output_port_id]),
    ).toEqual([
      ["if-one", "if_one"],
      ["elif-two", "elif_two"],
    ]);
    expect(added.else_output_port_id).toBe("fallback");

    const moved = moveConditionBranch(added, 1, 0);
    expect(moved.branches.map((branch) => branch.id)).toEqual([
      "elif-two",
      "if-one",
    ]);
    expect(removeConditionBranch(CONDITION, 0)).toBe(CONDITION);
  });

  it("enforces ordered Aggregate group/candidate Catalog limits", () => {
    const second = {
      id: "details",
      name: "详情",
      value_type: {
        kind: "string" as const,
        collection: false,
        nullable: true,
      },
      candidate_input_ids: ["right"],
    };
    const withSecond = appendAggregateGroup(AGGREGATE, second, 2);
    expect(withSecond.groups.map((group) => group.id)).toEqual([
      "summary",
      "details",
    ]);
    expect(appendAggregateGroup(withSecond, second, 2)).toBe(withSecond);

    const withCandidate = appendAggregateCandidate(AGGREGATE, 0, "right", 2);
    expect(withCandidate.groups[0]?.candidate_input_ids).toEqual([
      "left",
      "right",
    ]);
    expect(appendAggregateCandidate(withCandidate, 0, "third", 2)).toBe(
      withCandidate,
    );
    expect(
      moveAggregateCandidate(withCandidate, 0, 1, 0).groups[0]
        ?.candidate_input_ids,
    ).toEqual(["right", "left"]);
  });

  it("builds only compound/reparent Loop commands and typed binding updates", () => {
    const entry = buildAddLoopBodyEntryCommand({
      loopNodeId: NODE_ID,
      nodeId: BODY_ID,
      nodeType: "transform",
      position: { x: 24, y: 80 },
      setAsExit: true,
    });
    expect(entry).toMatchObject({
      type: "add_loop_body_entry",
      loop_node_id: NODE_ID,
      node: { id: BODY_ID, type: "transform" },
      layout: { node_id: BODY_ID },
      set_as_exit: true,
    });
    expect(entry).not.toHaveProperty("transition");
    expect(buildSetLoopBodyExitCommand(NODE_ID, BODY_ID)).toEqual({
      type: "set_loop_body_exit",
      loop_node_id: NODE_ID,
      node_id: BODY_ID,
    });
    expect(buildReparentLoopChildCommand(BODY_ID, NODE_ID)).toEqual({
      type: "reparent_node",
      node_id: BODY_ID,
      parent_node_id: NODE_ID,
    });
    expect(
      buildLoopBindingUpdate(
        documentFor("loop", LOOP).spec.nodes![0]!,
        "initial_count",
        { kind: "literal", value: 0 },
      ),
    ).toMatchObject({
      type: "update_node_input_bindings",
      input_bindings: {
        left: { kind: "literal", value: "left" },
        initial_count: { kind: "literal", value: 0 },
      },
    });
  });

  it("keeps at least one Loop variable and bounds max iterations by native policy", () => {
    const added = appendLoopVariable(LOOP, {
      id: "draft",
      name: "draft",
      initialInputId: "initial_draft",
      nextInputId: "next_draft",
      outputPortId: "draft_value",
    });
    expect(added.variables.map((variable) => variable.id)).toEqual([
      "count",
      "draft",
    ]);
    expect(LOOP_NATIVE_MAX_ITERATIONS).toBe(1_000_000);
  });

  it("renders typed AST controls, partial Draft issues, limits, and MISSING semantics", () => {
    const condition = renderPanel(ConditionNodeConfigPanel, "condition", {});
    expect(condition).toContain("IF / ELIF");
    expect(condition).toContain("ELSE");
    expect(condition).toContain("typed Predicate AST");
    expect(condition).not.toContain("raw expression");

    const aggregate = renderPanel(
      VariableAggregateNodeConfigPanel,
      "variable_aggregate",
      AGGREGATE,
    );
    expect(aggregate).toContain("MISSING");
    expect(aggregate).toContain("JSON null");
    expect(aggregate).toContain("同型");
    expect(aggregate).toContain("最多 2 个分组");

    const loop = renderPanel(LoopNodeConfigPanel, "loop", {
      mode: "do_until",
    });
    expect(loop).toContain("至少需要一个循环变量");
    expect(loop).toContain("每轮变量原子更新后求值");
    expect(loop).toContain('max="8"');
    expect(loop).not.toContain("break");
    expect(loop).not.toContain("continue");
  });

  it.each([
    [ConditionNodeConfigPanel, "condition"],
    [VariableAggregateNodeConfigPanel, "variable_aggregate"],
    [LoopNodeConfigPanel, "loop"],
  ] as const)(
    "preserves values while disabling all authored controls in locked %s panels",
    (Panel, type) => {
      for (const locked of [{ readOnly: true }, { disabled: true }]) {
        const config =
          type === "condition"
            ? CONDITION
            : type === "variable_aggregate"
              ? AGGREGATE
              : LOOP;
        const html = renderPanel(Panel, type, config, locked);
        expect(html).toMatch(/<fieldset[^>]*disabled/);
        expect(html).toContain('aria-disabled="true"');
      }
    },
  );

  it.each([
    [ConditionWorkflowNode, "condition", "条件出口"],
    [VariableAggregateWorkflowNode, "variable_aggregate", "聚合输出"],
    [LoopWorkflowNode, "loop", "循环输出"],
  ] as const)(
    "renders dedicated %s cards from safe projection data only",
    (Card, kind, summary) => {
      const data: WorkflowFlowNode["data"] = {
        nodeId: NODE_ID,
        nodeKind: kind,
        originalType: kind,
        title: "安全标题",
        supportState: "supported",
        statusLabel: "状态：可用",
        availabilityReason: null,
        readOnly: false,
        disabled: false,
        inputPorts: [],
        outputPorts: [
          {
            id: "next",
            label: "下一步",
            kind: "control",
            cardinality: "one",
            direction: "output",
          },
        ],
        focusedPortId: null,
        portSignature: "safe-signature",
        config: "must-not-render",
        predicate: "must-not-render",
      };
      const html = renderToStaticMarkup(
        <ReactFlowProvider>
          <Card
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
            type={kind}
            zIndex={0}
          />
        </ReactFlowProvider>,
      );
      expect(html).toContain(`data-branch-loop-node-card="${kind}"`);
      expect(html).toContain(summary);
      expect(html).toContain('aria-label="工作流节点：安全标题"');
      expect(html).not.toContain("must-not-render");
    },
  );
});

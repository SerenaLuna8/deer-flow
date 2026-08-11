import { describe, expect, it } from "@rstest/core";

import { WORKFLOW_NODE_TYPES } from "@/components/projects/workflows/canvas/workflow-canvas";
import {
  EndNodeConfigPanel,
  LlmNodeConfigPanel,
  StartNodeConfigPanel,
  TransformNodeConfigPanel,
} from "@/components/projects/workflows/node-config/basic";
import {
  ConditionNodeConfigPanel,
  VariableAggregateNodeConfigPanel,
} from "@/components/projects/workflows/node-config/branching";
import { PythonCodeNodeConfigPanel } from "@/components/projects/workflows/node-config/code";
import { HttpRequestNodeConfigPanel } from "@/components/projects/workflows/node-config/http";
import { LoopNodeConfigPanel } from "@/components/projects/workflows/node-config/loop";
import { WORKFLOW_NODE_CONFIG_PANELS } from "@/components/projects/workflows/node-config/registry";
import {
  EndWorkflowNode,
  LlmWorkflowNode,
  StartWorkflowNode,
  TransformWorkflowNode,
} from "@/components/projects/workflows/nodes/basic-node-cards";
import {
  ConditionWorkflowNode,
  LoopWorkflowNode,
  VariableAggregateWorkflowNode,
} from "@/components/projects/workflows/nodes/branch-loop-node-cards";
import { HttpRequestWorkflowNode } from "@/components/projects/workflows/nodes/http-node-card";
import { PythonCodeWorkflowNode } from "@/components/projects/workflows/nodes/python-code-node-card";
import { WorkflowNodeCard } from "@/components/projects/workflows/nodes/workflow-node";
import { workflowNodeCatalogKinds } from "@/core/project-workflows/catalog";

describe("G20 closed node renderer and Inspector registry", () => {
  it("maps the exact nine first-batch kinds to specialized Inspector panels", () => {
    expect(Object.keys(WORKFLOW_NODE_CONFIG_PANELS)).toEqual(
      workflowNodeCatalogKinds,
    );
    expect(WORKFLOW_NODE_CONFIG_PANELS).toEqual({
      start: StartNodeConfigPanel,
      llm: LlmNodeConfigPanel,
      condition: ConditionNodeConfigPanel,
      transform: TransformNodeConfigPanel,
      variable_aggregate: VariableAggregateNodeConfigPanel,
      loop: LoopNodeConfigPanel,
      http_request: HttpRequestNodeConfigPanel,
      python_code: PythonCodeNodeConfigPanel,
      end: EndNodeConfigPanel,
    });
    expect(Object.isFrozen(WORKFLOW_NODE_CONFIG_PANELS)).toBe(true);
  });

  it("maps known kinds to safe specialized cards and only unknowns to Unsupported", () => {
    expect(WORKFLOW_NODE_TYPES).toEqual({
      start: StartWorkflowNode,
      llm: LlmWorkflowNode,
      condition: ConditionWorkflowNode,
      transform: TransformWorkflowNode,
      variable_aggregate: VariableAggregateWorkflowNode,
      loop: LoopWorkflowNode,
      http_request: HttpRequestWorkflowNode,
      python_code: PythonCodeWorkflowNode,
      end: EndWorkflowNode,
      unsupported: WorkflowNodeCard,
    });
  });
});

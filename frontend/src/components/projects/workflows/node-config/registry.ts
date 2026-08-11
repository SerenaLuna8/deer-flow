import type { WorkflowNodeConfigPanelRegistry } from "@/components/projects/workflows/node-config/contracts";

import { BASIC_WORKFLOW_NODE_CONFIG_PANELS } from "./basic";
import {
  ConditionNodeConfigPanel,
  VariableAggregateNodeConfigPanel,
} from "./branching";
import { PYTHON_CODE_WORKFLOW_NODE_CONFIG_PANELS } from "./code";
import { HTTP_WORKFLOW_NODE_CONFIG_PANELS } from "./http";
import { LoopNodeConfigPanel } from "./loop";

/** Closed first-batch panel authority; unknown/future kinds never enter it. */
export const WORKFLOW_NODE_CONFIG_PANELS = Object.freeze({
  start: BASIC_WORKFLOW_NODE_CONFIG_PANELS.start,
  llm: BASIC_WORKFLOW_NODE_CONFIG_PANELS.llm,
  condition: ConditionNodeConfigPanel,
  transform: BASIC_WORKFLOW_NODE_CONFIG_PANELS.transform,
  variable_aggregate: VariableAggregateNodeConfigPanel,
  loop: LoopNodeConfigPanel,
  http_request: HTTP_WORKFLOW_NODE_CONFIG_PANELS.http_request,
  python_code: PYTHON_CODE_WORKFLOW_NODE_CONFIG_PANELS.python_code,
  end: BASIC_WORKFLOW_NODE_CONFIG_PANELS.end,
}) satisfies WorkflowNodeConfigPanelRegistry;

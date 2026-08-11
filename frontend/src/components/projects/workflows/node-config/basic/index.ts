import type { WorkflowNodeConfigPanel } from "@/components/projects/workflows/node-config/contracts";

import { EndNodeConfigPanel } from "./end-node-config-panel";
import { LlmNodeConfigPanel } from "./llm-node-config-panel";
import { StartNodeConfigPanel } from "./start-node-config-panel";
import { TransformNodeConfigPanel } from "./transform-node-config-panel";

export {
  buildWorkflowOutputMove,
  buildWorkflowOutputRemoval,
  buildWorkflowOutputReplacement,
  EndNodeConfigPanel,
} from "./end-node-config-panel";
export {
  buildLlmNodeConfigUpdate,
  LlmNodeConfigPanel,
} from "./llm-node-config-panel";
export {
  buildWorkflowInputMove,
  buildWorkflowInputRemoval,
  buildWorkflowInputReplacement,
  StartNodeConfigPanel,
} from "./start-node-config-panel";
export {
  buildTransformNodeConfigUpdate,
  TransformNodeConfigPanel,
} from "./transform-node-config-panel";

export const BASIC_WORKFLOW_NODE_CONFIG_PANELS = Object.freeze({
  start: StartNodeConfigPanel,
  llm: LlmNodeConfigPanel,
  transform: TransformNodeConfigPanel,
  end: EndNodeConfigPanel,
} satisfies Record<
  "start" | "llm" | "transform" | "end",
  WorkflowNodeConfigPanel
>);

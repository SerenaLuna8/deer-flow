import type { AIMessage } from "@langchain/langgraph-sdk";

import type { ExecutionApprovalStatus } from "../execution-approvals/schemas";
import type { TokenUsage } from "../messages/usage";

import type { SubtaskStep } from "./steps";

export type SubtaskStatusSource =
  | "inferred"
  | "custom_event"
  | "tool_result"
  | "execution_approval";

export type SubtaskStopReason =
  | "token_capped"
  | "turn_capped"
  | "loop_capped"
  | "tool_budget_capped"
  | "output_truncated";

export interface SubtaskExecutionApprovalState {
  approvalId: string;
  status: ExecutionApprovalStatus;
  exitCode?: number;
  reasonCode?: string;
}

export interface Subtask {
  id: string;
  /** Server-owned Context Subject identity; never derived from the tool call id. */
  executionId?: string;
  status: "unknown" | "in_progress" | "completed" | "failed";
  /**
   * Where the current status came from. Pending card inference is weaker than
   * the streamed lifecycle event, while the structured ToolMessage remains the
   * final authority. The rank prevents a later render of the original task
   * tool call from overwriting a terminal result.
   */
  statusSource?: SubtaskStatusSource;
  /** Presentation state for a delegated Local-host approval. */
  executionApproval?: SubtaskExecutionApprovalState;
  subagent_type: string;
  description: string;
  /** Effective Fluva model selected for this delegated run. */
  modelName?: string;
  /** Latest cumulative token snapshot reported by the delegated run. */
  usage?: TokenUsage;
  latestMessage?: AIMessage;
  /**
   * Full ordered step history (assistant turns + tool outputs) of the subagent.
   * Accumulated live from `task_running` events and backfilled on expand for
   * historical runs (#3779). Replaces the old "only latestMessage" behavior.
   */
  steps?: SubtaskStep[];
  prompt: string;
  result?: string;
  error?: string;
  /**
   * Why the run ended without a clean final response (a guardrail cap or
   * Provider output truncation), or ``undefined`` for a clean run. The pill
   * status stays normal (``completed``/``failed``); the task surface shows the
   * reason without parsing result text.
   */
  stopReason?: SubtaskStopReason;
}

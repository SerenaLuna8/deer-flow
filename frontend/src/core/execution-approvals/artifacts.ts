import type { Message } from "@langchain/langgraph-sdk";
import { z } from "zod";

const executionApprovalArtifactSchema = z
  .object({
    schema_version: z.literal(1),
    kind: z.literal("local_shell"),
    approval_id: z.string().uuid(),
    source_run_id: z.string().min(1).max(256),
    source_tool_call_id: z.string().min(1).max(128),
  })
  .strict();

export type ExecutionApprovalArtifact = z.infer<
  typeof executionApprovalArtifactSchema
>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function extractExecutionApprovalArtifact(
  message: Message,
): ExecutionApprovalArtifact | null {
  if (message.type !== "tool") return null;
  const artifact = Reflect.get(message, "artifact");
  if (!isRecord(artifact)) return null;
  const parsed = executionApprovalArtifactSchema.safeParse(
    artifact.host_execution_approval,
  );
  return parsed.success ? parsed.data : null;
}

export function findLatestExecutionApprovalArtifact(
  messages: readonly Message[],
): ExecutionApprovalArtifact | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const artifact = extractExecutionApprovalArtifact(messages[index]!);
    if (artifact) return artifact;
  }
  return null;
}

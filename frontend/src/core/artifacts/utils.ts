import type { AgentThread } from "../threads";

export function extractArtifactsFromThread(thread: AgentThread) {
  return thread.values.artifacts ?? [];
}

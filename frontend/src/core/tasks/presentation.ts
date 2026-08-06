import { formatTokenCount, type TokenUsage } from "@/core/messages/usage";
import type { Model } from "@/core/models/types";

export function resolveSubtaskModelLabel(
  modelName: string | undefined,
  models: Model[],
): string | undefined {
  if (!modelName) {
    return undefined;
  }
  return (
    models.find((model) => model.name === modelName)?.display_name ?? modelName
  );
}

export function formatSubtaskTokenUsage(
  usage: TokenUsage | undefined,
): string | undefined {
  return usage ? formatTokenCount(usage.totalTokens) : undefined;
}

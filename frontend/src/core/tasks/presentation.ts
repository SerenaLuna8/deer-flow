import { formatTokenCount, type TokenUsage } from "@/core/messages/usage";
import { resolveModelDisplayName } from "@/core/models/presentation";
import type { Model } from "@/core/models/types";

export function resolveSubtaskModelLabel(
  modelName: string | undefined,
  models: Model[],
): string | undefined {
  if (!modelName) {
    return undefined;
  }
  return resolveModelDisplayName(modelName, models);
}

export function formatSubtaskTokenUsage(
  usage: TokenUsage | undefined,
): string | undefined {
  return usage ? formatTokenCount(usage.totalTokens) : undefined;
}

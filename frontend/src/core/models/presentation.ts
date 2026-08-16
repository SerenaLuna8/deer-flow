import type { Model } from "./types";

export function resolveModelDisplayName(
  modelRef: string | null | undefined,
  models: readonly Model[],
): string | undefined {
  if (!modelRef) return undefined;
  const model =
    modelRef === "default"
      ? models.find((candidate) => candidate.is_default)
      : models.find((candidate) => candidate.name === modelRef);
  return model?.display_name;
}

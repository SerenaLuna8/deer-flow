export type KnowledgeProcessingUnitProfile = Readonly<{
  chunk: Readonly<{ unit: "character" | "token" }>;
}>;

/** Historical documents without a profile retain their character semantics. */
export function processingUnitLabel(
  profile: KnowledgeProcessingUnitProfile | null,
): "characters" | "knowledgeTokens" {
  return profile?.chunk.unit === "token" ? "knowledgeTokens" : "characters";
}

const IMAGE_FAILURE_WARNING_CODES = new Set([
  "IMAGE_CORRUPT",
  "IMAGE_LIMIT_EXCEEDED",
]);

export function parseWarningSummary(
  warnings: readonly Readonly<{ code: string }>[],
): Readonly<{
  total: number;
  imageFailures: number;
  headerInferred: boolean;
}> {
  return {
    total: warnings.length,
    imageFailures: warnings.filter((warning) =>
      IMAGE_FAILURE_WARNING_CODES.has(warning.code),
    ).length,
    headerInferred: warnings.some(
      (warning) => warning.code === "HEADER_INFERRED",
    ),
  };
}

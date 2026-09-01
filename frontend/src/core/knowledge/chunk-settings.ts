/**
 * Client-side mirrors of the backend chunk-parameter and name bounds
 * (`backend/packages/knowledge/actweave_knowledge/documents/service.py` and
 * `bases/service.py`). The wizard creates the base before uploading files, so
 * out-of-range parameters accepted client-side would strand the user with an
 * empty base whose uploads can only be rejected — the form bounds must match
 * the backend exactly.
 */

import { knowledgeHeaderRuleSchema, type KnowledgeHeaderRule } from "./types";

export const KNOWLEDGE_CHUNK_SIZE_MIN = 200;
export const KNOWLEDGE_CHUNK_SIZE_MAX = 4000;
export const KNOWLEDGE_CHUNK_OVERLAP_MIN = 0;
export const KNOWLEDGE_CHUNK_OVERLAP_MAX = 500;
export const KNOWLEDGE_CHILD_CHUNK_SIZE_MIN = 100;
export const KNOWLEDGE_CHILD_CHUNK_SIZE_MAX = 2000;
export const KNOWLEDGE_SEPARATOR_MAX_CHARS = 64;
export const KNOWLEDGE_BASE_NAME_MAX_CHARS = 120;

export type KnowledgeChunkLimits = Readonly<{
  parent_min: number;
  parent_max: number;
  overlap_max: number;
  child_min: number;
  child_max: number;
}>;

const DEFAULT_CHUNK_LIMITS: KnowledgeChunkLimits = {
  parent_min: KNOWLEDGE_CHUNK_SIZE_MIN,
  parent_max: KNOWLEDGE_CHUNK_SIZE_MAX,
  overlap_max: KNOWLEDGE_CHUNK_OVERLAP_MAX,
  child_min: KNOWLEDGE_CHILD_CHUNK_SIZE_MIN,
  child_max: KNOWLEDGE_CHILD_CHUNK_SIZE_MAX,
};

export function isChunkSizeValid(
  chunkSize: number,
  limits: KnowledgeChunkLimits = DEFAULT_CHUNK_LIMITS,
): boolean {
  return (
    Number.isSafeInteger(chunkSize) &&
    chunkSize >= limits.parent_min &&
    chunkSize <= limits.parent_max
  );
}

export function isChunkOverlapValid(
  chunkOverlap: number,
  chunkSize: number,
  limits: KnowledgeChunkLimits = DEFAULT_CHUNK_LIMITS,
): boolean {
  return (
    Number.isSafeInteger(chunkOverlap) &&
    chunkOverlap >= KNOWLEDGE_CHUNK_OVERLAP_MIN &&
    chunkOverlap <= limits.overlap_max &&
    chunkOverlap < chunkSize
  );
}

/** Validated in the escaped form exactly as typed (`\n\n`), never trimmed. */
export function isChunkSeparatorValid(separator: string): boolean {
  return (
    separator.length > 0 && separator.length <= KNOWLEDGE_SEPARATOR_MAX_CHARS
  );
}

export function isChildChunkSizeValid(
  childChunkSize: number,
  chunkSize: number,
  limits: KnowledgeChunkLimits = DEFAULT_CHUNK_LIMITS,
): boolean {
  return (
    Number.isSafeInteger(childChunkSize) &&
    childChunkSize >= limits.child_min &&
    childChunkSize <= limits.child_max &&
    childChunkSize < chunkSize
  );
}

export function normalizeKnowledgeExtension(value: string): string | null {
  const normalized = value.trim().toLowerCase();
  const extension = normalized.startsWith(".") ? normalized : `.${normalized}`;
  return /^\.[a-z0-9]+$/u.test(extension) ? extension : null;
}

export function knowledgeFileExtension(fileName: string): string | null {
  const dot = fileName.lastIndexOf(".");
  return dot >= 0 ? normalizeKnowledgeExtension(fileName.slice(dot)) : null;
}

export function normalizeKnowledgeHeaderRules(
  rules: readonly KnowledgeHeaderRule[],
): KnowledgeHeaderRule[] {
  const parsed = rules.map((rule) => knowledgeHeaderRuleSchema.parse(rule));
  if (new Set(parsed.map((rule) => rule.sheet)).size !== parsed.length) {
    throw new Error("duplicate sheet header rule");
  }
  return parsed.sort((left, right) =>
    (left.sheet ?? "").localeCompare(right.sheet ?? "", "en"),
  );
}

/**
 * Client-side mirrors of the backend chunk-parameter and name bounds
 * (`backend/packages/knowledge/actweave_knowledge/documents/service.py` and
 * `bases/service.py`). The wizard creates the base before uploading files, so
 * out-of-range parameters accepted client-side would strand the user with an
 * empty base whose uploads can only be rejected — the form bounds must match
 * the backend exactly.
 */

export const KNOWLEDGE_CHUNK_SIZE_MIN = 200;
export const KNOWLEDGE_CHUNK_SIZE_MAX = 4000;
export const KNOWLEDGE_CHUNK_OVERLAP_MIN = 0;
export const KNOWLEDGE_CHUNK_OVERLAP_MAX = 500;
export const KNOWLEDGE_CHILD_CHUNK_SIZE_MIN = 100;
export const KNOWLEDGE_CHILD_CHUNK_SIZE_MAX = 2000;
export const KNOWLEDGE_SEPARATOR_MAX_CHARS = 64;
export const KNOWLEDGE_BASE_NAME_MAX_CHARS = 120;

export function isChunkSizeValid(chunkSize: number): boolean {
  return (
    Number.isSafeInteger(chunkSize) &&
    chunkSize >= KNOWLEDGE_CHUNK_SIZE_MIN &&
    chunkSize <= KNOWLEDGE_CHUNK_SIZE_MAX
  );
}

export function isChunkOverlapValid(
  chunkOverlap: number,
  chunkSize: number,
): boolean {
  return (
    Number.isSafeInteger(chunkOverlap) &&
    chunkOverlap >= KNOWLEDGE_CHUNK_OVERLAP_MIN &&
    chunkOverlap <= KNOWLEDGE_CHUNK_OVERLAP_MAX &&
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
): boolean {
  return (
    Number.isSafeInteger(childChunkSize) &&
    childChunkSize >= KNOWLEDGE_CHILD_CHUNK_SIZE_MIN &&
    childChunkSize <= KNOWLEDGE_CHILD_CHUNK_SIZE_MAX &&
    childChunkSize < chunkSize
  );
}

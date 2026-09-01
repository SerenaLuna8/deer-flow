/**
 * Identity tracking for the wizard's chunk-preview panel.
 *
 * Every preview request carries a full identity — the exact File object, a
 * parameter snapshot, the owning scope, and a monotonically increasing
 * sequence number. The reducer publishes a response only while its identity
 * is still the newest one, so a slow request that was replaced (new file,
 * resubmitted parameters, removed file, or a scope switch) can never
 * overwrite the preview the user is actually looking at.
 */

import {
  DEFAULT_CHILD_CHUNK_SEPARATOR,
  type KnowledgeChunkingMode,
  type KnowledgeChunkPreviewResponse,
  type KnowledgeHeaderRule,
  type KnowledgeProcessingParameters,
} from "./types";

export type KnowledgePreviewParams = {
  chunk_size: number;
  chunk_overlap: number;
  chunk_separator: string;
  remove_extra_spaces: boolean;
  remove_urls_emails: boolean;
  chunking_mode: KnowledgeChunkingMode;
  child_chunk_size?: number;
  child_chunk_separator?: string;
  unit: "character" | "token";
  tokenizer_profile_id: string | null;
  capability_revision: string;
  header_rules: KnowledgeHeaderRule[];
};

export type KnowledgePreviewIdentity = {
  file: File;
  params: KnowledgePreviewParams;
  scopeKey: string;
  sequence: number;
};

export type KnowledgeSuccessfulPreview = {
  file: File;
  params: KnowledgePreviewParams;
  fingerprint: string;
};

export type KnowledgePreviewState = {
  /** Identity of the newest request; anything older is a latecomer. */
  current: KnowledgePreviewIdentity | null;
  status: "idle" | "loading" | "success" | "error";
  /** Payload belonging to `current`, never a replaced request's. */
  data: KnowledgeChunkPreviewResponse | null;
  error: unknown;
};

export const KNOWLEDGE_PREVIEW_IDLE: KnowledgePreviewState = {
  current: null,
  status: "idle",
  data: null,
  error: null,
};

export type KnowledgePreviewEvent =
  | { type: "requested"; identity: KnowledgePreviewIdentity }
  | {
      type: "resolved";
      scopeKey: string;
      sequence: number;
      data: KnowledgeChunkPreviewResponse;
    }
  | { type: "failed"; scopeKey: string; sequence: number; error: unknown }
  | { type: "file_removed"; file: File }
  | { type: "scope_changed"; scopeKey: string };

export function previewParamsEqual(
  left: KnowledgePreviewParams,
  right: KnowledgePreviewParams,
): boolean {
  const canonicalHeaderRules = (rules: KnowledgeHeaderRule[]) =>
    [...rules].sort((left, right) => {
      const leftSheet = left.sheet ?? "";
      const rightSheet = right.sheet ?? "";
      return leftSheet.localeCompare(rightSheet, "en");
    });
  return (
    left.chunk_size === right.chunk_size &&
    left.chunk_overlap === right.chunk_overlap &&
    left.chunk_separator === right.chunk_separator &&
    left.remove_extra_spaces === right.remove_extra_spaces &&
    left.remove_urls_emails === right.remove_urls_emails &&
    left.chunking_mode === right.chunking_mode &&
    left.child_chunk_size === right.child_chunk_size &&
    left.child_chunk_separator === right.child_chunk_separator &&
    left.unit === right.unit &&
    left.tokenizer_profile_id === right.tokenizer_profile_id &&
    left.capability_revision === right.capability_revision &&
    JSON.stringify(canonicalHeaderRules(left.header_rules)) ===
      JSON.stringify(canonicalHeaderRules(right.header_rules))
  );
}

export function matchingPreviewFingerprint(
  record: KnowledgeSuccessfulPreview | null | undefined,
  file: File,
  params: KnowledgePreviewParams,
): string | null {
  return record?.file === file && previewParamsEqual(record.params, params)
    ? record.fingerprint
    : null;
}

export function previewProcessingParameters(
  params: KnowledgePreviewParams,
): KnowledgeProcessingParameters {
  return {
    unit: params.unit,
    mode: params.chunking_mode,
    size: params.chunk_size,
    overlap: params.chunk_overlap,
    separator: params.chunk_separator,
    child_size: params.child_chunk_size ?? 500,
    child_separator:
      params.child_chunk_separator ?? DEFAULT_CHILD_CHUNK_SEPARATOR,
    remove_extra_spaces: params.remove_extra_spaces,
    remove_urls_emails: params.remove_urls_emails,
    header_rules: params.header_rules,
  };
}

function settles(
  state: KnowledgePreviewState,
  scopeKey: string,
  sequence: number,
): boolean {
  return (
    state.current !== null &&
    state.current.scopeKey === scopeKey &&
    state.current.sequence === sequence
  );
}

export function knowledgePreviewReducer(
  state: KnowledgePreviewState,
  event: KnowledgePreviewEvent,
): KnowledgePreviewState {
  switch (event.type) {
    case "requested":
      // Only the current file's preview is ever kept, so a new request
      // always starts from a blank panel rather than another identity's
      // payload.
      return {
        current: event.identity,
        status: "loading",
        data: null,
        error: null,
      };
    case "resolved":
      if (!settles(state, event.scopeKey, event.sequence)) return state;
      return { ...state, status: "success", data: event.data, error: null };
    case "failed":
      if (!settles(state, event.scopeKey, event.sequence)) return state;
      return { ...state, status: "error", data: null, error: event.error };
    case "file_removed":
      return state.current?.file === event.file
        ? KNOWLEDGE_PREVIEW_IDLE
        : state;
    case "scope_changed":
      return state.current !== null && state.current.scopeKey !== event.scopeKey
        ? KNOWLEDGE_PREVIEW_IDLE
        : state;
  }
}

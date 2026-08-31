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

import type {
  KnowledgeChunkingMode,
  KnowledgeChunkPreviewResponse,
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
};

export type KnowledgePreviewIdentity = {
  file: File;
  params: KnowledgePreviewParams;
  scopeKey: string;
  sequence: number;
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
  return (
    left.chunk_size === right.chunk_size &&
    left.chunk_overlap === right.chunk_overlap &&
    left.chunk_separator === right.chunk_separator &&
    left.remove_extra_spaces === right.remove_extra_spaces &&
    left.remove_urls_emails === right.remove_urls_emails &&
    left.chunking_mode === right.chunking_mode &&
    left.child_chunk_size === right.child_chunk_size &&
    left.child_chunk_separator === right.child_chunk_separator
  );
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

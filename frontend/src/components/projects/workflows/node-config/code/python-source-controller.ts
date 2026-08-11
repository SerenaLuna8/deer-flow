import type { WorkflowEditorFlushRegistry } from "@/core/project-workflows/editor/flush-registry";
import { utf8ByteLength } from "@/core/project-workflows/validation";

export { utf8ByteLength } from "@/core/project-workflows/validation";

export type PythonSourceCommitOutcome =
  | Readonly<{ applied: true }>
  | Readonly<{ applied: false; safeMessage?: string }>;

export type PythonSourceControllerState = Readonly<{
  buffer: string;
  persistedSource: string;
  dirty: boolean;
  generation: number;
  byteLength: number;
  issue: string | null;
}>;

export type PythonSourceController = Readonly<{
  getState(): PythonSourceControllerState;
  subscribe(listener: () => void): () => void;
  edit(source: string): void;
  receiveExternalSource(source: string): void;
  updateMaxBytes(maxBytes: number): void;
  commit(): void;
  acknowledgeCommitted(source: string): void;
  detach(): void;
}>;

const activeFlushTokens = new WeakMap<
  WorkflowEditorFlushRegistry,
  Map<string, object>
>();

const flushTokensFor = (
  registry: WorkflowEditorFlushRegistry,
): Map<string, object> => {
  const existing = activeFlushTokens.get(registry);
  if (existing) return existing;
  const created = new Map<string, object>();
  activeFlushTokens.set(registry, created);
  return created;
};

const oversizedIssue = (byteLength: number, maxBytes: number): string =>
  `Python source 为 ${byteLength} UTF-8 bytes，超过 Catalog 上限 ${maxBytes} UTF-8 bytes。`;

export function createPythonSourceController({
  commitSource,
  flushKey,
  initialSource,
  maxBytes: initialMaxBytes,
  registry,
  debounceMs = 350,
}: {
  commitSource: (source: string) => PythonSourceCommitOutcome;
  flushKey: string;
  initialSource: string;
  maxBytes: number;
  registry: WorkflowEditorFlushRegistry;
  debounceMs?: number;
}): PythonSourceController {
  let maxBytes = initialMaxBytes;
  let state: PythonSourceControllerState = Object.freeze({
    buffer: initialSource,
    persistedSource: initialSource,
    dirty: false,
    generation: 0,
    byteLength: utf8ByteLength(initialSource),
    issue: null,
  });
  let releaseRegistration: (() => void) | null = null;
  let flushToken: object | null = null;
  let debounceTimer: ReturnType<typeof setTimeout> | null = null;
  const listeners = new Set<() => void>();

  const publish = (next: PythonSourceControllerState) => {
    if (
      next.buffer === state.buffer &&
      next.persistedSource === state.persistedSource &&
      next.dirty === state.dirty &&
      next.generation === state.generation &&
      next.byteLength === state.byteLength &&
      next.issue === state.issue
    ) {
      return;
    }
    state = Object.freeze(next);
    for (const listener of listeners) listener();
  };

  const clearRegistration = () => {
    releaseRegistration?.();
    releaseRegistration = null;
    const tokens = flushTokensFor(registry);
    if (flushToken !== null && tokens.get(flushKey) === flushToken) {
      tokens.delete(flushKey);
    }
    flushToken = null;
  };

  const clearDebounce = () => {
    if (debounceTimer !== null) clearTimeout(debounceTimer);
    debounceTimer = null;
  };

  const flushGeneration = (generation: number) => {
    if (
      !state.dirty ||
      state.generation !== generation ||
      flushToken === null ||
      flushTokensFor(registry).get(flushKey) !== flushToken
    ) {
      return;
    }
    clearDebounce();
    const byteLength = utf8ByteLength(state.buffer);
    if (
      !Number.isSafeInteger(maxBytes) ||
      maxBytes <= 0 ||
      byteLength > maxBytes
    ) {
      const issue = oversizedIssue(byteLength, maxBytes);
      publish({ ...state, byteLength, issue });
      throw new Error(issue);
    }

    let outcome: PythonSourceCommitOutcome;
    try {
      outcome = commitSource(state.buffer);
    } catch (error) {
      const issue =
        error instanceof Error && error.message
          ? error.message
          : "Python source 无法写入 Workflow Draft。";
      publish({ ...state, issue });
      throw error;
    }
    if (!outcome.applied) {
      const issue =
        outcome.safeMessage ?? "Python source 无法写入 Workflow Draft。";
      publish({ ...state, issue });
      throw new Error(issue);
    }

    const committedSource = state.buffer;
    publish({
      ...state,
      persistedSource: committedSource,
      dirty: false,
      byteLength,
      issue: null,
    });
    clearRegistration();
  };

  const registerGeneration = () => {
    clearRegistration();
    clearDebounce();
    if (!state.dirty) return;
    const generation = state.generation;
    flushToken = Object.freeze({ generation });
    flushTokensFor(registry).set(flushKey, flushToken);
    releaseRegistration = registry.register(flushKey, () =>
      flushGeneration(generation),
    );
    if (Number.isFinite(debounceMs) && debounceMs >= 0) {
      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        try {
          flushGeneration(generation);
        } catch {
          // Preserve both the safe issue and pending registry generation.
        }
      }, debounceMs);
    }
  };

  return Object.freeze({
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    edit(source) {
      const byteLength = utf8ByteLength(source);
      publish({
        ...state,
        buffer: source,
        dirty: source !== state.persistedSource,
        generation: state.generation + 1,
        byteLength,
        issue:
          Number.isSafeInteger(maxBytes) &&
          maxBytes > 0 &&
          byteLength > maxBytes
            ? oversizedIssue(byteLength, maxBytes)
            : null,
      });
      registerGeneration();
    },
    receiveExternalSource(source) {
      if (state.dirty && source !== state.buffer) {
        publish({ ...state, persistedSource: source });
        return;
      }
      publish({
        ...state,
        buffer: source,
        persistedSource: source,
        dirty: false,
        byteLength: utf8ByteLength(source),
        issue: null,
      });
      clearRegistration();
      clearDebounce();
    },
    updateMaxBytes(nextMaxBytes) {
      maxBytes = nextMaxBytes;
      const byteLength = utf8ByteLength(state.buffer);
      publish({
        ...state,
        byteLength,
        issue:
          Number.isSafeInteger(maxBytes) &&
          maxBytes > 0 &&
          byteLength > maxBytes
            ? oversizedIssue(byteLength, maxBytes)
            : state.issue?.includes("UTF-8 bytes")
              ? null
              : state.issue,
      });
    },
    commit() {
      flushGeneration(state.generation);
    },
    acknowledgeCommitted(source) {
      if (source !== state.buffer) return;
      publish({
        ...state,
        persistedSource: source,
        dirty: false,
        byteLength: utf8ByteLength(source),
        issue: null,
      });
      clearRegistration();
      clearDebounce();
    },
    detach() {
      // Keep a dirty registration flushable by save/validate/publish after the
      // Inspector unmounts, but never allow its stale debounce timer to race a
      // newer editor instance with the same Workbench-local key.
      clearDebounce();
    },
  });
}

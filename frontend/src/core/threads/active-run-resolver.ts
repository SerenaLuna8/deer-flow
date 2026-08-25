import { z } from "zod";

import {
  projectClientScopeSchema,
  type RunMetadataStorage,
} from "../private-work/types";

const activeRunScopeSchema = projectClientScopeSchema
  .extend({ threadId: z.string().uuid() })
  .strict();
const runIdSchema = z.string().uuid();
const runStatusSchema = z.enum([
  "pending",
  "running",
  "error",
  "success",
  "timeout",
  "interrupted",
]);
const activeRunCatalogEntrySchema = z
  .object({
    run_id: runIdSchema,
    status: runStatusSchema,
  })
  .strict();
const activeRunCatalogSchema = z.array(activeRunCatalogEntrySchema);

const ACTIVE_RUN_STATUSES = new Set<z.infer<typeof runStatusSchema>>([
  "pending",
  "running",
]);

export type ActiveRunScope = z.infer<typeof activeRunScopeSchema>;

export type ActiveRunCatalogEntry = Readonly<
  z.infer<typeof activeRunCatalogEntrySchema>
>;

export type ActiveRunResolution =
  | Readonly<{
      kind: "resolved";
      runId: string;
      generation: number;
      resumeFromHint: boolean;
      source: "admission" | "catalog";
    }>
  | Readonly<{ kind: "none"; generation: number }>
  | Readonly<{ kind: "conflict"; generation: number }>
  | Readonly<{ kind: "unavailable"; generation: number }>;

export type ActiveRunCatalogReader = (
  scope: ActiveRunScope,
  signal: AbortSignal,
) => Promise<unknown>;

export type ActiveRunResolverGeneration = Readonly<{
  generation: number;
  reconnectStorage: RunMetadataStorage;
  onCreated(runId: string): ActiveRunResolution | null;
  resolveFromServerCatalog(): Promise<ActiveRunResolution | null>;
  dispose(): void;
}>;

export type ActiveRunResolver = Readonly<{
  begin(input: {
    scope: ActiveRunScope;
    reconnectStorage: RunMetadataStorage;
    readServerCatalog: ActiveRunCatalogReader;
  }): ActiveRunResolverGeneration;
}>;

type GenerationState = {
  generation: number;
  scope: ActiveRunScope;
  reconnectStorage: RunMetadataStorage;
  readServerCatalog: ActiveRunCatalogReader;
  operation: number;
  controller: AbortController | null;
  canonicalRunId: string | null;
};

function reconnectKey(threadId: string): `lg:stream:${string}` {
  return `lg:stream:${threadId}`;
}

function clearReconnectValue(
  storage: RunMetadataStorage,
  key: `lg:stream:${string}`,
  expected: string,
): void {
  if (storage.getItem(key) === expected) {
    storage.removeItem(key);
  }
}

function activeRunIds(catalog: readonly ActiveRunCatalogEntry[]): string[] {
  return [
    ...new Set(
      catalog.flatMap((entry) =>
        ACTIVE_RUN_STATUSES.has(entry.status) && entry.run_id
          ? [entry.run_id]
          : [],
      ),
    ),
  ];
}

export function createActiveRunResolver(): ActiveRunResolver {
  let generationCounter = 0;
  let current: GenerationState | null = null;

  return {
    begin(input) {
      current?.controller?.abort();
      const state: GenerationState = {
        generation: ++generationCounter,
        scope: activeRunScopeSchema.parse(input.scope),
        reconnectStorage: input.reconnectStorage,
        readServerCatalog: input.readServerCatalog,
        operation: 0,
        controller: null,
        canonicalRunId: null,
      };
      current = state;
      const key = reconnectKey(state.scope.threadId);
      const isCurrent = () => current === state;
      const adapter: RunMetadataStorage = {
        getItem(selectedKey) {
          if (
            !isCurrent() ||
            selectedKey !== key ||
            state.canonicalRunId === null
          ) {
            return null;
          }
          const stored = state.reconnectStorage.getItem(key);
          return stored === state.canonicalRunId ? stored : null;
        },
        setItem(selectedKey, value) {
          if (
            isCurrent() &&
            selectedKey === key &&
            value === state.canonicalRunId
          ) {
            state.reconnectStorage.setItem(key, value);
          }
        },
        removeItem(selectedKey) {
          if (!isCurrent() || selectedKey !== key) return;
          const stored = state.reconnectStorage.getItem(key);
          if (stored !== null && stored === state.canonicalRunId) {
            state.reconnectStorage.removeItem(key);
          }
        },
      };

      return {
        generation: state.generation,
        reconnectStorage: adapter,
        onCreated(runId) {
          if (!isCurrent()) return null;
          const selectedRunId = runIdSchema.parse(runId);
          state.operation += 1;
          state.controller?.abort();
          state.controller = null;
          const hint = state.reconnectStorage.getItem(key);
          if (hint !== null && hint !== selectedRunId) {
            clearReconnectValue(state.reconnectStorage, key, hint);
          }
          state.canonicalRunId = selectedRunId;
          return {
            kind: "resolved",
            runId: selectedRunId,
            generation: state.generation,
            resumeFromHint: false,
            source: "admission",
          };
        },
        async resolveFromServerCatalog() {
          const operation = ++state.operation;
          state.canonicalRunId = null;
          state.controller?.abort();
          const controller = new AbortController();
          state.controller = controller;
          let catalog: readonly ActiveRunCatalogEntry[];
          try {
            catalog = activeRunCatalogSchema.parse(
              await state.readServerCatalog(state.scope, controller.signal),
            );
          } catch {
            if (!isCurrent() || state.operation !== operation) return null;
            return { kind: "unavailable", generation: state.generation };
          }
          if (!isCurrent() || state.operation !== operation) return null;
          const runIds = activeRunIds(catalog);
          if (runIds.length !== 1) {
            if (runIds.length === 0) {
              const hint = state.reconnectStorage.getItem(key);
              if (hint !== null) {
                clearReconnectValue(state.reconnectStorage, key, hint);
              }
            }
            return {
              kind: runIds.length === 0 ? "none" : "conflict",
              generation: state.generation,
            };
          }
          const runId = runIds[0]!;
          const hint = state.reconnectStorage.getItem(key);
          if (hint !== null && hint !== runId) {
            clearReconnectValue(state.reconnectStorage, key, hint);
          }
          state.canonicalRunId = runId;
          return {
            kind: "resolved",
            runId,
            generation: state.generation,
            resumeFromHint: hint === runId,
            source: "catalog",
          };
        },
        dispose() {
          if (!isCurrent()) return;
          state.controller?.abort();
          current = null;
        },
      };
    },
  };
}

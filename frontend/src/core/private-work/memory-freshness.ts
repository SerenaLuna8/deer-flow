"use client";

import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { z } from "zod";

import { projectPageSchema, projectSchema } from "@/core/projects/types";

import { privateWorkQueryKey } from "./query-keys";
import { projectClientScopeSchema, type ProjectClientScope } from "./types";

export const PROJECT_CACHE_HINT_CHANNEL = "actweave:project-cache:v1";

export const projectMemoryCacheChangeSchema = z.enum([
  "pending",
  "document",
  "versions",
  "episodes",
  "reset",
]);

export const projectCacheHintSchema = projectClientScopeSchema
  .extend({
    schemaVersion: z.literal(1),
    eventId: z.string().uuid(),
    sourceTabId: z.string().uuid(),
    domain: z.literal("memory"),
    change: projectMemoryCacheChangeSchema,
  })
  .strict();

export const accountAgentRuntimeCacheHintSchema = projectClientScopeSchema
  .pick({ accountId: true })
  .extend({
    schemaVersion: z.literal(1),
    eventId: z.string().uuid(),
    sourceTabId: z.string().uuid(),
    domain: z.literal("agent_runtime"),
    change: z.literal("context_usage"),
  })
  .strict();

export type ProjectMemoryCacheChange = z.infer<
  typeof projectMemoryCacheChangeSchema
>;
export type ProjectCacheHint = z.infer<typeof projectCacheHintSchema>;
export type AccountAgentRuntimeCacheHint = z.infer<
  typeof accountAgentRuntimeCacheHintSchema
>;

type CacheHintMessageEvent = { readonly data: unknown };

export interface ProjectCacheHintChannel {
  postMessage(message: unknown): void;
  addEventListener(
    type: "message",
    listener: (event: CacheHintMessageEvent) => void,
  ): void;
  removeEventListener(
    type: "message",
    listener: (event: CacheHintMessageEvent) => void,
  ): void;
  close(): void;
}

export type ProjectCacheHintChannelFactory = (
  name: string,
) => ProjectCacheHintChannel | null;

const DEFAULT_DEDUPLICATION_LIMIT = 256;
let sourceTabId: string | null = null;

function randomUUID(): string | null {
  const browserCrypto = globalThis.crypto;
  return browserCrypto && typeof browserCrypto.randomUUID === "function"
    ? browserCrypto.randomUUID()
    : null;
}

function currentSourceTabId(): string | null {
  sourceTabId ??= randomUUID();
  return sourceTabId;
}

function defaultChannelFactory(name: string): ProjectCacheHintChannel | null {
  if (typeof globalThis.BroadcastChannel !== "function") return null;
  return new globalThis.BroadcastChannel(
    name,
  ) as unknown as ProjectCacheHintChannel;
}

function sameScope(
  left: ProjectClientScope,
  right: ProjectClientScope,
): boolean {
  return (
    left.accountId === right.accountId && left.projectId === right.projectId
  );
}

export function createProjectMemoryCacheHintSubscription({
  scope,
  onHint,
  channelFactory = defaultChannelFactory,
  tabId = currentSourceTabId(),
  eventIdFactory = randomUUID,
  deduplicationLimit = DEFAULT_DEDUPLICATION_LIMIT,
}: {
  scope: ProjectClientScope;
  onHint: (hint: ProjectCacheHint) => void;
  channelFactory?: ProjectCacheHintChannelFactory;
  tabId?: string | null;
  eventIdFactory?: () => string | null;
  deduplicationLimit?: number;
}) {
  const parsedScope = projectClientScopeSchema.parse(scope);
  const channel = tabId ? channelFactory(PROJECT_CACHE_HINT_CHANNEL) : null;
  const seenEventIds = new Set<string>();
  let disposed = false;

  const rememberEvent = (eventId: string) => {
    seenEventIds.add(eventId);
    if (seenEventIds.size <= deduplicationLimit) return;
    const oldest = seenEventIds.values().next().value;
    if (oldest !== undefined) seenEventIds.delete(oldest);
  };

  const receive = (event: CacheHintMessageEvent) => {
    if (disposed) return;
    const parsed = projectCacheHintSchema.safeParse(event.data);
    if (!parsed.success) return;
    const hint = parsed.data;
    if (
      hint.sourceTabId === tabId ||
      !sameScope(hint, parsedScope) ||
      seenEventIds.has(hint.eventId)
    ) {
      return;
    }
    rememberEvent(hint.eventId);
    onHint(hint);
  };

  channel?.addEventListener("message", receive);

  return {
    publish(change: ProjectMemoryCacheChange): ProjectCacheHint | null {
      if (disposed || !channel || !tabId) return null;
      const eventId = eventIdFactory();
      if (!eventId) return null;
      const hint = projectCacheHintSchema.parse({
        schemaVersion: 1,
        eventId,
        sourceTabId: tabId,
        accountId: parsedScope.accountId,
        projectId: parsedScope.projectId,
        domain: "memory",
        change,
      });
      channel.postMessage(hint);
      return hint;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      channel?.removeEventListener("message", receive);
      channel?.close();
      seenEventIds.clear();
    },
  };
}

export function createAccountAgentRuntimeCacheHintSubscription({
  accountId,
  onHint,
  channelFactory = defaultChannelFactory,
  tabId = currentSourceTabId(),
  eventIdFactory = randomUUID,
  deduplicationLimit = DEFAULT_DEDUPLICATION_LIMIT,
}: {
  accountId: string;
  onHint: (hint: AccountAgentRuntimeCacheHint) => void;
  channelFactory?: ProjectCacheHintChannelFactory;
  tabId?: string | null;
  eventIdFactory?: () => string | null;
  deduplicationLimit?: number;
}) {
  const parsedAccountId =
    projectClientScopeSchema.shape.accountId.parse(accountId);
  const channel = tabId ? channelFactory(PROJECT_CACHE_HINT_CHANNEL) : null;
  const seenEventIds = new Set<string>();
  let disposed = false;

  const rememberEvent = (eventId: string) => {
    seenEventIds.add(eventId);
    if (seenEventIds.size <= deduplicationLimit) return;
    const oldest = seenEventIds.values().next().value;
    if (oldest !== undefined) seenEventIds.delete(oldest);
  };

  const receive = (event: CacheHintMessageEvent) => {
    if (disposed) return;
    const parsed = accountAgentRuntimeCacheHintSchema.safeParse(event.data);
    if (!parsed.success) return;
    const hint = parsed.data;
    if (
      hint.sourceTabId === tabId ||
      hint.accountId !== parsedAccountId ||
      seenEventIds.has(hint.eventId)
    ) {
      return;
    }
    rememberEvent(hint.eventId);
    onHint(hint);
  };

  channel?.addEventListener("message", receive);

  return {
    publish(): AccountAgentRuntimeCacheHint | null {
      if (disposed || !channel || !tabId) return null;
      const eventId = eventIdFactory();
      if (!eventId) return null;
      const hint = accountAgentRuntimeCacheHintSchema.parse({
        schemaVersion: 1,
        eventId,
        sourceTabId: tabId,
        accountId: parsedAccountId,
        domain: "agent_runtime",
        change: "context_usage",
      });
      channel.postMessage(hint);
      return hint;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      channel?.removeEventListener("message", receive);
      channel?.close();
      seenEventIds.clear();
    },
  };
}

function memoryQueryKey(
  scope: ProjectClientScope,
  ...segments: readonly unknown[]
) {
  return privateWorkQueryKey(scope, "memory", ...segments);
}

export async function applyAccountAgentRuntimeCacheHint(
  queryClient: QueryClient,
  accountId: string,
): Promise<void> {
  const parsedAccountId =
    projectClientScopeSchema.shape.accountId.parse(accountId);
  const predicate = (query: { readonly queryKey: readonly unknown[] }) => {
    const key = query.queryKey;
    return (
      key[0] === "account" &&
      key[1] === parsedAccountId &&
      key[2] === "project" &&
      key[4] === "private-work" &&
      key[5] === "thread-context-usage"
    );
  };
  await queryClient.cancelQueries({ predicate });
  await queryClient.invalidateQueries({ predicate });
}

export function createAccountAgentRuntimeCacheHintListener({
  queryClient,
  accountId,
  channelFactory,
  tabId,
  eventIdFactory,
  deduplicationLimit,
}: {
  queryClient: QueryClient;
  accountId: string;
  channelFactory?: ProjectCacheHintChannelFactory;
  tabId?: string | null;
  eventIdFactory?: () => string | null;
  deduplicationLimit?: number;
}) {
  return createAccountAgentRuntimeCacheHintSubscription({
    accountId,
    channelFactory,
    tabId,
    eventIdFactory,
    deduplicationLimit,
    onHint: () => {
      void applyAccountAgentRuntimeCacheHint(queryClient, accountId).catch(
        () => undefined,
      );
    },
  });
}

export async function applyProjectMemoryCacheChange(
  queryClient: QueryClient,
  scope: ProjectClientScope,
  change: ProjectMemoryCacheChange,
): Promise<void> {
  await applyProjectMemoryCacheChanges(queryClient, scope, [change]);
}

export async function applyProjectMemoryCacheChanges(
  queryClient: QueryClient,
  scope: ProjectClientScope,
  changes: readonly ProjectMemoryCacheChange[],
): Promise<void> {
  const parsedScope = projectClientScopeSchema.parse(scope);
  const root = memoryQueryKey(parsedScope);
  const uniqueChanges = new Set(changes);
  if (uniqueChanges.has("reset")) {
    await queryClient.cancelQueries({ queryKey: root });
    queryClient.removeQueries({ queryKey: root });
    return;
  }

  const keys = new Map<string, readonly unknown[]>();
  const addKey = (...segments: readonly unknown[]) => {
    const key = memoryQueryKey(parsedScope, ...segments);
    keys.set(JSON.stringify(key), key);
  };
  for (const change of uniqueChanges) {
    if (change === "pending") {
      addKey("document");
      addKey("pending");
    } else if (change === "document") {
      addKey("document");
      addKey("versions");
      addKey("version");
    } else if (change === "versions") {
      addKey("versions");
      addKey("version");
    } else if (change === "episodes") {
      addKey("episodes");
    }
  }

  await Promise.all(
    [...keys.values()].map((queryKey) =>
      queryClient.invalidateQueries({ queryKey }),
    ),
  );
}

export function broadcastProjectMemoryCacheHint(
  scope: ProjectClientScope,
  change: ProjectMemoryCacheChange,
  channelFactory: ProjectCacheHintChannelFactory = defaultChannelFactory,
): ProjectCacheHint | null {
  const subscription = createProjectMemoryCacheHintSubscription({
    scope,
    onHint: () => undefined,
    channelFactory,
  });
  try {
    const hint = subscription.publish(change);
    queueMicrotask(() => subscription.dispose());
    return hint;
  } catch {
    subscription.dispose();
    return null;
  }
}

export function broadcastAccountAgentRuntimeCacheHint(
  accountId: string,
  channelFactory: ProjectCacheHintChannelFactory = defaultChannelFactory,
): AccountAgentRuntimeCacheHint | null {
  const subscription = createAccountAgentRuntimeCacheHintSubscription({
    accountId,
    onHint: () => undefined,
    channelFactory,
  });
  try {
    const hint = subscription.publish();
    queueMicrotask(() => subscription.dispose());
    return hint;
  } catch {
    subscription.dispose();
    return null;
  }
}

export async function commitAccountAgentRuntimeCacheHint(
  queryClient: QueryClient,
  accountId: string,
): Promise<void> {
  try {
    await applyAccountAgentRuntimeCacheHint(queryClient, accountId);
  } finally {
    broadcastAccountAgentRuntimeCacheHint(accountId);
  }
}

export async function commitProjectMemoryCacheChange(
  queryClient: QueryClient,
  scope: ProjectClientScope,
  change: ProjectMemoryCacheChange,
): Promise<void> {
  try {
    await applyProjectMemoryCacheChange(queryClient, scope, change);
  } finally {
    // Broadcast is a best-effort cache hint after the server-authoritative
    // local refresh has been scheduled. A local refetch failure must not leave
    // healthy sibling tabs stale.
    broadcastProjectMemoryCacheHint(scope, change);
  }
}

export async function commitProjectMemoryCacheChanges(
  queryClient: QueryClient,
  scope: ProjectClientScope,
  changes: readonly ProjectMemoryCacheChange[],
): Promise<void> {
  const uniqueChanges = [...new Set(changes)];
  try {
    await applyProjectMemoryCacheChanges(queryClient, scope, uniqueChanges);
  } finally {
    for (const change of uniqueChanges) {
      broadcastProjectMemoryCacheHint(scope, change);
    }
  }
}

export function collectAccountProjectCacheScopes(
  queryClient: QueryClient,
  accountId: string,
): ProjectClientScope[] {
  const scopes = new Map<string, ProjectClientScope>();
  const add = (projectId: unknown) => {
    const parsed = projectClientScopeSchema.safeParse({ accountId, projectId });
    if (parsed.success) scopes.set(parsed.data.projectId, parsed.data);
  };

  for (const query of queryClient.getQueryCache().getAll()) {
    const key = query.queryKey;
    if (key[0] !== "account" || key[1] !== accountId) continue;
    if (key[2] === "project" && key[4] === "private-work") {
      add(key[3]);
      continue;
    }
    if (key[2] !== "projects") continue;
    const page = projectPageSchema.safeParse(query.state.data);
    if (page.success) {
      for (const project of page.data.items) add(project.id);
      continue;
    }
    const project = projectSchema.safeParse(query.state.data);
    if (project.success) add(project.data.id);
  }
  return [...scopes.values()];
}

export function useProjectMemoryCacheHintListener(
  scope: ProjectClientScope,
): void {
  const queryClient = useQueryClient();
  const { accountId, projectId } = projectClientScopeSchema.parse(scope);

  useEffect(() => {
    const currentScope = { accountId, projectId };
    const pending = new Set<ProjectMemoryCacheChange>();
    let flushQueued = false;
    let disposed = false;

    const flush = () => {
      flushQueued = false;
      if (disposed) return;
      const changes = pending.has("reset") ? ["reset" as const] : [...pending];
      pending.clear();
      void applyProjectMemoryCacheChanges(
        queryClient,
        currentScope,
        changes,
      ).catch(() => undefined);
    };

    const subscription = createProjectMemoryCacheHintSubscription({
      scope: currentScope,
      onHint: (hint) => {
        pending.add(hint.change);
        if (flushQueued) return;
        flushQueued = true;
        queueMicrotask(flush);
      },
    });
    return () => {
      disposed = true;
      pending.clear();
      subscription.dispose();
    };
  }, [accountId, projectId, queryClient]);

  useEffect(() => {
    const listener = createAccountAgentRuntimeCacheHintListener({
      queryClient,
      accountId,
    });
    return () => listener.dispose();
  }, [accountId, queryClient]);
}

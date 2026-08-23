import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryObserver } from "@tanstack/react-query";

import {
  accountAgentRuntimeCacheHintSchema,
  applyAccountAgentRuntimeCacheHint,
  applyProjectMemoryCacheChange,
  collectAccountProjectCacheScopes,
  createAccountAgentRuntimeCacheHintSubscription,
  createProjectMemoryCacheHintSubscription,
  PROJECT_CACHE_HINT_CHANNEL,
  projectCacheHintSchema,
  type ProjectCacheHintChannel,
} from "@/core/private-work/memory-freshness";
import { privateWorkQueryKey } from "@/core/private-work/query-keys";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const OTHER_ACCOUNT_ID = "11111111-1111-4111-8111-111111111112";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const OTHER_PROJECT_ID = "22222222-2222-4222-8222-222222222223";
const TAB_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_TAB_ID = "33333333-3333-4333-8333-333333333334";
const EVENT_ID = "44444444-4444-4444-8444-444444444444";
const SECOND_EVENT_ID = "44444444-4444-4444-8444-444444444445";

const scope = { accountId: ACCOUNT_ID, projectId: PROJECT_ID };

class FakeChannel implements ProjectCacheHintChannel {
  readonly listeners = new Set<(event: { data: unknown }) => void>();
  readonly messages: unknown[] = [];
  closed = false;

  postMessage(message: unknown): void {
    this.messages.push(message);
  }

  addEventListener(
    _type: "message",
    listener: (event: { data: unknown }) => void,
  ): void {
    this.listeners.add(listener);
  }

  removeEventListener(
    _type: "message",
    listener: (event: { data: unknown }) => void,
  ): void {
    this.listeners.delete(listener);
  }

  close(): void {
    this.closed = true;
  }

  emit(data: unknown): void {
    for (const listener of this.listeners) listener({ data });
  }
}

function hint(overrides: Record<string, unknown> = {}) {
  return {
    schemaVersion: 1,
    eventId: EVENT_ID,
    sourceTabId: OTHER_TAB_ID,
    accountId: ACCOUNT_ID,
    projectId: PROJECT_ID,
    domain: "memory",
    change: "pending",
    ...overrides,
  };
}

function agentRuntimeHint(overrides: Record<string, unknown> = {}) {
  return {
    schemaVersion: 1,
    eventId: EVENT_ID,
    sourceTabId: OTHER_TAB_ID,
    accountId: ACCOUNT_ID,
    domain: "agent_runtime",
    change: "context_usage",
    ...overrides,
  };
}

describe("project Memory freshness hints", () => {
  test("accepts only the strict content-free scoped contract", () => {
    expect(projectCacheHintSchema.safeParse(hint()).success).toBe(true);
    expect(
      projectCacheHintSchema.safeParse(
        hint({ taggedText: "private Memory must not be broadcast" }),
      ).success,
    ).toBe(false);
    expect(
      projectCacheHintSchema.safeParse(hint({ schemaVersion: 2 })).success,
    ).toBe(false);
    expect(
      projectCacheHintSchema.safeParse(hint({ projectId: "not-a-uuid" }))
        .success,
    ).toBe(false);
  });

  test("publishes the fixed schema and receives only new hints for its exact scope", () => {
    const channel = new FakeChannel();
    const onHint = rs.fn();
    const subscription = createProjectMemoryCacheHintSubscription({
      scope,
      onHint,
      channelFactory: (name) => {
        expect(name).toBe(PROJECT_CACHE_HINT_CHANNEL);
        return channel;
      },
      tabId: TAB_ID,
      eventIdFactory: () => EVENT_ID,
    });

    expect(subscription.publish("document")).toEqual({
      schemaVersion: 1,
      eventId: EVENT_ID,
      sourceTabId: TAB_ID,
      accountId: ACCOUNT_ID,
      projectId: PROJECT_ID,
      domain: "memory",
      change: "document",
    });
    expect(channel.messages).toHaveLength(1);

    channel.emit(hint({ sourceTabId: TAB_ID }));
    channel.emit(hint({ accountId: OTHER_ACCOUNT_ID }));
    channel.emit(hint({ projectId: OTHER_PROJECT_ID }));
    channel.emit(hint({ content: "must be rejected" }));
    channel.emit(hint());
    channel.emit(hint());
    channel.emit(hint({ eventId: SECOND_EVENT_ID, change: "episodes" }));

    expect(onHint).toHaveBeenCalledTimes(2);
    expect(onHint.mock.calls.map(([value]) => value.change)).toEqual([
      "pending",
      "episodes",
    ]);

    subscription.dispose();
    channel.emit(hint({ eventId: "44444444-4444-4444-8444-444444444446" }));
    expect(onHint).toHaveBeenCalledTimes(2);
    expect(channel.closed).toBe(true);
    expect(channel.listeners.size).toBe(0);
  });

  test("invalidates only the mapped scoped roots and removes the root on reset", async () => {
    const queryClient = new QueryClient();
    const documentKey = privateWorkQueryKey(scope, "memory", "document");
    const pendingKey = privateWorkQueryKey(scope, "memory", "pending");
    const versionsKey = privateWorkQueryKey(scope, "memory", "versions", 51, 0);
    const threadKey = privateWorkQueryKey(scope, "thread", "thread-1");
    queryClient.setQueryData(documentKey, { version: 1 });
    queryClient.setQueryData(pendingKey, { items: [] });
    queryClient.setQueryData(versionsKey, { items: [] });
    queryClient.setQueryData(threadKey, { status: "idle" });

    await applyProjectMemoryCacheChange(queryClient, scope, "pending");

    expect(queryClient.getQueryState(documentKey)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(pendingKey)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(versionsKey)?.isInvalidated).toBe(false);
    expect(queryClient.getQueryState(threadKey)?.isInvalidated).toBe(false);

    await applyProjectMemoryCacheChange(queryClient, scope, "reset");

    expect(queryClient.getQueryData(documentKey)).toBeUndefined();
    expect(queryClient.getQueryData(pendingKey)).toBeUndefined();
    expect(queryClient.getQueryData(versionsKey)).toBeUndefined();
    expect(queryClient.getQueryData(threadKey)).toEqual({ status: "idle" });
  });

  test("discovers only cached project scopes for the exact account", () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(
      privateWorkQueryKey(scope, "thread", "thread-1"),
      {},
    );
    queryClient.setQueryData(
      privateWorkQueryKey(
        { accountId: ACCOUNT_ID, projectId: OTHER_PROJECT_ID },
        "memory",
        "document",
      ),
      {},
    );
    queryClient.setQueryData(
      privateWorkQueryKey(
        { accountId: OTHER_ACCOUNT_ID, projectId: OTHER_PROJECT_ID },
        "memory",
        "document",
      ),
      {},
    );

    expect(collectAccountProjectCacheScopes(queryClient, ACCOUNT_ID)).toEqual([
      scope,
      { accountId: ACCOUNT_ID, projectId: OTHER_PROJECT_ID },
    ]);
  });
});

describe("account agent_runtime freshness hints", () => {
  test("publishes a content-free account hint and receives each sibling event once", () => {
    const channel = new FakeChannel();
    const onHint = rs.fn();
    const subscription = createAccountAgentRuntimeCacheHintSubscription({
      accountId: ACCOUNT_ID,
      onHint,
      channelFactory: (name) => {
        expect(name).toBe(PROJECT_CACHE_HINT_CHANNEL);
        return channel;
      },
      tabId: TAB_ID,
      eventIdFactory: () => EVENT_ID,
    });

    expect(subscription.publish()).toEqual({
      schemaVersion: 1,
      eventId: EVENT_ID,
      sourceTabId: TAB_ID,
      accountId: ACCOUNT_ID,
      domain: "agent_runtime",
      change: "context_usage",
    });
    expect(
      accountAgentRuntimeCacheHintSchema.safeParse({
        ...agentRuntimeHint(),
        policy: { summarization: "must not be broadcast" },
      }).success,
    ).toBe(false);

    channel.emit(agentRuntimeHint({ sourceTabId: TAB_ID }));
    channel.emit(agentRuntimeHint({ accountId: OTHER_ACCOUNT_ID }));
    channel.emit(agentRuntimeHint());
    channel.emit(agentRuntimeHint());
    channel.emit(agentRuntimeHint({ eventId: SECOND_EVENT_ID }));

    expect(onHint).toHaveBeenCalledTimes(2);

    subscription.dispose();
  });

  test("refetches active context usage and marks inactive readings stale for the exact account", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const activeKey = privateWorkQueryKey(
      scope,
      "thread-context-usage",
      "thread-active",
    );
    const inactiveKey = privateWorkQueryKey(
      { accountId: ACCOUNT_ID, projectId: OTHER_PROJECT_ID },
      "thread-context-usage",
      "thread-inactive",
    );
    const otherAccountKey = privateWorkQueryKey(
      { accountId: OTHER_ACCOUNT_ID, projectId: OTHER_PROJECT_ID },
      "thread-context-usage",
      "thread-other-account",
    );
    queryClient.setQueryData(activeKey, { estimated_tokens: 10 });
    queryClient.setQueryData(inactiveKey, { estimated_tokens: 20 });
    queryClient.setQueryData(otherAccountKey, { estimated_tokens: 30 });
    const refetch = rs.fn(async () => ({ estimated_tokens: 11 }));
    const observer = new QueryObserver(queryClient, {
      queryKey: activeKey,
      queryFn: refetch,
      staleTime: Number.POSITIVE_INFINITY,
    });
    const unsubscribe = observer.subscribe(() => undefined);

    await applyAccountAgentRuntimeCacheHint(queryClient, ACCOUNT_ID);

    expect(refetch).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryState(inactiveKey)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(otherAccountKey)?.isInvalidated).toBe(
      false,
    );

    unsubscribe();
    queryClient.clear();
  });

  test("cancels a pending first load before refetching so its late result cannot overwrite the new policy", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const queryKey = privateWorkQueryKey(
      scope,
      "thread-context-usage",
      "thread-pending",
    );
    let resolveFirst!: (value: { estimated_tokens: number }) => void;
    let firstSignal: AbortSignal | undefined;
    let callCount = 0;
    const queryFn = rs.fn(({ signal }: { signal: AbortSignal }) => {
      callCount += 1;
      if (callCount === 1) {
        firstSignal = signal;
        return new Promise<{ estimated_tokens: number }>((resolve) => {
          resolveFirst = resolve;
        });
      }
      return Promise.resolve({ estimated_tokens: 20 });
    });
    const observer = new QueryObserver(queryClient, {
      queryKey,
      queryFn,
      staleTime: Number.POSITIVE_INFINITY,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    await Promise.resolve();

    const refresh = applyAccountAgentRuntimeCacheHint(queryClient, ACCOUNT_ID);
    await Promise.resolve();
    resolveFirst({ estimated_tokens: 10 });
    await refresh;
    await Promise.resolve();

    expect(firstSignal?.aborted).toBe(true);
    expect(queryFn).toHaveBeenCalledTimes(2);
    expect(queryClient.getQueryData(queryKey)).toEqual({
      estimated_tokens: 20,
    });

    unsubscribe();
    queryClient.clear();
  });
});

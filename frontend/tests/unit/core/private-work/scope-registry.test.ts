import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import { agentBuilderRootKey } from "@/core/agent-builder/query-keys";
import { knowledgeRoot } from "@/core/knowledge/query-keys";
import { privateWorkRoot } from "@/core/private-work/query-keys";
import {
  clearProjectReconnectStorage,
  createPrivateWorkScopeRegistry,
  projectReconnectStorage,
  transitionPrivateWorkScope,
} from "@/core/private-work/scope-registry";
import { automationRoot } from "@/core/project-automations/query-keys";
import { governanceRoot } from "@/core/project-governance/query-keys";
import { projectSharedAssetRoot } from "@/core/shared-assets/query-keys";
import { skillBuilderRootKey } from "@/core/skill-builder/query-keys";

const A_P1 = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const A_P2 = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
};

function makeSessionStorage() {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    getItem: rs.fn((key: string) => values.get(key) ?? null),
    key: rs.fn((index: number) => [...values.keys()][index] ?? null),
    removeItem: rs.fn((key: string) => values.delete(key)),
    setItem: rs.fn((key: string, value: string) => values.set(key, value)),
  };
}

describe("project private-work scope registry", () => {
  test("owns project event streams and rejects late frames after disposal", async () => {
    class FakeEventSource {
      static instances: FakeEventSource[] = [];
      readonly listeners = new Map<string, Set<EventListener>>();
      closed = false;

      constructor(
        readonly url: string,
        readonly options: EventSourceInit,
      ) {
        FakeEventSource.instances.push(this);
      }

      addEventListener(name: string, listener: EventListener) {
        const listeners = this.listeners.get(name) ?? new Set<EventListener>();
        listeners.add(listener);
        this.listeners.set(name, listeners);
      }

      removeEventListener(name: string, listener: EventListener) {
        this.listeners.get(name)?.delete(listener);
      }

      close() {
        this.closed = true;
      }

      emit(name: string, data: string) {
        for (const listener of this.listeners.get(name) ?? []) {
          listener({ data } as MessageEvent<string>);
        }
      }
    }

    rs.stubGlobal("EventSource", FakeEventSource);
    const registry = createPrivateWorkScopeRegistry();
    const queryClient = new QueryClient();
    const access = registry.acquire(A_P1);
    const received: string[] = [];
    access.subscribeEventStream?.(
      "/api/projects/p1/agent-builder/activities/stream",
      "activity",
      (data) => received.push(data),
    );
    const source = FakeEventSource.instances[0];
    expect(source?.options.withCredentials).toBe(true);
    source?.emit("activity", "first");

    await transitionPrivateWorkScope(registry, queryClient, A_P1, A_P2);
    source?.emit("activity", "late");

    expect(received).toEqual(["first"]);
    expect(source?.closed).toBe(true);
    rs.unstubAllGlobals();
  });

  test("aborts and removes in-flight governance queries and mutations on project switch", async () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(A_P1);
    const queryClient = new QueryClient();
    const root = governanceRoot(A_P1);
    let queryStarted!: () => void;
    const queryReady = new Promise<void>((resolve) => {
      queryStarted = resolve;
    });
    let queryAborted = false;
    const pendingQuery = queryClient
      .fetchQuery({
        queryKey: [...root, "usage"],
        queryFn: ({ signal }) =>
          new Promise<string>((resolve) => {
            signal.addEventListener("abort", () => {
              queryAborted = true;
              resolve("late-query");
            });
            queryStarted();
          }),
      })
      .catch(() => undefined);

    let mutationStarted!: () => void;
    const mutationReady = new Promise<void>((resolve) => {
      mutationStarted = resolve;
    });
    let mutationAborted = false;
    let resolveMutation!: (value: string) => void;
    const deferredMutation = new Promise<string>((resolve) => {
      resolveMutation = resolve;
    });
    const mutation = queryClient.getMutationCache().build(queryClient, {
      mutationKey: [...root, "usage", "mutation", "limits"],
      mutationFn: () =>
        access.runAbortable!(async (signal) => {
          signal.addEventListener("abort", () => {
            mutationAborted = true;
          });
          mutationStarted();
          return deferredMutation;
        }),
    });
    const pendingMutation = mutation.execute(undefined);
    await Promise.all([queryReady, mutationReady]);

    await transitionPrivateWorkScope(registry, queryClient, A_P1, A_P2);

    expect(queryAborted).toBe(true);
    expect(mutationAborted).toBe(true);
    expect(queryClient.getQueryData([...root, "usage"])).toBeUndefined();
    expect(
      queryClient.getMutationCache().findAll({ mutationKey: root }),
    ).toHaveLength(0);

    resolveMutation("late-mutation");
    await Promise.all([pendingQuery, pendingMutation]);
  });

  test("drops a deferred automation mutation without recreating its old query", async () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(A_P1);
    const queryClient = new QueryClient();
    let resolveMutation!: (value: string) => void;
    const deferred = new Promise<string>((resolve) => {
      resolveMutation = resolve;
    });
    let markStarted!: () => void;
    const started = new Promise<void>((resolve) => {
      markStarted = resolve;
    });
    let aborted = false;
    const queryKey = [...automationRoot(A_P1), "list", 50, 0];
    const mutation = queryClient.getMutationCache().build(queryClient, {
      mutationKey: [...automationRoot(A_P1), "mutation", "trigger"],
      mutationFn: (_variables: { expectedVersion: number }) =>
        access.runAbortable!(async (signal) => {
          signal.addEventListener("abort", () => {
            aborted = true;
          });
          markStarted();
          return deferred;
        }),
      onSuccess: (value) => {
        if (access.isActive?.() ?? true) {
          queryClient.setQueryData(queryKey, value);
        }
      },
    });
    const pending = mutation.execute({ expectedVersion: 3 });
    await started;
    expect(mutation.state.variables).toEqual({ expectedVersion: 3 });

    await transitionPrivateWorkScope(registry, queryClient, A_P1, A_P2);
    expect(aborted).toBe(true);
    resolveMutation("late-old-project");
    await pending;

    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);
    expect(queryClient.getQueryData(queryKey)).toBeUndefined();
  });

  test("cancels in-flight work before removing scope state", async () => {
    const order: string[] = [];
    const registry = createPrivateWorkScopeRegistry();
    registry.acquire(A_P1);
    const originalDispose = registry.dispose.bind(registry);
    const dispose = rs
      .spyOn(registry, "dispose")
      .mockImplementation((scope) => {
        order.push("dispose");
        return originalDispose(scope);
      });
    const queryClient = new QueryClient();
    queryClient.setQueryData([...privateWorkRoot(A_P1), "threads"], "old");
    queryClient.setQueryData(
      [...skillBuilderRootKey(A_P1.accountId, A_P1.projectId), "sessions"],
      "old",
    );
    queryClient.setQueryData([...automationRoot(A_P1), "list", 50, 0], "old");
    queryClient.setQueryData(
      [...projectSharedAssetRoot(A_P1), "skills"],
      "old",
    );
    queryClient.setQueryData([...knowledgeRoot(A_P1), "bases", "list"], "old");
    rs.spyOn(queryClient, "cancelQueries").mockImplementation(async () => {
      order.push("cancel");
    });

    await transitionPrivateWorkScope(registry, queryClient, A_P1, A_P2);

    expect(order).toEqual([
      "cancel",
      "cancel",
      "cancel",
      "cancel",
      "cancel",
      "cancel",
      "cancel",
      "dispose",
    ]);
    expect(dispose).toHaveBeenCalledWith(A_P1);
    expect(registry.has(A_P1)).toBe(false);
    expect(
      queryClient.getQueryData([...privateWorkRoot(A_P1), "threads"]),
    ).toBeUndefined();
    expect(
      queryClient.getQueryData([
        ...skillBuilderRootKey(A_P1.accountId, A_P1.projectId),
        "sessions",
      ]),
    ).toBeUndefined();
    expect(
      queryClient.getQueryData([...automationRoot(A_P1), "list", 50, 0]),
    ).toBeUndefined();
    expect(
      queryClient.getQueryData([...projectSharedAssetRoot(A_P1), "skills"]),
    ).toBeUndefined();
    expect(
      queryClient.getQueryData([...knowledgeRoot(A_P1), "bases", "list"]),
    ).toBeUndefined();
  });

  test("aborts and removes project shared-asset mutations on scope exit", async () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(A_P1);
    const queryClient = new QueryClient();
    const root = projectSharedAssetRoot(A_P1);
    let mutationStarted!: () => void;
    const started = new Promise<void>((resolve) => {
      mutationStarted = resolve;
    });
    let mutationAborted = false;
    let resolveMutation!: (value: string) => void;
    const deferred = new Promise<string>((resolve) => {
      resolveMutation = resolve;
    });
    const mutation = queryClient.getMutationCache().build(queryClient, {
      mutationKey: [...root, "skills", "mutation", "create-version"],
      mutationFn: () =>
        access.runAbortable!(async (signal) => {
          signal.addEventListener("abort", () => {
            mutationAborted = true;
          });
          mutationStarted();
          return deferred;
        }),
    });
    const pending = mutation.execute(undefined);
    await started;

    await transitionPrivateWorkScope(registry, queryClient, A_P1, A_P2);

    expect(mutationAborted).toBe(true);
    expect(
      queryClient.getMutationCache().findAll({ mutationKey: root }),
    ).toHaveLength(0);

    resolveMutation("late-shared-asset");
    await pending;
  });

  test("aborts and removes Agent Builder queries and mutations on scope exit", async () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(A_P1);
    const queryClient = new QueryClient();
    const root = agentBuilderRootKey(A_P1.accountId, A_P1.projectId);
    let queryStarted!: () => void;
    const queryReady = new Promise<void>((resolve) => {
      queryStarted = resolve;
    });
    let queryAborted = false;
    const pendingQuery = queryClient
      .fetchQuery({
        queryKey: [...root, "sessions"],
        queryFn: ({ signal }) =>
          new Promise<string>((resolve) => {
            signal.addEventListener("abort", () => {
              queryAborted = true;
              resolve("late-query");
            });
            queryStarted();
          }),
      })
      .catch(() => undefined);

    let mutationStarted!: () => void;
    const mutationReady = new Promise<void>((resolve) => {
      mutationStarted = resolve;
    });
    let mutationAborted = false;
    let resolveMutation!: (value: string) => void;
    const deferredMutation = new Promise<string>((resolve) => {
      resolveMutation = resolve;
    });
    const mutation = queryClient.getMutationCache().build(queryClient, {
      mutationKey: [...root, "mutation", "submit-turn"],
      mutationFn: () =>
        access.runAbortable!(async (signal) => {
          signal.addEventListener("abort", () => {
            mutationAborted = true;
          });
          mutationStarted();
          return deferredMutation;
        }),
    });
    const pendingMutation = mutation.execute(undefined);
    await Promise.all([queryReady, mutationReady]);

    await transitionPrivateWorkScope(registry, queryClient, A_P1, A_P2);

    expect(queryAborted).toBe(true);
    expect(mutationAborted).toBe(true);
    expect(queryClient.getQueryData([...root, "sessions"])).toBeUndefined();
    expect(
      queryClient.getMutationCache().findAll({ mutationKey: root }),
    ).toHaveLength(0);

    resolveMutation("late-mutation");
    await Promise.all([pendingQuery, pendingMutation]);
  });

  test("aborts and removes Skill Builder queries and mutations on scope exit", async () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(A_P1);
    const queryClient = new QueryClient();
    const root = skillBuilderRootKey(A_P1.accountId, A_P1.projectId);
    let queryStarted!: () => void;
    const queryReady = new Promise<void>((resolve) => {
      queryStarted = resolve;
    });
    let queryAborted = false;
    const pendingQuery = queryClient
      .fetchQuery({
        queryKey: [...root, "sessions"],
        queryFn: ({ signal }) =>
          new Promise<string>((resolve) => {
            signal.addEventListener("abort", () => {
              queryAborted = true;
              resolve("late-query");
            });
            queryStarted();
          }),
      })
      .catch(() => undefined);

    let mutationStarted!: () => void;
    const mutationReady = new Promise<void>((resolve) => {
      mutationStarted = resolve;
    });
    let mutationAborted = false;
    let resolveMutation!: (value: string) => void;
    const deferredMutation = new Promise<string>((resolve) => {
      resolveMutation = resolve;
    });
    const mutation = queryClient.getMutationCache().build(queryClient, {
      mutationKey: [...root, "mutation", "draft-turn"],
      mutationFn: () =>
        access.runAbortable!(async (signal) => {
          signal.addEventListener("abort", () => {
            mutationAborted = true;
          });
          mutationStarted();
          return deferredMutation;
        }),
    });
    const pendingMutation = mutation.execute(undefined);
    await Promise.all([queryReady, mutationReady]);

    await transitionPrivateWorkScope(registry, queryClient, A_P1, A_P2);

    expect(queryAborted).toBe(true);
    expect(mutationAborted).toBe(true);
    expect(queryClient.getQueryData([...root, "sessions"])).toBeUndefined();
    expect(
      queryClient.getMutationCache().findAll({ mutationKey: root }),
    ).toHaveLength(0);

    resolveMutation("late-mutation");
    await Promise.all([pendingQuery, pendingMutation]);
  });

  test("isolates reconnect metadata by account and project", () => {
    const storage = makeSessionStorage();
    const p1 = projectReconnectStorage(A_P1, storage);
    const p2 = projectReconnectStorage(A_P2, storage);

    p1.setItem("lg:stream:thread-1", "run-p1");
    p2.setItem("lg:stream:thread-1", "run-p2");

    expect(p1.getItem("lg:stream:thread-1")).toBe("run-p1");
    expect(p2.getItem("lg:stream:thread-1")).toBe("run-p2");
    expect(storage.setItem.mock.calls[0]?.[0]).not.toBe(
      storage.setItem.mock.calls[1]?.[0],
    );
  });

  test("detaches a project scope without discarding its resumable run id", () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(A_P1);
    const reconnect = (
      access.reconnectOnMount as () => ReturnType<
        typeof projectReconnectStorage
      >
    )();
    reconnect.setItem("lg:stream:thread-live", "run-live");

    registry.dispose(A_P1);
    expect(access.isActive?.()).toBe(false);
    const resumed = registry.acquire(A_P1);
    expect(
      (
        resumed.reconnectOnMount as () => ReturnType<
          typeof projectReconnectStorage
        >
      )().getItem("lg:stream:thread-live"),
    ).toBe("run-live");

    clearProjectReconnectStorage(A_P1);
    registry.dispose(A_P1);
    rs.unstubAllGlobals();
  });
});

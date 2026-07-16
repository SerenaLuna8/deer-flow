import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import { privateWorkRoot } from "@/core/private-work/query-keys";
import {
  createPrivateWorkScopeRegistry,
  projectReconnectStorage,
  transitionPrivateWorkScope,
} from "@/core/private-work/scope-registry";
import { automationRoot } from "@/core/project-automations/query-keys";

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
    getItem: rs.fn((key: string) => values.get(key) ?? null),
    removeItem: rs.fn((key: string) => values.delete(key)),
    setItem: rs.fn((key: string, value: string) => values.set(key, value)),
  };
}

describe("project private-work scope registry", () => {
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
    queryClient.setQueryData([...automationRoot(A_P1), "list", 50, 0], "old");
    rs.spyOn(queryClient, "cancelQueries").mockImplementation(async () => {
      order.push("cancel");
    });

    await transitionPrivateWorkScope(registry, queryClient, A_P1, A_P2);

    expect(order).toEqual(["cancel", "cancel", "dispose"]);
    expect(dispose).toHaveBeenCalledWith(A_P1);
    expect(registry.has(A_P1)).toBe(false);
    expect(
      queryClient.getQueryData([...privateWorkRoot(A_P1), "threads"]),
    ).toBeUndefined();
    expect(
      queryClient.getQueryData([...automationRoot(A_P1), "list", 50, 0]),
    ).toBeUndefined();
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
});

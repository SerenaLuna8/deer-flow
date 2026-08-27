import { describe, expect, test, rs } from "@rstest/core";

import { createAuthIdentityCoordinator } from "@/core/auth/identity-coordinator";

describe("auth identity coordinator", () => {
  test("a new refresh aborts refresh1 and owns loading until refresh2 finishes", () => {
    const loading: boolean[] = [];
    const coordinator = createAuthIdentityCoordinator((value) =>
      loading.push(value),
    );
    const refresh1 = coordinator.startRefresh();
    const refresh2 = coordinator.startRefresh();

    expect(refresh1.signal.aborted).toBe(true);
    expect(coordinator.isCurrent(refresh1)).toBe(false);
    expect(coordinator.isCurrent(refresh2)).toBe(true);
    expect(coordinator.finishRefresh(refresh1)).toBe(false);
    expect(loading.at(-1)).toBe(true);
    expect(coordinator.finishRefresh(refresh2)).toBe(true);
    expect(loading.at(-1)).toBe(false);
  });

  test("logout aborts refresh and late success cannot commit a user", async () => {
    const coordinator = createAuthIdentityCoordinator(() => undefined);
    const refresh = coordinator.startRefresh();
    const commit = rs.fn();
    coordinator.beginIdentityChange();

    expect(refresh.signal.aborted).toBe(true);
    await expect(
      coordinator.commitAtGeneration(
        refresh.generation,
        async () => undefined,
        commit,
      ),
    ).resolves.toBe(false);
    expect(commit).not.toHaveBeenCalled();
  });

  test("an external apply during transition makes the old refresh drop its commit", async () => {
    const coordinator = createAuthIdentityCoordinator(() => undefined);
    const refresh = coordinator.startRefresh();
    let releaseTransition: (() => void) | undefined;
    const transition = new Promise<void>((resolve) => {
      releaseTransition = resolve;
    });
    const commit = rs.fn();
    const pending = coordinator.commitAtGeneration(
      refresh.generation,
      () => transition,
      commit,
    );
    await Promise.resolve();

    coordinator.beginIdentityChange();
    releaseTransition?.();
    await expect(pending).resolves.toBe(false);
    expect(commit).not.toHaveBeenCalled();
  });

  test("old 401 and network error decisions cannot redirect, log, or clear identity", async () => {
    const coordinator = createAuthIdentityCoordinator(() => undefined);
    const stale = coordinator.startRefresh();
    coordinator.startRefresh();
    const redirect = rs.fn();
    const log = rs.fn();
    const clearIdentity = rs.fn();

    for (const sideEffect of [redirect, log, clearIdentity]) {
      await expect(
        coordinator.commitAtGeneration(
          stale.generation,
          async () => undefined,
          sideEffect,
        ),
      ).resolves.toBe(false);
    }
    expect(redirect).not.toHaveBeenCalled();
    expect(log).not.toHaveBeenCalled();
    expect(clearIdentity).not.toHaveBeenCalled();
  });

  test("dispose aborts the active refresh without allowing a late finish", () => {
    const loading: boolean[] = [];
    const coordinator = createAuthIdentityCoordinator((value) =>
      loading.push(value),
    );
    const refresh = coordinator.startRefresh();
    coordinator.dispose();
    expect(refresh.signal.aborted).toBe(true);
    expect(coordinator.finishRefresh(refresh)).toBe(false);
    expect(loading).toEqual([true]);
  });

  test("reactivates after Strict Effects replay without reviving the old attempt", async () => {
    const loading: boolean[] = [];
    const coordinator = createAuthIdentityCoordinator((value) =>
      loading.push(value),
    );
    const oldRefresh = coordinator.startRefresh();
    coordinator.dispose();
    coordinator.activate();
    const newRefresh = coordinator.startRefresh();
    const commit = rs.fn();

    expect(oldRefresh.signal.aborted).toBe(true);
    expect(coordinator.isCurrent(oldRefresh)).toBe(false);
    expect(coordinator.isCurrent(newRefresh)).toBe(true);
    await expect(
      coordinator.commitAtGeneration(
        oldRefresh.generation,
        async () => undefined,
        commit,
      ),
    ).resolves.toBe(false);
    await expect(
      coordinator.commitAtGeneration(
        newRefresh.generation,
        async () => undefined,
        commit,
      ),
    ).resolves.toBe(true);
    expect(commit).toHaveBeenCalledTimes(1);
    expect(coordinator.finishRefresh(newRefresh)).toBe(true);
    expect(loading.at(-1)).toBe(false);
  });

  test("inactive stale callbacks cannot set loading or commit identity", async () => {
    const loading: boolean[] = [];
    const coordinator = createAuthIdentityCoordinator((value) =>
      loading.push(value),
    );
    coordinator.dispose();
    const refresh = coordinator.startRefresh();
    const generation = coordinator.beginIdentityChange();
    const commit = rs.fn();

    expect(refresh.signal.aborted).toBe(true);
    expect(coordinator.isCurrent(refresh)).toBe(false);
    await expect(
      coordinator.commitAtGeneration(generation, async () => undefined, commit),
    ).resolves.toBe(false);
    expect(commit).not.toHaveBeenCalled();
    expect(loading).toEqual([]);

    coordinator.activate();
    await expect(
      coordinator.commitAtGeneration(generation, async () => undefined, commit),
    ).resolves.toBe(false);
    expect(commit).not.toHaveBeenCalled();
    expect(loading).toEqual([false]);
  });
});

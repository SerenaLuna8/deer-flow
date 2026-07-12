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
});

import { describe, expect, rs, test } from "@rstest/core";

import {
  buildRootThreadStreamOptions,
  createDeferredThreadStreamDetach,
  isRootStreamCallback,
} from "@/core/threads/stream-events";

describe("isRootStreamCallback", () => {
  test("accepts root callbacks with absent or empty namespaces", () => {
    expect(isRootStreamCallback(undefined)).toBe(true);
    expect(isRootStreamCallback({ namespace: undefined })).toBe(true);
    expect(isRootStreamCallback({ namespace: [] })).toBe(true);
  });

  test("rejects child namespace callbacks", () => {
    expect(isRootStreamCallback({ namespace: ["task:child"] })).toBe(false);
    expect(
      isRootStreamCallback({ namespace: ["task:child", "agent:grandchild"] }),
    ).toBe(false);
  });
});

describe("buildRootThreadStreamOptions", () => {
  test("keeps submissions resumable without requesting delegated frames", () => {
    const options = buildRootThreadStreamOptions();

    expect(options).toEqual({
      streamResumable: true,
      config: { recursion_limit: 1000 },
    });
    expect("streamSubgraphs" in options).toBe(false);
  });
});

describe("createDeferredThreadStreamDetach", () => {
  test("cancels the Strict Effects cleanup when the same hook is retained", () => {
    const scheduled: Array<() => void> = [];
    const detach = createDeferredThreadStreamDetach((task) => {
      scheduled.push(task);
    });
    const disconnect = rs.fn();

    detach.defer(disconnect);
    detach.retain();
    scheduled.splice(0).forEach((task) => task());

    expect(disconnect).not.toHaveBeenCalled();
  });

  test("disconnects the local stream after a real unmount", () => {
    const scheduled: Array<() => void> = [];
    const detach = createDeferredThreadStreamDetach((task) => {
      scheduled.push(task);
    });
    const disconnect = rs.fn();

    detach.defer(disconnect);
    scheduled.splice(0).forEach((task) => task());

    expect(disconnect).toHaveBeenCalledTimes(1);
  });
});

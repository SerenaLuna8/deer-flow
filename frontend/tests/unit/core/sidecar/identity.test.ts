import { describe, expect, test } from "@rstest/core";

import {
  adoptSidecarThread,
  advanceSidecarIdentity,
  consumeSidecarQueue,
  createSidecarIdentity,
  guardSidecarClear,
  visibleSidecarThreadId,
} from "@/core/sidecar/identity";

describe("sidecar parent identity and generation", () => {
  test("drops late create and queued work after a parent switch", () => {
    const first = createSidecarIdentity("parent-1");
    const second = advanceSidecarIdentity(first, "parent-2");

    expect(adoptSidecarThread(second, first, "sidecar-old")).toBeNull();
    expect(
      consumeSidecarQueue({
        currentIdentity: second,
        queued: {
          identity: first,
          threadId: "sidecar-old",
          value: "old queued message",
        },
        sidecarThreadId: null,
        boundThreadId: null,
      }),
    ).toEqual({ action: "drop", queued: null });
  });

  test("drops late create and restore after close invalidates the generation", () => {
    const opened = createSidecarIdentity("parent-1");
    const closed = advanceSidecarIdentity(opened);

    expect(adoptSidecarThread(closed, opened, "sidecar-late")).toBeNull();
    expect(
      consumeSidecarQueue({
        currentIdentity: closed,
        queued: {
          identity: opened,
          threadId: "sidecar-late",
          value: "closed queued message",
        },
        sidecarThreadId: null,
        boundThreadId: null,
      }),
    ).toEqual({ action: "drop", queued: null });

    expect(adoptSidecarThread(closed, closed, "sidecar-fresh")).toEqual({
      identity: closed,
      threadId: "sidecar-fresh",
    });
  });

  test("an old delete completion cannot clear the new parent binding", () => {
    const oldParent = createSidecarIdentity("parent-1");
    const newParent = advanceSidecarIdentity(oldParent, "parent-2");
    const newBinding = adoptSidecarThread(newParent, newParent, "sidecar-new");

    expect(guardSidecarClear(newParent, oldParent)).toBe(false);
    expect(visibleSidecarThreadId(newParent, newBinding)).toBe("sidecar-new");
    expect(guardSidecarClear(newParent, newParent)).toBe(true);
  });

  test("consumes current queued work only after the same thread is bound", () => {
    const identity = createSidecarIdentity("parent-1");
    const queued = {
      identity,
      threadId: "sidecar-1",
      value: "current message",
    };

    expect(
      consumeSidecarQueue({
        currentIdentity: identity,
        queued,
        sidecarThreadId: "sidecar-1",
        boundThreadId: null,
      }),
    ).toEqual({ action: "wait", queued });
    expect(
      consumeSidecarQueue({
        currentIdentity: identity,
        queued,
        sidecarThreadId: "sidecar-1",
        boundThreadId: "sidecar-1",
      }),
    ).toEqual({ action: "send", queued: null, value: "current message" });
  });
});

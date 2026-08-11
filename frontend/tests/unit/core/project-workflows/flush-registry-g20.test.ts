import { describe, expect, it, rs } from "@rstest/core";

import { createWorkflowEditorFlushRegistry } from "@/core/project-workflows/editor/flush-registry";

describe("G20 per-Workbench editor flush registry", () => {
  it("isolates instances and consumes successful pending flushes", () => {
    const first = createWorkflowEditorFlushRegistry();
    const second = createWorkflowEditorFlushRegistry();
    const flush = rs.fn();

    first.register("python:node-a", flush);

    expect(first.hasPending()).toBe(true);
    expect(second.hasPending()).toBe(false);
    first.flushAll();
    expect(flush).toHaveBeenCalledTimes(1);
    expect(first.hasPending()).toBe(false);
    expect(second.hasPending()).toBe(false);
  });

  it("makes same-key replacement generation safe for stale release callbacks", () => {
    const registry = createWorkflowEditorFlushRegistry();
    const stale = rs.fn();
    const current = rs.fn();
    const releaseStale = registry.register("python:node-a", stale);
    const releaseCurrent = registry.register("python:node-a", current);

    releaseStale();
    registry.flushAll();

    expect(stale).not.toHaveBeenCalled();
    expect(current).toHaveBeenCalledTimes(1);
    expect(registry.hasPending()).toBe(false);

    registry.register("python:node-a", current);
    releaseCurrent();
    expect(registry.hasPending()).toBe(true);
  });

  it("runs a deterministic snapshot, retains failures, and reports them after later flushes run", () => {
    const registry = createWorkflowEditorFlushRegistry();
    const order: string[] = [];
    const betaError = new Error("beta failed");
    const gammaError = new Error("gamma failed");

    registry.register("gamma", () => {
      order.push("gamma");
      throw gammaError;
    });
    registry.register("alpha", () => {
      order.push("alpha");
    });
    registry.register("beta", () => {
      order.push("beta");
      throw betaError;
    });

    let failure: unknown;
    try {
      registry.flushAll();
    } catch (error) {
      failure = error;
    }

    expect(order).toEqual(["alpha", "beta", "gamma"]);
    expect(failure).toBeInstanceOf(AggregateError);
    expect((failure as AggregateError).errors).toEqual([betaError, gammaError]);
    expect(registry.hasPending()).toBe(true);

    const beta = rs.fn();
    const gamma = rs.fn();
    registry.register("beta", beta);
    registry.register("gamma", gamma);
    registry.flushAll();
    expect(beta).toHaveBeenCalledTimes(1);
    expect(gamma).toHaveBeenCalledTimes(1);
    expect(registry.hasPending()).toBe(false);
  });

  it("does not consume a replacement registered by the callback being flushed", () => {
    const registry = createWorkflowEditorFlushRegistry();
    const replacement = rs.fn();
    const first = rs.fn(() => {
      registry.register("python:node-a", replacement);
    });
    registry.register("python:node-a", first);

    registry.flushAll();
    expect(first).toHaveBeenCalledTimes(1);
    expect(replacement).not.toHaveBeenCalled();
    expect(registry.hasPending()).toBe(true);

    registry.flushAll();
    expect(replacement).toHaveBeenCalledTimes(1);
    expect(registry.hasPending()).toBe(false);
  });
});

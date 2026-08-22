import { beforeEach, describe, expect, rs, test } from "@rstest/core";

type TestEffect = () => void | (() => void);

const refs: Array<{ current: unknown }> = [];
const stateValues: unknown[] = [];
let refCursor = 0;
let stateCursor = 0;

rs.mock("react", () => ({
  useCallback: <T extends (...args: never[]) => unknown>(callback: T) =>
    callback,
  useEffect: (effect: TestEffect) => {
    effect();
  },
  useRef: <T>(initialValue: T) => {
    const index = refCursor;
    refCursor += 1;
    refs[index] ??= { current: initialValue };
    return refs[index] as { current: T };
  },
  useState: <T>(initialValue: T) => {
    const index = stateCursor;
    stateCursor += 1;
    if (stateValues.length <= index) stateValues[index] = initialValue;
    const setValue = (nextValue: T | ((current: T) => T)) => {
      const current = stateValues[index] as T;
      stateValues[index] =
        typeof nextValue === "function"
          ? (nextValue as (value: T) => T)(current)
          : nextValue;
    };
    return [stateValues[index] as T, setValue] as const;
  },
}));

import { useImperativeRequest } from "@/core/api/use-imperative-request";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function useTestRender<TInput, TResult>(
  request: (input: TInput) => Promise<TResult>,
) {
  refCursor = 0;
  stateCursor = 0;
  return useImperativeRequest(request);
}

beforeEach(() => {
  refs.length = 0;
  stateValues.length = 0;
  refCursor = 0;
  stateCursor = 0;
});

describe("useImperativeRequest", () => {
  test("tracks concurrent requests without retaining input in hook state", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    let call = 0;
    const request = (_input: string) =>
      call++ === 0 ? first.promise : second.promise;
    let hook = useTestRender(request);

    const firstRun = hook.execute("temporary-secret-one");
    const secondRun = hook.execute("temporary-secret-two");
    hook = useTestRender(request);
    expect(hook.isPending).toBe(true);

    first.resolve("first-result");
    await firstRun;
    hook = useTestRender(request);
    expect(hook.isPending).toBe(true);

    second.resolve("second-result");
    await secondRun;
    hook = useTestRender(request);
    expect(hook.isPending).toBe(false);
    expect(hook.error).toBeNull();
    expect(stateValues).not.toContain("temporary-secret-one");
    expect(stateValues).not.toContain("temporary-secret-two");
    expect(refs.map((ref) => ref.current)).not.toContain(
      "temporary-secret-one",
    );
    expect(refs.map((ref) => ref.current)).not.toContain(
      "temporary-secret-two",
    );
  });

  test("retains only a sanitized failure object after rejection", async () => {
    const request = async (_input: string) => {
      throw new Error("safe failure");
    };
    let hook = useTestRender(request);

    await hook.execute("temporary-secret").catch(() => undefined);
    hook = useTestRender(request);

    expect(hook.error).toBeInstanceOf(Error);
    expect((hook.error as Error).message).toBe("safe failure");
    expect(stateValues).not.toContain("temporary-secret");
    expect(refs.map((ref) => ref.current)).not.toContain("temporary-secret");
  });
});

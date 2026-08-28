import { expect, test } from "@rstest/core";

import { resolveThreadAvailability } from "@/core/threads/thread-actions";

const settled = {
  isLoading: false,
  isFetching: false,
};

test("treats the metadata hook's hidden 403 or 404 result as not found", () => {
  expect(
    resolveThreadAvailability({
      ...settled,
      data: null,
      error: null,
    }),
  ).toBe("not-found");
});

test("keeps a cached not-found result loading while metadata refetches", () => {
  expect(
    resolveThreadAvailability({
      isLoading: false,
      isFetching: true,
      data: null,
      error: null,
    }),
  ).toBe("loading");
});

test("keeps a cached Thread available while metadata refetches", () => {
  expect(
    resolveThreadAvailability({
      isLoading: false,
      isFetching: true,
      data: { thread_id: "thread-a" },
      error: null,
    }),
  ).toBe("available");
});

test("keeps metadata loading and failures distinct from not found", () => {
  expect(
    resolveThreadAvailability({
      data: undefined,
      error: null,
      isLoading: true,
      isFetching: false,
    }),
  ).toBe("loading");
  expect(
    resolveThreadAvailability({
      ...settled,
      data: undefined,
      error: new Error("Gateway unavailable"),
    }),
  ).toBe("error");
});

test("recognizes a settled Thread before dependent queries start", () => {
  expect(
    resolveThreadAvailability({
      ...settled,
      data: { thread_id: "thread-a" },
      error: null,
    }),
  ).toBe("available");
});

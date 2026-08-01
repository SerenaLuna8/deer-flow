import { afterEach, describe, expect, rs, test } from "@rstest/core";

import { AuthRequestTimeoutError, fetchAuth } from "@/core/auth/request";

describe("bounded auth requests", () => {
  afterEach(() => {
    rs.unstubAllGlobals();
  });

  test("forwards credentials and caller cancellation", async () => {
    const fetchMock = rs.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        }),
    );
    rs.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const pending = fetchAuth("/api/v1/auth/me", {
      signal: controller.signal,
    });

    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/me",
      expect.objectContaining({
        credentials: "include",
        signal: expect.any(AbortSignal),
      }),
    );
  });

  test("turns a bounded timeout into an explicit availability error", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(
        (_input: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener(
              "abort",
              () => reject(new DOMException("Aborted", "AbortError")),
              { once: true },
            );
          }),
      ),
    );

    await expect(fetchAuth("/api/v1/auth/me", {}, 1)).rejects.toBeInstanceOf(
      AuthRequestTimeoutError,
    );
  });
});

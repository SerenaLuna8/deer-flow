export const AUTH_PROBE_TIMEOUT_MS = 5_000;
export const AUTH_SUBMIT_TIMEOUT_MS = 15_000;

export class AuthRequestTimeoutError extends Error {
  constructor() {
    super("Authentication request timed out");
    this.name = "AuthRequestTimeoutError";
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException
    ? error.name === "AbortError"
    : error instanceof Error && error.name === "AbortError";
}

type RequestExecutor = (
  input: RequestInfo | string,
  init?: RequestInit,
) => Promise<Response>;

export async function fetchWithAuthTimeout(
  executor: RequestExecutor,
  input: RequestInfo | string,
  init: RequestInit = {},
  timeoutMs = AUTH_PROBE_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const externalSignal = init.signal;
  let timedOut = false;

  const abortFromCaller = () => {
    controller.abort(externalSignal?.reason);
  };
  if (externalSignal?.aborted) {
    abortFromCaller();
  } else {
    externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  const timeout = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    return await executor(input, {
      ...init,
      signal: controller.signal,
    });
  } catch (error) {
    if (timedOut) throw new AuthRequestTimeoutError();
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  }
}

/**
 * Run one auth request with a bounded lifetime.
 *
 * Auth pages use the raw gateway endpoints because a 401 is part of their
 * normal response contract. Protected application requests continue to use
 * the centralized CSRF-aware fetcher.
 */
export async function fetchAuth(
  input: RequestInfo | string,
  init: RequestInit = {},
  timeoutMs = AUTH_PROBE_TIMEOUT_MS,
): Promise<Response> {
  return fetchWithAuthTimeout(
    (requestInput, requestInit) =>
      globalThis.fetch(requestInput, {
        ...requestInit,
        credentials: requestInit?.credentials ?? "include",
      }),
    input,
    {
      ...init,
    },
    timeoutMs,
  );
}

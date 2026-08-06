"use client";

import { Client as LangGraphClient } from "@langchain/langgraph-sdk/client";

import { RUN_EXECUTION_PROFILE_CONTEXT_KEY } from "../private-work/execution-profile";

import { isStateChangingMethod, readCsrfCookie } from "./fetcher";
import { sanitizeRunStreamOptions } from "./stream-mode";

/**
 * SDK ``onRequest`` hook that mints the ``X-CSRF-Token`` header from the
 * live ``csrf_token`` cookie just before each outbound fetch.
 *
 * Reading the cookie per-request (rather than baking it into the SDK's
 * ``defaultHeaders`` at construction) handles login / logout / password
 * change cookie rotation transparently. Both the project-private SDK adapter
 * and the direct REST endpoints in ``fetcher.ts:fetchWithAuth``
 * share :func:`readCsrfCookie` and :const:`STATE_CHANGING_METHODS` so
 * the contract stays in lockstep.
 */
function injectCsrfHeader(_url: URL, init: RequestInit): RequestInit {
  if (!isStateChangingMethod(init.method ?? "GET")) {
    return init;
  }
  const token = readCsrfCookie();
  if (!token) return init;
  const headers = new Headers(init.headers);
  if (!headers.has("X-CSRF-Token")) {
    headers.set("X-CSRF-Token", token);
  }
  return { ...init, headers };
}

const PRIVATE_RUN_CREATE_PATH = /\/threads\/[^/]+\/runs(?:\/(?:stream|wait))?$/;

/**
 * The upstream LangGraph SDK serializes a fixed set of Run fields and does not
 * yet expose ActWeave's top-level ``execution_profile`` extension. Callers put
 * the profile under a reserved context key; this final request hook promotes
 * it after SDK serialization and removes the reserved key before the request
 * crosses the trust boundary.
 */
export function promotePrivateRunExecutionProfile(
  url: URL,
  init: RequestInit,
): RequestInit {
  if (
    (init.method ?? "GET").toUpperCase() !== "POST" ||
    !PRIVATE_RUN_CREATE_PATH.test(url.pathname) ||
    typeof init.body !== "string"
  ) {
    return init;
  }

  let body: unknown;
  try {
    body = JSON.parse(init.body);
  } catch {
    return init;
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return init;
  }

  const context = Reflect.get(body, "context");
  if (
    typeof context !== "object" ||
    context === null ||
    Array.isArray(context) ||
    !Object.hasOwn(context, RUN_EXECUTION_PROFILE_CONTEXT_KEY)
  ) {
    return init;
  }

  const executionProfile = Reflect.get(
    context,
    RUN_EXECUTION_PROFILE_CONTEXT_KEY,
  );
  const nextContext = { ...context };
  Reflect.deleteProperty(nextContext, RUN_EXECUTION_PROFILE_CONTEXT_KEY);

  return {
    ...init,
    body: JSON.stringify({
      ...body,
      context: nextContext,
      execution_profile: executionProfile,
    }),
  };
}

function prepareSdkRequest(url: URL, init: RequestInit): RequestInit {
  return injectCsrfHeader(url, promotePrivateRunExecutionProfile(url, init));
}

type RunMetadataStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function clearReconnectRun(
  threadId: string | null | undefined,
  runId: string,
  storageOverride?: RunMetadataStorage,
): void {
  if (typeof window === "undefined" || !threadId) return;

  const key = `lg:stream:${threadId}`;
  try {
    const storage = storageOverride ?? window.sessionStorage;
    if (storage.getItem(key) === runId) {
      storage.removeItem(key);
    }
  } catch {
    // Ignore storage access failures so reconnect cleanup never throws.
  }
}

export type ProjectPrivateClientOptions = {
  apiUrl: string;
};

export function createProjectPrivateClient({
  apiUrl,
}: ProjectPrivateClientOptions): LangGraphClient {
  const client = new LangGraphClient({
    apiUrl,
    onRequest: prepareSdkRequest,
  });

  const originalRunStream = client.runs.stream.bind(client.runs);
  client.runs.stream = ((threadId, assistantId, payload) =>
    originalRunStream(
      threadId,
      assistantId,
      sanitizeRunStreamOptions(payload),
    )) as typeof client.runs.stream;

  const originalJoinStream = client.runs.joinStream.bind(client.runs);
  client.runs.joinStream = async function* (threadId, runId, options) {
    yield* originalJoinStream(
      threadId,
      runId,
      sanitizeRunStreamOptions(options),
    );
  } as typeof client.runs.joinStream;

  return client;
}

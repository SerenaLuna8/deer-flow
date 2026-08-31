import type { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  adminModelProviderConnectionTestSchema,
  adminModelProviderDeleteSchema,
  adminModelProviderListSchema,
  adminModelProviderMutationSchema,
  adminModelRegistryAccountIdSchema,
  adminProviderModelDeleteSchema,
  adminProviderModelListSchema,
  adminProviderModelMutationSchema,
  adminProviderModelTestSchema,
  testAdminModelProviderConnectionInputSchema,
  type AdminModelProviderConnectionTestResult,
  type AdminModelProviderItem,
  type AdminProviderModelItem,
  type AdminProviderModelStatus,
  type AdminProviderModelTestResult,
  type CreateAdminModelProviderInput,
  type CreateAdminProviderModelInput,
  type TestAdminModelProviderConnectionInput,
  type UpdateAdminModelProviderInput,
} from "./types";

export class AdminModelRegistryApiError extends Error {
  readonly status: number;
  readonly code:
    | "AUTH_REQUIRED"
    | "NETWORK_ERROR"
    | "REQUEST_FAILED"
    | "INVALID_RESPONSE";
  /** Backend `KNOWLEDGE_*` error code from the error envelope, if present. */
  readonly knowledgeCode: string | null;
  readonly serverMessage: string | null;

  constructor(
    status: number,
    code: AdminModelRegistryApiError["code"],
    message: string,
    options: {
      knowledgeCode?: string | null;
      serverMessage?: string | null;
    } = {},
  ) {
    super(message);
    this.name = "AdminModelRegistryApiError";
    this.status = status;
    this.code = code;
    this.knowledgeCode = options.knowledgeCode ?? null;
    this.serverMessage = options.serverMessage ?? null;
  }
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

function adminSettingsBaseURL(): string {
  return `${getBackendBaseURL()}/api/admin/settings`;
}

async function requestAdminModelRegistry(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (isAbortError(error) || error instanceof AdminModelRegistryApiError) {
      throw error;
    }
    if (error instanceof AuthRequiredError) {
      throw new AdminModelRegistryApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new AdminModelRegistryApiError(
      0,
      "NETWORK_ERROR",
      "Model registry settings are temporarily unavailable.",
    );
  }
}

function readErrorEnvelope(body: unknown): {
  knowledgeCode: string | null;
  serverMessage: string | null;
} {
  if (typeof body !== "object" || body === null) {
    return { knowledgeCode: null, serverMessage: null };
  }
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail !== "object" || detail === null) {
    return { knowledgeCode: null, serverMessage: null };
  }
  const code = (detail as { code?: unknown }).code;
  const message = (detail as { message?: unknown }).message;
  return {
    knowledgeCode: typeof code === "string" ? code : null,
    serverMessage: typeof message === "string" ? message : null,
  };
}

async function readAdminModelRegistryResponse<T>(
  response: Response,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
): Promise<T> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new AdminModelRegistryApiError(
      response.status,
      "INVALID_RESPONSE",
      "Model registry settings response was invalid.",
    );
  }
  if (!response.ok) {
    const envelope = readErrorEnvelope(body);
    throw new AdminModelRegistryApiError(
      response.status,
      "REQUEST_FAILED",
      envelope.serverMessage ?? "Model registry settings request failed.",
      envelope,
    );
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new AdminModelRegistryApiError(
      response.status,
      "INVALID_RESPONSE",
      "Model registry settings response was invalid.",
    );
  }
  return parsed.data;
}

function jsonRequestInit(
  method: "POST" | "PATCH" | "DELETE",
  body?: unknown,
  signal?: AbortSignal,
): RequestInit {
  return {
    method,
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    ...(signal ? { signal } : {}),
  };
}

export async function listAdminModelProviders(
  accountId: string,
  signal?: AbortSignal,
): Promise<AdminModelProviderItem[]> {
  adminModelRegistryAccountIdSchema.parse(accountId);
  const response = await requestAdminModelRegistry(
    `${adminSettingsBaseURL()}/model-providers`,
    signal ? { signal } : undefined,
  );
  const parsed = await readAdminModelRegistryResponse(
    response,
    adminModelProviderListSchema,
  );
  return parsed.items;
}

export async function createAdminModelProvider(
  accountId: string,
  input: CreateAdminModelProviderInput,
  signal?: AbortSignal,
): Promise<AdminModelProviderItem> {
  adminModelRegistryAccountIdSchema.parse(accountId);
  const response = await requestAdminModelRegistry(
    `${adminSettingsBaseURL()}/model-providers`,
    jsonRequestInit("POST", input, signal),
  );
  const parsed = await readAdminModelRegistryResponse(
    response,
    adminModelProviderMutationSchema,
  );
  return parsed.item;
}

export async function updateAdminModelProvider(
  accountId: string,
  providerId: string,
  input: UpdateAdminModelProviderInput,
  signal?: AbortSignal,
): Promise<AdminModelProviderItem> {
  adminModelRegistryAccountIdSchema.parse(accountId);
  const response = await requestAdminModelRegistry(
    `${adminSettingsBaseURL()}/model-providers/${encodeURIComponent(providerId)}`,
    jsonRequestInit("PATCH", input, signal),
  );
  const parsed = await readAdminModelRegistryResponse(
    response,
    adminModelProviderMutationSchema,
  );
  return parsed.item;
}

export async function deleteAdminModelProvider(
  accountId: string,
  providerId: string,
  signal?: AbortSignal,
): Promise<void> {
  adminModelRegistryAccountIdSchema.parse(accountId);
  const response = await requestAdminModelRegistry(
    `${adminSettingsBaseURL()}/model-providers/${encodeURIComponent(providerId)}`,
    jsonRequestInit("DELETE", undefined, signal),
  );
  await readAdminModelRegistryResponse(
    response,
    adminModelProviderDeleteSchema,
  );
}

export async function listAdminProviderModels(
  accountId: string,
  providerId: string,
  signal?: AbortSignal,
): Promise<AdminProviderModelItem[]> {
  adminModelRegistryAccountIdSchema.parse(accountId);
  const response = await requestAdminModelRegistry(
    `${adminSettingsBaseURL()}/model-providers/${encodeURIComponent(providerId)}/models`,
    signal ? { signal } : undefined,
  );
  const parsed = await readAdminModelRegistryResponse(
    response,
    adminProviderModelListSchema,
  );
  return parsed.items;
}

export async function createAdminProviderModel(
  accountId: string,
  providerId: string,
  input: CreateAdminProviderModelInput,
  signal?: AbortSignal,
): Promise<AdminProviderModelItem> {
  adminModelRegistryAccountIdSchema.parse(accountId);
  const response = await requestAdminModelRegistry(
    `${adminSettingsBaseURL()}/model-providers/${encodeURIComponent(providerId)}/models`,
    jsonRequestInit("POST", input, signal),
  );
  const parsed = await readAdminModelRegistryResponse(
    response,
    adminProviderModelMutationSchema,
  );
  return parsed.item;
}

export async function setAdminProviderModelStatus(
  accountId: string,
  modelId: string,
  status: AdminProviderModelStatus,
  signal?: AbortSignal,
): Promise<AdminProviderModelItem> {
  adminModelRegistryAccountIdSchema.parse(accountId);
  const response = await requestAdminModelRegistry(
    `${adminSettingsBaseURL()}/provider-models/${encodeURIComponent(modelId)}`,
    jsonRequestInit("PATCH", { status }, signal),
  );
  const parsed = await readAdminModelRegistryResponse(
    response,
    adminProviderModelMutationSchema,
  );
  return parsed.item;
}

export async function deleteAdminProviderModel(
  accountId: string,
  modelId: string,
  signal?: AbortSignal,
): Promise<void> {
  adminModelRegistryAccountIdSchema.parse(accountId);
  const response = await requestAdminModelRegistry(
    `${adminSettingsBaseURL()}/provider-models/${encodeURIComponent(modelId)}`,
    jsonRequestInit("DELETE", undefined, signal),
  );
  await readAdminModelRegistryResponse(
    response,
    adminProviderModelDeleteSchema,
  );
}

/**
 * Candidate URL/Key test: imperative-only. The request body carries the
 * transient Key and must never be cached, retried, or logged by callers.
 */
export async function testAdminModelProviderConnection(
  accountId: string,
  input: TestAdminModelProviderConnectionInput,
  signal?: AbortSignal,
): Promise<AdminModelProviderConnectionTestResult> {
  adminModelRegistryAccountIdSchema.parse(accountId);
  const body = testAdminModelProviderConnectionInputSchema.parse(input);
  const response = await requestAdminModelRegistry(
    `${adminSettingsBaseURL()}/model-providers/test-connection`,
    jsonRequestInit("POST", body, signal),
  );
  return readAdminModelRegistryResponse(
    response,
    adminModelProviderConnectionTestSchema,
  );
}

export async function testAdminProviderModel(
  accountId: string,
  modelId: string,
  signal?: AbortSignal,
): Promise<AdminProviderModelTestResult> {
  adminModelRegistryAccountIdSchema.parse(accountId);
  const response = await requestAdminModelRegistry(
    `${adminSettingsBaseURL()}/provider-models/${encodeURIComponent(modelId)}/test`,
    jsonRequestInit("POST", undefined, signal),
  );
  return readAdminModelRegistryResponse(
    response,
    adminProviderModelTestSchema,
  );
}

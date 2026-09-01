import type { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  adminModelAccountIdSchema,
  adminModelCatalogSchema,
  adminModelConnectionTestResponseSchema,
  adminModelDefaultInputSchema,
  adminModelDeleteResponseSchema,
  adminModelIdSchema,
  adminModelMutationResponseSchema,
  adminModelStatusInputSchema,
  createAdminModelInputSchema,
  replaceAdminModelInputSchema,
  testAdminModelConnectionInputSchema,
  type AdminModelCatalog,
  type AdminModelConnectionTestResponse,
  type AdminModelDefaultInput,
  type AdminModelDeleteResponse,
  type AdminModelMutationResponse,
  type AdminModelStatusInput,
  type CreateAdminModelInput,
  type ReplaceAdminModelInput,
  type TestAdminModelConnectionInput,
} from "./types";

export class AdminModelSettingsApiError extends Error {
  readonly status: number;
  readonly code:
    | "AUTH_REQUIRED"
    | "NETWORK_ERROR"
    | "REQUEST_FAILED"
    | "INVALID_RESPONSE";

  constructor(
    status: number,
    code: AdminModelSettingsApiError["code"],
    message: string,
  ) {
    super(message);
    this.name = "AdminModelSettingsApiError";
    this.status = status;
    this.code = code;
  }
}

const mutationControllers = new Map<string, Set<AbortController>>();

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

function modelSettingsBaseURL(): string {
  return `${getBackendBaseURL()}/api/admin/settings/models`;
}

async function requestModelSettings(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (isAbortError(error) || error instanceof AdminModelSettingsApiError) {
      throw error;
    }
    if (error instanceof AuthRequiredError) {
      throw new AdminModelSettingsApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new AdminModelSettingsApiError(
      0,
      "NETWORK_ERROR",
      "Model settings are temporarily unavailable.",
    );
  }
}

async function readModelSettingsResponse<T>(
  response: Response,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
): Promise<T> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new AdminModelSettingsApiError(
      response.status,
      "INVALID_RESPONSE",
      "Model settings response was invalid.",
    );
  }

  if (!response.ok) {
    throw new AdminModelSettingsApiError(
      response.status,
      "REQUEST_FAILED",
      "Model settings request failed.",
    );
  }

  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new AdminModelSettingsApiError(
      response.status,
      "INVALID_RESPONSE",
      "Model settings response was invalid.",
    );
  }
  return parsed.data;
}

function jsonRequestInit(
  method: "POST" | "PUT",
  body: unknown,
  signal?: AbortSignal,
): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    ...(signal ? { signal } : {}),
  };
}

export async function fetchAdminModelCatalog(
  accountId: string,
  signal?: AbortSignal,
): Promise<AdminModelCatalog> {
  adminModelAccountIdSchema.parse(accountId);
  const response = await requestModelSettings(modelSettingsBaseURL(), {
    signal,
  });
  return readModelSettingsResponse(response, adminModelCatalogSchema);
}

export async function createAdminModel(
  accountId: string,
  input: CreateAdminModelInput,
  signal?: AbortSignal,
): Promise<AdminModelMutationResponse> {
  adminModelAccountIdSchema.parse(accountId);
  const body = createAdminModelInputSchema.parse(input);
  const response = await requestModelSettings(
    modelSettingsBaseURL(),
    jsonRequestInit("POST", body, signal),
  );
  return readModelSettingsResponse(response, adminModelMutationResponseSchema);
}

export async function replaceAdminModel(
  accountId: string,
  modelId: string,
  input: ReplaceAdminModelInput,
  signal?: AbortSignal,
): Promise<AdminModelMutationResponse> {
  adminModelAccountIdSchema.parse(accountId);
  const parsedId = adminModelIdSchema.parse(modelId);
  const body = replaceAdminModelInputSchema.parse(input);
  const response = await requestModelSettings(
    `${modelSettingsBaseURL()}/${parsedId}`,
    jsonRequestInit("PUT", body, signal),
  );
  return readModelSettingsResponse(response, adminModelMutationResponseSchema);
}

export async function deleteAdminModel(
  accountId: string,
  modelId: string,
  signal?: AbortSignal,
): Promise<AdminModelDeleteResponse> {
  adminModelAccountIdSchema.parse(accountId);
  const parsedId = adminModelIdSchema.parse(modelId);
  const response = await requestModelSettings(
    `${modelSettingsBaseURL()}/${parsedId}`,
    {
      method: "DELETE",
      ...(signal ? { signal } : {}),
    },
  );
  return readModelSettingsResponse(response, adminModelDeleteResponseSchema);
}

export async function testAdminModelConnection(
  accountId: string,
  input: TestAdminModelConnectionInput,
  signal?: AbortSignal,
): Promise<AdminModelConnectionTestResponse> {
  adminModelAccountIdSchema.parse(accountId);
  const body = testAdminModelConnectionInputSchema.parse(input);
  const response = await requestModelSettings(
    `${modelSettingsBaseURL()}/test-connection`,
    jsonRequestInit("POST", body, signal),
  );
  return readModelSettingsResponse(
    response,
    adminModelConnectionTestResponseSchema,
  );
}

export async function setAdminModelStatus(
  accountId: string,
  modelId: string,
  input: AdminModelStatusInput,
  signal?: AbortSignal,
): Promise<AdminModelMutationResponse> {
  adminModelAccountIdSchema.parse(accountId);
  const parsedId = adminModelIdSchema.parse(modelId);
  const body = adminModelStatusInputSchema.parse(input);
  const response = await requestModelSettings(
    `${modelSettingsBaseURL()}/${parsedId}/status`,
    jsonRequestInit("POST", body, signal),
  );
  return readModelSettingsResponse(response, adminModelMutationResponseSchema);
}

export async function setAdminModelDefault(
  accountId: string,
  modelId: string,
  input: AdminModelDefaultInput,
  signal?: AbortSignal,
): Promise<AdminModelMutationResponse> {
  adminModelAccountIdSchema.parse(accountId);
  const parsedId = adminModelIdSchema.parse(modelId);
  const body = adminModelDefaultInputSchema.parse(input);
  const response = await requestModelSettings(
    `${modelSettingsBaseURL()}/${parsedId}/default`,
    jsonRequestInit("POST", body, signal),
  );
  return readModelSettingsResponse(response, adminModelMutationResponseSchema);
}

export async function runAbortableAdminModelMutation<T>(
  accountId: string,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const parsed = adminModelAccountIdSchema.parse(accountId);
  const controller = new AbortController();
  const controllers = mutationControllers.get(parsed) ?? new Set();
  controllers.add(controller);
  mutationControllers.set(parsed, controllers);
  try {
    return await operation(controller.signal);
  } finally {
    controllers.delete(controller);
    if (controllers.size === 0) mutationControllers.delete(parsed);
  }
}

export function abortAdminModelSettingsAccount(accountId: string): void {
  const parsed = adminModelAccountIdSchema.safeParse(accountId);
  if (!parsed.success) return;
  mutationControllers
    .get(parsed.data)
    ?.forEach((controller) => controller.abort());
  mutationControllers.delete(parsed.data);
}

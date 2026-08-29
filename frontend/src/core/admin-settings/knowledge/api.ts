import type { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  adminKnowledgeAccountIdSchema,
  adminKnowledgeModelDeleteSchema,
  adminKnowledgeModelListSchema,
  adminKnowledgeModelMutationSchema,
  adminKnowledgeModelTestSchema,
  type AdminKnowledgeModelItem,
  type AdminKnowledgeModelList,
  type AdminKnowledgeModelTestResult,
  type CreateAdminKnowledgeModelInput,
  type UpdateAdminKnowledgeModelInput,
} from "./types";

export class AdminKnowledgeApiError extends Error {
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
    code: AdminKnowledgeApiError["code"],
    message: string,
    options: {
      knowledgeCode?: string | null;
      serverMessage?: string | null;
    } = {},
  ) {
    super(message);
    this.name = "AdminKnowledgeApiError";
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

function adminKnowledgeBaseURL(): string {
  return `${getBackendBaseURL()}/api/admin/knowledge/models`;
}

async function requestAdminKnowledge(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (isAbortError(error) || error instanceof AdminKnowledgeApiError) {
      throw error;
    }
    if (error instanceof AuthRequiredError) {
      throw new AdminKnowledgeApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new AdminKnowledgeApiError(
      0,
      "NETWORK_ERROR",
      "Knowledge model settings are temporarily unavailable.",
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

async function readAdminKnowledgeResponse<T>(
  response: Response,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
): Promise<T> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new AdminKnowledgeApiError(
      response.status,
      "INVALID_RESPONSE",
      "Knowledge model settings response was invalid.",
    );
  }
  if (!response.ok) {
    const envelope = readErrorEnvelope(body);
    throw new AdminKnowledgeApiError(
      response.status,
      "REQUEST_FAILED",
      envelope.serverMessage ?? "Knowledge model settings request failed.",
      envelope,
    );
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new AdminKnowledgeApiError(
      response.status,
      "INVALID_RESPONSE",
      "Knowledge model settings response was invalid.",
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

export async function listAdminKnowledgeModels(
  accountId: string,
  signal?: AbortSignal,
): Promise<AdminKnowledgeModelList> {
  adminKnowledgeAccountIdSchema.parse(accountId);
  const response = await requestAdminKnowledge(
    `${adminKnowledgeBaseURL()}?page=1&page_size=100`,
    signal ? { signal } : undefined,
  );
  return readAdminKnowledgeResponse(response, adminKnowledgeModelListSchema);
}

export async function createAdminKnowledgeModel(
  accountId: string,
  input: CreateAdminKnowledgeModelInput,
  signal?: AbortSignal,
): Promise<AdminKnowledgeModelItem> {
  adminKnowledgeAccountIdSchema.parse(accountId);
  const response = await requestAdminKnowledge(
    adminKnowledgeBaseURL(),
    jsonRequestInit("POST", input, signal),
  );
  const parsed = await readAdminKnowledgeResponse(
    response,
    adminKnowledgeModelMutationSchema,
  );
  return parsed.item;
}

export async function updateAdminKnowledgeModel(
  accountId: string,
  configurationId: string,
  input: UpdateAdminKnowledgeModelInput,
  signal?: AbortSignal,
): Promise<AdminKnowledgeModelItem> {
  adminKnowledgeAccountIdSchema.parse(accountId);
  const response = await requestAdminKnowledge(
    `${adminKnowledgeBaseURL()}/${encodeURIComponent(configurationId)}`,
    jsonRequestInit("PATCH", input, signal),
  );
  const parsed = await readAdminKnowledgeResponse(
    response,
    adminKnowledgeModelMutationSchema,
  );
  return parsed.item;
}

export async function deleteAdminKnowledgeModel(
  accountId: string,
  configurationId: string,
  signal?: AbortSignal,
): Promise<void> {
  adminKnowledgeAccountIdSchema.parse(accountId);
  const response = await requestAdminKnowledge(
    `${adminKnowledgeBaseURL()}/${encodeURIComponent(configurationId)}`,
    jsonRequestInit("DELETE", undefined, signal),
  );
  await readAdminKnowledgeResponse(response, adminKnowledgeModelDeleteSchema);
}

export async function testAdminKnowledgeModel(
  accountId: string,
  configurationId: string,
  signal?: AbortSignal,
): Promise<AdminKnowledgeModelTestResult> {
  adminKnowledgeAccountIdSchema.parse(accountId);
  const response = await requestAdminKnowledge(
    `${adminKnowledgeBaseURL()}/${encodeURIComponent(configurationId)}/test`,
    jsonRequestInit("POST", undefined, signal),
  );
  return readAdminKnowledgeResponse(response, adminKnowledgeModelTestSchema);
}

import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  privacyAccountIdSchema,
  privacyCaseListSchema,
  privacyEarlyDeleteResponseSchema,
  privacyProjectIdSchema,
  type PrivacyCase,
  type PrivacyEarlyDeleteResponse,
} from "./types";

const privacyErrorCodeSchema = z.enum([
  "PRIVACY_CASE_NOT_FOUND",
  "DATABASE_UNAVAILABLE",
]);

const privacyErrorEnvelopeSchema = z
  .object({
    detail: z
      .object({
        code: privacyErrorCodeSchema,
        message: z.string().min(1),
      })
      .strict(),
  })
  .strict();

type PrivacyServerErrorCode = z.infer<typeof privacyErrorCodeSchema>;
type PrivacyClientErrorCode =
  | PrivacyServerErrorCode
  | "AUTH_REQUIRED"
  | "PRIVACY_NETWORK_ERROR"
  | "PRIVACY_RESPONSE_INVALID"
  | "PRIVACY_ERROR_RESPONSE_INVALID"
  | "PRIVACY_VALIDATION_FAILED";

const SAFE_MESSAGES: Record<PrivacyServerErrorCode, string> = {
  PRIVACY_CASE_NOT_FOUND: "Privacy retention case not found",
  DATABASE_UNAVAILABLE: "Privacy storage is temporarily unavailable",
};

export class PrivacyCenterApiError extends Error {
  readonly status: number;
  readonly code: PrivacyClientErrorCode;

  constructor(status: number, code: PrivacyClientErrorCode, message: string) {
    super(message);
    this.name = "PrivacyCenterApiError";
    this.status = status;
    this.code = code;
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

function parseIdentity(accountId: string, projectId?: string) {
  const account = privacyAccountIdSchema.safeParse(accountId);
  const project =
    projectId === undefined
      ? undefined
      : privacyProjectIdSchema.safeParse(projectId);
  if (!account.success || (project !== undefined && !project.success)) {
    throw new PrivacyCenterApiError(
      422,
      "PRIVACY_VALIDATION_FAILED",
      "Privacy request is invalid",
    );
  }
  return {
    accountId: account.data,
    projectId: project?.data,
  };
}

function privacyURL(path = "") {
  return `${getBackendBaseURL()}/api/privacy${path}`;
}

async function request(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (error instanceof PrivacyCenterApiError || isAbortError(error)) {
      throw error;
    }
    if (error instanceof AuthRequiredError) {
      throw new PrivacyCenterApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new PrivacyCenterApiError(
      0,
      "PRIVACY_NETWORK_ERROR",
      "Privacy service is unavailable",
    );
  }
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new PrivacyCenterApiError(
      response.status,
      response.ok
        ? "PRIVACY_RESPONSE_INVALID"
        : "PRIVACY_ERROR_RESPONSE_INVALID",
      response.ok ? "Privacy response was invalid" : "Privacy request failed",
    );
  }
}

async function throwResponseError(response: Response): Promise<never> {
  const parsed = privacyErrorEnvelopeSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new PrivacyCenterApiError(
      response.status,
      "PRIVACY_ERROR_RESPONSE_INVALID",
      "Privacy request failed",
    );
  }
  const { code } = parsed.data.detail;
  throw new PrivacyCenterApiError(response.status, code, SAFE_MESSAGES[code]);
}

async function parseResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
): Promise<T> {
  if (!response.ok) await throwResponseError(response);
  const parsed = schema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new PrivacyCenterApiError(
      response.status,
      "PRIVACY_RESPONSE_INVALID",
      "Privacy response was invalid",
    );
  }
  return parsed.data;
}

export async function listPrivacyCases(
  accountId: string,
  signal?: AbortSignal,
): Promise<PrivacyCase[]> {
  parseIdentity(accountId);
  const response = await request(privacyURL("/cases"), {
    credentials: "include",
    signal,
  });
  return parseResponse(response, privacyCaseListSchema);
}

export function privacyExportURL(accountId: string, projectId: string): string {
  const identity = parseIdentity(accountId, projectId);
  return privacyURL(`/cases/${identity.projectId}/export`);
}

export async function requestPrivacyEarlyDelete(
  accountId: string,
  projectId: string,
  signal?: AbortSignal,
): Promise<PrivacyEarlyDeleteResponse> {
  const identity = parseIdentity(accountId, projectId);
  const response = await request(
    privacyURL(`/cases/${identity.projectId}/early-delete`),
    {
      method: "POST",
      credentials: "include",
      signal,
    },
  );
  return parseResponse(response, privacyEarlyDeleteResponseSchema);
}

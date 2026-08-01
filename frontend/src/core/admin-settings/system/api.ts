import type { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  adminSystemSettingsAccountIdSchema,
  replaceAgentRuntimeSettingsInputSchema,
  replaceAuthSettingsInputSchema,
  replaceQuotaSettingsInputSchema,
  systemSettingsCatalogSchema,
  systemSettingsMutationResponseSchema,
  systemSettingsSectionNameSchema,
  type ReplaceAgentRuntimeSettingsInput,
  type ReplaceAuthSettingsInput,
  type ReplaceQuotaSettingsInput,
  type SystemSettingsCatalog,
  type SystemSettingsMutationResponse,
  type SystemSettingsSectionName,
} from "./types";

export class AdminSystemSettingsApiError extends Error {
  readonly status: number;
  readonly code:
    | "AUTH_REQUIRED"
    | "NETWORK_ERROR"
    | "REQUEST_FAILED"
    | "INVALID_RESPONSE";

  constructor(
    status: number,
    code: AdminSystemSettingsApiError["code"],
    message: string,
  ) {
    super(message);
    this.name = "AdminSystemSettingsApiError";
    this.status = status;
    this.code = code;
  }
}

export type ReplaceSystemSettingsSectionInput =
  | {
      section: "agent_runtime";
      input: ReplaceAgentRuntimeSettingsInput;
    }
  | { section: "auth"; input: ReplaceAuthSettingsInput }
  | { section: "quotas"; input: ReplaceQuotaSettingsInput };

const mutationControllers = new Map<string, Set<AbortController>>();

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

function systemSettingsBaseURL(): string {
  return `${getBackendBaseURL()}/api/admin/settings/system`;
}

async function requestSystemSettings(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (isAbortError(error) || error instanceof AdminSystemSettingsApiError) {
      throw error;
    }
    if (error instanceof AuthRequiredError) {
      throw new AdminSystemSettingsApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new AdminSystemSettingsApiError(
      0,
      "NETWORK_ERROR",
      "System settings are temporarily unavailable.",
    );
  }
}

async function readSystemSettingsResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
): Promise<T> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new AdminSystemSettingsApiError(
      response.status,
      "INVALID_RESPONSE",
      "System settings response was invalid.",
    );
  }
  if (!response.ok) {
    throw new AdminSystemSettingsApiError(
      response.status,
      "REQUEST_FAILED",
      "System settings request failed.",
    );
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new AdminSystemSettingsApiError(
      response.status,
      "INVALID_RESPONSE",
      "System settings response was invalid.",
    );
  }
  return parsed.data;
}

export async function fetchAdminSystemSettings(
  accountId: string,
  signal?: AbortSignal,
): Promise<SystemSettingsCatalog> {
  adminSystemSettingsAccountIdSchema.parse(accountId);
  const response = await requestSystemSettings(systemSettingsBaseURL(), {
    signal,
  });
  return readSystemSettingsResponse(response, systemSettingsCatalogSchema);
}

function parseReplaceInput(
  section: SystemSettingsSectionName,
  input: unknown,
):
  | ReplaceAgentRuntimeSettingsInput
  | ReplaceAuthSettingsInput
  | ReplaceQuotaSettingsInput {
  switch (section) {
    case "agent_runtime":
      return replaceAgentRuntimeSettingsInputSchema.parse(input);
    case "auth":
      return replaceAuthSettingsInputSchema.parse(input);
    case "quotas":
      return replaceQuotaSettingsInputSchema.parse(input);
  }
}

export async function replaceAdminSystemSettingsSection(
  accountId: string,
  section: "agent_runtime",
  input: ReplaceAgentRuntimeSettingsInput,
  signal?: AbortSignal,
): Promise<SystemSettingsMutationResponse>;
export async function replaceAdminSystemSettingsSection(
  accountId: string,
  section: "auth",
  input: ReplaceAuthSettingsInput,
  signal?: AbortSignal,
): Promise<SystemSettingsMutationResponse>;
export async function replaceAdminSystemSettingsSection(
  accountId: string,
  section: "quotas",
  input: ReplaceQuotaSettingsInput,
  signal?: AbortSignal,
): Promise<SystemSettingsMutationResponse>;
export async function replaceAdminSystemSettingsSection(
  accountId: string,
  section: SystemSettingsSectionName,
  input:
    | ReplaceAgentRuntimeSettingsInput
    | ReplaceAuthSettingsInput
    | ReplaceQuotaSettingsInput,
  signal?: AbortSignal,
): Promise<SystemSettingsMutationResponse> {
  adminSystemSettingsAccountIdSchema.parse(accountId);
  const parsedSection = systemSettingsSectionNameSchema.parse(section);
  const body = parseReplaceInput(parsedSection, input);
  const response = await requestSystemSettings(
    `${systemSettingsBaseURL()}/${parsedSection}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  const parsed = await readSystemSettingsResponse(
    response,
    systemSettingsMutationResponseSchema,
  );
  if (parsed.section !== parsedSection) {
    throw new AdminSystemSettingsApiError(
      response.status,
      "INVALID_RESPONSE",
      "System settings response was invalid.",
    );
  }
  return parsed;
}

export async function runAbortableAdminSystemSettingsMutation<T>(
  accountId: string,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const parsed = adminSystemSettingsAccountIdSchema.parse(accountId);
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

export function abortAdminSystemSettingsAccount(accountId: string): void {
  const parsed = adminSystemSettingsAccountIdSchema.safeParse(accountId);
  if (!parsed.success) return;
  mutationControllers
    .get(parsed.data)
    ?.forEach((controller) => controller.abort());
  mutationControllers.delete(parsed.data);
}

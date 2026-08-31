import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  adminKnowledgeAccountIdSchema,
  adminKnowledgeSettingsSchema,
  adminKnowledgeSettingsUpdateSchema,
  type AdminKnowledgeSettings,
  type AdminKnowledgeSettingsUpdate,
} from "./types";

const serverErrorSchema = z
  .object({
    detail: z
      .object({
        code: z.enum([
          "KNOWLEDGE_SETTINGS_CONFLICT",
          "KNOWLEDGE_SETTINGS_INVALID",
          "KNOWLEDGE_SETTINGS_UNAVAILABLE",
        ]),
        message: z.string().min(1).max(1024),
        request_id: z.string(),
      })
      .strict(),
  })
  .strict();

export class AdminKnowledgeSettingsApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly publicMessage?: string,
  ) {
    super("Knowledge settings request failed.");
    this.name = "AdminKnowledgeSettingsApiError";
  }
}

const mutationControllers = new Map<string, Set<AbortController>>();
const accountGenerations = new Map<string, number>();

export function knowledgeSettingsAccountGeneration(accountId: string): number {
  return accountGenerations.get(accountId) ?? 0;
}

function requireActive(signal?: AbortSignal): void {
  if (signal?.aborted) throw new DOMException("Request aborted", "AbortError");
}

async function requestSettings(
  init: RequestInit,
): Promise<AdminKnowledgeSettings> {
  let response: Response;
  try {
    response = await fetchWithAuth(
      `${getBackendBaseURL()}/api/admin/settings/knowledge`,
      init,
    );
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    if (error instanceof AuthRequiredError)
      throw new AdminKnowledgeSettingsApiError(401, "AUTH_REQUIRED");
    throw new AdminKnowledgeSettingsApiError(0, "NETWORK_ERROR");
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new AdminKnowledgeSettingsApiError(
      response.status,
      "INVALID_RESPONSE",
    );
  }
  requireActive(init.signal ?? undefined);
  if (!response.ok) {
    const error = serverErrorSchema.safeParse(body);
    throw new AdminKnowledgeSettingsApiError(
      response.status,
      error.success ? error.data.detail.code : "REQUEST_FAILED",
      error.success && error.data.detail.code === "KNOWLEDGE_SETTINGS_INVALID"
        ? error.data.detail.message
        : undefined,
    );
  }
  const parsed = adminKnowledgeSettingsSchema.safeParse(body);
  if (!parsed.success)
    throw new AdminKnowledgeSettingsApiError(
      response.status,
      "INVALID_RESPONSE",
    );
  return parsed.data;
}

export async function fetchAdminKnowledgeSettings(
  accountId: string,
  signal?: AbortSignal,
) {
  adminKnowledgeAccountIdSchema.parse(accountId);
  requireActive(signal);
  return requestSettings({ signal });
}

/** Imperative write: plaintext must never become TanStack mutation variables. */
export async function replaceAdminKnowledgeSettings(
  accountId: string,
  input: AdminKnowledgeSettingsUpdate,
  signal?: AbortSignal,
): Promise<AdminKnowledgeSettings> {
  const account = adminKnowledgeAccountIdSchema.parse(accountId);
  const parsed = adminKnowledgeSettingsUpdateSchema.safeParse(input);
  if (!parsed.success)
    throw new AdminKnowledgeSettingsApiError(422, "INVALID_REQUEST");
  requireActive(signal);
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  const controllers =
    mutationControllers.get(account) ?? new Set<AbortController>();
  controllers.add(controller);
  mutationControllers.set(account, controllers);
  try {
    return await requestSettings({
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed.data),
      signal: controller.signal,
    });
  } finally {
    signal?.removeEventListener("abort", cancel);
    controllers.delete(controller);
    if (
      controllers.size === 0 &&
      mutationControllers.get(account) === controllers
    )
      mutationControllers.delete(account);
  }
}

export function abortAdminKnowledgeSettingsAccount(accountId: string): void {
  accountGenerations.set(
    accountId,
    knowledgeSettingsAccountGeneration(accountId) + 1,
  );
  mutationControllers
    .get(accountId)
    ?.forEach((controller) => controller.abort());
  mutationControllers.delete(accountId);
}

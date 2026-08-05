import type { z } from "zod";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";

import {
  accountPersonalizationAccountIdSchema,
  accountPersonalizationSchema,
  resetAccountMemoryInputSchema,
  resetAccountMemoryResultSchema,
  updateAccountPersonalizationInputSchema,
  type AccountPersonalization,
  type ResetAccountMemoryInput,
  type ResetAccountMemoryResult,
  type UpdateAccountPersonalizationInput,
} from "./types";

const ACCOUNT_PERSONALIZATION_URL = "/api/v1/account/personalization";
const mutationControllers = new Map<string, Set<AbortController>>();

async function readJSON<T>(
  response: Response,
  schema: z.ZodType<T>,
  fallback: string,
): Promise<T> {
  if (!response.ok) await throwGatewayApiError(response, fallback);
  return schema.parse(await response.json());
}

function jsonRequestInit(
  method: "PATCH" | "POST",
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

export async function fetchAccountPersonalization(
  accountId: string,
  signal?: AbortSignal,
): Promise<AccountPersonalization> {
  accountPersonalizationAccountIdSchema.parse(accountId);
  const response = await fetchWithAuth(ACCOUNT_PERSONALIZATION_URL, {
    cache: "no-store",
    ...(signal ? { signal } : {}),
  });
  return readJSON(
    response,
    accountPersonalizationSchema,
    "Failed to load personalization settings",
  );
}

export async function updateAccountPersonalization(
  accountId: string,
  input: UpdateAccountPersonalizationInput,
  signal?: AbortSignal,
): Promise<AccountPersonalization> {
  accountPersonalizationAccountIdSchema.parse(accountId);
  const body = updateAccountPersonalizationInputSchema.parse(input);
  const response = await fetchWithAuth(
    ACCOUNT_PERSONALIZATION_URL,
    jsonRequestInit("PATCH", body, signal),
  );
  return readJSON(
    response,
    accountPersonalizationSchema,
    "Failed to update personalization settings",
  );
}

export async function resetAccountMemory(
  accountId: string,
  input: ResetAccountMemoryInput,
  signal?: AbortSignal,
): Promise<ResetAccountMemoryResult> {
  accountPersonalizationAccountIdSchema.parse(accountId);
  const body = resetAccountMemoryInputSchema.parse(input);
  const response = await fetchWithAuth(
    `${ACCOUNT_PERSONALIZATION_URL}/memory/reset`,
    jsonRequestInit("POST", body, signal),
  );
  return readJSON(
    response,
    resetAccountMemoryResultSchema,
    "Failed to reset account Memory",
  );
}

export async function runAbortableAccountPersonalizationMutation<T>(
  accountId: string,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const parsedAccountId = accountPersonalizationAccountIdSchema.parse(accountId);
  const controller = new AbortController();
  const accountControllers = mutationControllers.get(parsedAccountId) ?? new Set();
  accountControllers.add(controller);
  mutationControllers.set(parsedAccountId, accountControllers);
  try {
    return await operation(controller.signal);
  } finally {
    accountControllers.delete(controller);
    if (accountControllers.size === 0) {
      mutationControllers.delete(parsedAccountId);
    }
  }
}

export function abortAccountPersonalizationAccount(accountId: string): void {
  const parsedAccountId = accountPersonalizationAccountIdSchema.parse(accountId);
  for (const controller of mutationControllers.get(parsedAccountId) ?? []) {
    controller.abort();
  }
  mutationControllers.delete(parsedAccountId);
}

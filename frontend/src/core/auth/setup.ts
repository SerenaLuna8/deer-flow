import { z } from "zod";

import { AUTH_PROBE_TIMEOUT_MS, fetchAuth } from "./request";
import { parseAuthError } from "./types";

export const setupStatusSchema = z
  .object({
    needs_setup: z.boolean(),
    registration_enabled: z.boolean(),
  })
  .strict();

export type SetupStatusResponse = z.infer<typeof setupStatusSchema>;

export type SetupStatusCheck = {
  checked: boolean;
  status: SetupStatusResponse | null;
};

export const setupStatusFetchInit = {
  cache: "no-store",
  credentials: "include",
} satisfies RequestInit;

export async function fetchSetupStatus(
  signal?: AbortSignal,
): Promise<SetupStatusResponse> {
  const response = await fetchAuth(
    "/api/v1/auth/setup-status",
    {
      ...setupStatusFetchInit,
      signal,
    },
    AUTH_PROBE_TIMEOUT_MS,
  );
  if (!response.ok) {
    throw new Error(`setup-status failed: ${response.status}`);
  }
  return setupStatusSchema.parse(await response.json());
}

export function isSystemAlreadyInitializedError(data: unknown): boolean {
  return parseAuthError(data).code === "system_already_initialized";
}

export function canCreateRegularAccount(check: SetupStatusCheck): boolean {
  return (
    check.checked &&
    check.status?.needs_setup === false &&
    check.status.registration_enabled
  );
}

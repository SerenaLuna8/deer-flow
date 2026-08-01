import { z } from "zod";

import { AUTH_PROBE_TIMEOUT_MS, fetchAuth } from "./request";

export const authProviderSchema = z
  .object({
    id: z.string().min(1),
    display_name: z.string().min(1),
    type: z.literal("oidc"),
  })
  .strict();

export const authProvidersResponseSchema = z
  .object({
    providers: z.array(authProviderSchema),
  })
  .strict();

export type AuthProviderSummary = z.infer<typeof authProviderSchema>;

export async function fetchAuthProviders(
  signal?: AbortSignal,
): Promise<AuthProviderSummary[]> {
  const response = await fetchAuth(
    "/api/v1/auth/providers",
    {
      cache: "no-store",
      signal,
    },
    AUTH_PROBE_TIMEOUT_MS,
  );
  if (!response.ok) {
    throw new Error(`auth providers failed: ${response.status}`);
  }
  return authProvidersResponseSchema.parse(await response.json()).providers;
}

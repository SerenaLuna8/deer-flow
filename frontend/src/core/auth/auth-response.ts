import { type User, userSchema } from "./types";

export type AuthMeResult =
  | { type: "authenticated"; user: User }
  | { type: "unauthenticated" }
  | { type: "unavailable" };

export type PostAuthRefreshAction =
  | "complete"
  | "redirect-login"
  | "retry";

/**
 * Only an explicit 401 proves that the browser identity is gone. Network
 * failures, 5xx responses, and malformed success bodies are availability
 * failures and must not erase a known account or its caches.
 */
export async function classifyAuthMeResponse(
  response: Response,
): Promise<AuthMeResult> {
  if (response.status === 401) return { type: "unauthenticated" };
  if (!response.ok) return { type: "unavailable" };

  try {
    const parsed = userSchema.safeParse(await response.json());
    return parsed.success
      ? { type: "authenticated", user: parsed.data }
      : { type: "unavailable" };
  } catch {
    return { type: "unavailable" };
  }
}

export function postAuthRefreshAction(
  result: AuthMeResult | null,
): PostAuthRefreshAction {
  if (result?.type === "authenticated") return "complete";
  if (result?.type === "unauthenticated") return "redirect-login";
  return "retry";
}

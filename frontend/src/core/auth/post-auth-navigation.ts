import type { AuthMeResult } from "./auth-response";

export async function restoreSessionThenNavigate(
  restoreUser: () => Promise<AuthMeResult | null>,
  navigate: () => void,
): Promise<boolean> {
  const result = await restoreUser();
  if (result?.type !== "authenticated") return false;
  navigate();
  return true;
}

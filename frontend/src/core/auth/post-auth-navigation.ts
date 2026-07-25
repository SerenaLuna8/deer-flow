import type { User } from "./types";

export async function restoreSessionThenNavigate(
  restoreUser: () => Promise<User | null>,
  navigate: () => void,
): Promise<boolean> {
  const user = await restoreUser();
  if (!user) return false;
  navigate();
  return true;
}

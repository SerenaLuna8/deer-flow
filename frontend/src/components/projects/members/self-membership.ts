import { AUTH_DISABLED_USER } from "@/core/auth/auth-disabled-user";
import type { User } from "@/core/auth/types";
import type { ProjectMembership } from "@/core/projects/types";

export function findSelfMembership(
  memberships: readonly ProjectMembership[],
  user: User | null,
) {
  if (!user) return undefined;
  const byUserId = memberships.find(
    (membership) => membership.user_id === user.id,
  );
  if (byUserId) return byUserId;
  if (
    user.id !== AUTH_DISABLED_USER.id ||
    user.email !== AUTH_DISABLED_USER.email
  ) {
    return undefined;
  }
  return memberships.find(
    (membership) => membership.account_email === user.email,
  );
}

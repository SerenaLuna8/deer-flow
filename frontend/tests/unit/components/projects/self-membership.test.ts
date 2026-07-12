import { describe, expect, test } from "@rstest/core";

import { findSelfMembership } from "@/components/projects/members/self-membership";
import { AUTH_DISABLED_USER } from "@/core/auth/auth-disabled-user";
import type { User } from "@/core/auth/types";
import type { ProjectMembership } from "@/core/projects/types";

const membership: ProjectMembership = {
  membership_id: "20000000-0000-4000-8000-000000000001",
  user_id: "40000000-0000-4000-8000-000000000001",
  account_email: AUTH_DISABLED_USER.email,
  role: "viewer",
  status: "active",
  version: 4,
  joined_at: "2026-07-01T08:00:00+00:00",
};

const normalUser: User = {
  ...AUTH_DISABLED_USER,
  id: membership.user_id,
  email: "member@example.com",
  system_role: "user",
};

describe("findSelfMembership", () => {
  test("prefers the authenticated user ID", () => {
    expect(findSelfMembership([membership], normalUser)).toBe(membership);
  });

  test("allows email fallback only for the exact auth-disabled identity", () => {
    expect(findSelfMembership([membership], AUTH_DISABLED_USER)).toBe(
      membership,
    );
    expect(
      findSelfMembership([membership], {
        ...AUTH_DISABLED_USER,
        id: "another-user",
      }),
    ).toBeUndefined();
    expect(
      findSelfMembership([membership], {
        ...AUTH_DISABLED_USER,
        email: "other@example.com",
      }),
    ).toBeUndefined();
  });

  test("does not infer a normal user's membership from email", () => {
    expect(
      findSelfMembership([membership], {
        ...normalUser,
        id: "50000000-0000-4000-8000-000000000001",
        email: membership.account_email,
      }),
    ).toBeUndefined();
  });
});

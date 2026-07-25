import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, rs, test } from "@rstest/core";

import { restoreSessionThenNavigate } from "@/core/auth/post-auth-navigation";
import type { User } from "@/core/auth/types";

const invitedUser: User = {
  id: "40000000-0000-4000-8000-000000000002",
  email: "invitee@example.com",
  system_role: "user",
  needs_setup: false,
  oauth_provider: null,
};

describe("post-auth navigation", () => {
  test("waits for the current user before returning to the invitation", async () => {
    let release!: (user: User | null) => void;
    const restored = new Promise<User | null>((resolve) => {
      release = resolve;
    });
    const navigate = rs.fn();

    const completion = restoreSessionThenNavigate(
      () => restored,
      () => navigate("/invite"),
    );
    await Promise.resolve();
    expect(navigate).not.toHaveBeenCalled();

    release(invitedUser);
    await expect(completion).resolves.toBe(true);
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/invite");

    const loginSource = readFileSync(
      resolve(process.cwd(), "src/app/(auth)/login/page.tsx"),
      "utf8",
    );
    expect(loginSource).toContain(
      "restoreSessionThenNavigate(refreshUser, () =>",
    );
    expect(loginSource).toContain("window.location.replace(redirectPath)");
  });

  test("does not navigate when the session user cannot be restored", async () => {
    const navigate = rs.fn();

    await expect(
      restoreSessionThenNavigate(
        async () => null,
        () => navigate("/invite"),
      ),
    ).resolves.toBe(false);
    expect(navigate).not.toHaveBeenCalled();
  });
});

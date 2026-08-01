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
    let release!: (
      result:
        | { type: "authenticated"; user: User }
        | { type: "unavailable" },
    ) => void;
    const restored = new Promise<
      | { type: "authenticated"; user: User }
      | { type: "unavailable" }
    >((resolve) => {
      release = resolve;
    });
    const navigate = rs.fn();

    const completion = restoreSessionThenNavigate(
      () => restored,
      () => navigate("/invite"),
    );
    await Promise.resolve();
    expect(navigate).not.toHaveBeenCalled();

    release({ type: "authenticated", user: invitedUser });
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

  test("does not navigate when session restoration is temporarily unavailable", async () => {
    const navigate = rs.fn();

    await expect(
      restoreSessionThenNavigate(
        async () => ({ type: "unavailable" }),
        () => navigate("/invite"),
      ),
    ).resolves.toBe(false);
    expect(navigate).not.toHaveBeenCalled();
  });

  test("does not re-probe or invite password resubmission after a successful change", () => {
    const setupSource = readFileSync(
      resolve(process.cwd(), "src/app/(auth)/setup/page.tsx"),
      "utf8",
    );
    const settingsSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/settings/account-settings-page.tsx",
      ),
      "utf8",
    );

    expect(setupSource).toContain('window.location.replace("/workspace")');
    expect(setupSource).not.toContain(
      "const refreshed = await refreshUser(controller.signal)",
    );
    expect(settingsSource).not.toContain(
      "await refreshUser(controller.signal)",
    );
  });
});

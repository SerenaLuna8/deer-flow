import { describe, expect, test } from "@rstest/core";

import {
  classifyAuthMeResponse,
  postAuthRefreshAction,
} from "@/core/auth/auth-response";
import type { User } from "@/core/auth/types";

const validUser = {
  id: "10000000-0000-4000-8000-000000000001",
  email: "user@example.com",
  system_role: "user",
  needs_setup: false,
  oauth_provider: null,
} satisfies User;

describe("auth/me response classification", () => {
  test("accepts a strict authenticated user", async () => {
    await expect(
      classifyAuthMeResponse(
        new Response(JSON.stringify(validUser), { status: 200 }),
      ),
    ).resolves.toEqual({ type: "authenticated", user: validUser });
  });

  test("only classifies an explicit 401 as unauthenticated", async () => {
    await expect(
      classifyAuthMeResponse(new Response(null, { status: 401 })),
    ).resolves.toEqual({ type: "unauthenticated" });

    for (const status of [403, 429, 500, 503]) {
      await expect(
        classifyAuthMeResponse(new Response(null, { status })),
      ).resolves.toEqual({ type: "unavailable" });
    }
  });

  test("retains identity on malformed or authority-expanded 200 responses", async () => {
    await expect(
      classifyAuthMeResponse(new Response("{", { status: 200 })),
    ).resolves.toEqual({ type: "unavailable" });
    await expect(
      classifyAuthMeResponse(
        new Response(JSON.stringify({ ...validUser, project_role: "admin" }), {
          status: 200,
        }),
      ),
    ).resolves.toEqual({ type: "unavailable" });
  });

  test("keeps post-auth retry separate from authoritative logout", () => {
    expect(
      postAuthRefreshAction({ type: "authenticated", user: validUser }),
    ).toBe("complete");
    expect(postAuthRefreshAction({ type: "unauthenticated" })).toBe(
      "redirect-login",
    );
    expect(postAuthRefreshAction({ type: "unavailable" })).toBe("retry");
    expect(postAuthRefreshAction(null)).toBe("retry");
  });
});

import { beforeEach, describe, expect, test, rs } from "@rstest/core";
import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/headers", () => ({ headers: rs.fn() }));
rs.mock("next/navigation", () => ({
  redirect: rs.fn((destination: string) => {
    throw Object.assign(new Error("Redirect"), {
      code: "NEXT_REDIRECT",
      destination,
    });
  }),
}));
rs.mock("@/components/query-client-provider", () => ({
  QueryClientProvider: ({ children }: { children: React.ReactNode }) =>
    children,
}));
rs.mock("@/components/workspace/gateway-offline-fallback", () => ({
  GatewayOfflineFallback: ({ children }: { children: React.ReactNode }) =>
    children,
}));
rs.mock("@/core/auth/AuthProvider", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
}));
rs.mock("@/core/auth/server", () => ({ getServerSideUser: rs.fn() }));

import RootPage from "@/app/page";
import { WorkspaceLiveLayout } from "@/app/workspace/workspace-live-layout";
import { getServerSideUser } from "@/core/auth/server";

const authenticatedUser = {
  id: "10000000-0000-4000-8000-000000000001",
  email: "member@example.com",
  system_role: "user" as const,
  needs_setup: false,
  oauth_provider: null,
};

describe("root entry", () => {
  beforeEach(() => {
    rs.clearAllMocks();
    rs.mocked(headers).mockResolvedValue(new Headers());
  });

  test("delegates immediately to the authenticated workspace entry", () => {
    expect(() => RootPage()).toThrow("Redirect");
    expect(redirect).toHaveBeenCalledWith("/workspace");
  });

  test("sends an unauthenticated workspace request to login", async () => {
    rs.mocked(getServerSideUser).mockResolvedValue({
      tag: "unauthenticated",
    });

    await expect(
      WorkspaceLiveLayout({ children: <main>workspace</main> }),
    ).rejects.toThrow("Redirect");
    expect(redirect).toHaveBeenCalledWith("/login?next=%2Fworkspace");
  });

  test("renders the workspace for an authenticated user", async () => {
    rs.mocked(getServerSideUser).mockResolvedValue({
      tag: "authenticated",
      user: authenticatedUser,
    });

    const result = await WorkspaceLiveLayout({
      children: <main>workspace</main>,
    });

    expect(renderToStaticMarkup(result)).toBe("<main>workspace</main>");
    expect(redirect).not.toHaveBeenCalled();
  });
});

import { headers } from "next/headers";
import { redirect } from "next/navigation";

import { QueryClientProvider } from "@/components/query-client-provider";
import { GatewayOfflineFallback } from "@/components/workspace/gateway-offline-fallback";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { privateReturnPathFromHeaders } from "@/core/auth/private-return-path";
import { getServerSideUser } from "@/core/auth/server";
import { assertNever, buildLoginUrl } from "@/core/auth/types";

export async function WorkspaceLiveLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const result = await getServerSideUser();

  switch (result.tag) {
    case "authenticated":
      return (
        <QueryClientProvider>
          <AuthProvider initialUser={result.user}>{children}</AuthProvider>
        </QueryClientProvider>
      );
    case "needs_setup":
    case "system_setup_required":
      redirect("/setup");
    case "unauthenticated":
      redirect(
        buildLoginUrl(
          privateReturnPathFromHeaders(
            await headers(),
            ["/workspace"],
            "/workspace",
          ),
        ),
      );
    case "gateway_unavailable":
      return (
        <QueryClientProvider>
          <GatewayOfflineFallback renderBanner>
            {children}
          </GatewayOfflineFallback>
        </QueryClientProvider>
      );
    case "config_error":
      throw new Error(result.message);
    default:
      assertNever(result);
  }
}

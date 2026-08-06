import { redirect } from "next/navigation";

import { QueryClientProvider } from "@/components/query-client-provider";
import { GatewayOfflineFallback } from "@/components/workspace/gateway-offline-fallback";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { assertNever } from "@/core/auth/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export const dynamic = "force-dynamic";

export default async function InviteLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  if (isStaticWebsiteOnly()) redirect("/workspace");
  const result = await getServerSideUser();

  switch (result.tag) {
    case "authenticated":
      return (
        <QueryClientProvider>
          <AuthProvider initialUser={result.user}>{children}</AuthProvider>
        </QueryClientProvider>
      );
    case "unauthenticated":
      return (
        <QueryClientProvider>
          <AuthProvider initialUser={null}>{children}</AuthProvider>
        </QueryClientProvider>
      );
    case "needs_setup":
    case "system_setup_required":
      redirect("/setup");
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

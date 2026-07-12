import { redirect } from "next/navigation";
import { type ReactNode } from "react";

import { QueryClientProvider } from "@/components/query-client-provider";
import { GatewayOfflineFallback } from "@/components/workspace/gateway-offline-fallback";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { assertNever } from "@/core/auth/types";

export const dynamic = "force-dynamic";

export default async function AuthLayout({
  children,
}: {
  children: ReactNode;
}) {
  const result = await getServerSideUser();

  switch (result.tag) {
    case "authenticated":
      redirect("/workspace");
    case "needs_setup":
      // Allow access to setup page
      return (
        <QueryClientProvider>
          <AuthProvider initialUser={result.user}>{children}</AuthProvider>
        </QueryClientProvider>
      );
    case "system_setup_required":
    case "unauthenticated":
      return (
        <QueryClientProvider>
          <AuthProvider initialUser={null}>{children}</AuthProvider>
        </QueryClientProvider>
      );
    case "gateway_unavailable":
      // Auth pages have no banner of their own, so render one here. The
      // fallback's AuthProvider replaces the bare-HTML branch that
      // previously locked users out without any logout/retry capability.
      return (
        <QueryClientProvider>
          <GatewayOfflineFallback renderBanner>
            <div className="flex h-screen flex-col items-center justify-center gap-4">
              <p className="text-muted-foreground">
                Service temporarily unavailable.
              </p>
            </div>
          </GatewayOfflineFallback>
        </QueryClientProvider>
      );
    case "config_error":
      throw new Error(result.message);
    default:
      assertNever(result);
  }
}

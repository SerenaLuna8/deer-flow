import { redirect } from "next/navigation";

import { QueryClientProvider } from "@/components/query-client-provider";
import { GatewayOfflineFallback } from "@/components/workspace/gateway-offline-fallback";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { assertNever } from "@/core/auth/types";

import { WorkspaceContent } from "./workspace-content";
import { WorkspaceRouteFrame } from "./workspace-route-frame";

export const dynamic = "force-dynamic";

export default async function WorkspaceLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const result = await getServerSideUser();

  switch (result.tag) {
    case "authenticated":
      return (
        <QueryClientProvider>
          <AuthProvider initialUser={result.user}>
            <WorkspaceRouteFrame
              legacyShell={<WorkspaceContent>{children}</WorkspaceContent>}
            >
              {children}
            </WorkspaceRouteFrame>
          </AuthProvider>
        </QueryClientProvider>
      );
    case "needs_setup":
      redirect("/setup");
    case "system_setup_required":
      redirect("/setup");
    case "unauthenticated":
      redirect("/login");
    case "gateway_unavailable":
      // GatewayOfflineFallback supplies the AuthProvider. Compatibility routes
      // keep the existing offline banner inside WorkspaceContent.
      return (
        <QueryClientProvider>
          <GatewayOfflineFallback>
            <WorkspaceRouteFrame
              legacyShell={
                <WorkspaceContent gatewayUnavailable>
                  {children}
                </WorkspaceContent>
              }
            >
              {children}
            </WorkspaceRouteFrame>
          </GatewayOfflineFallback>
        </QueryClientProvider>
      );
    case "config_error":
      throw new Error(result.message);
    default:
      assertNever(result);
  }
}

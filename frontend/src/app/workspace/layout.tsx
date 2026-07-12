import { redirect } from "next/navigation";

import { QueryClientProvider } from "@/components/query-client-provider";
import { GatewayOfflineFallback } from "@/components/workspace/gateway-offline-fallback";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { assertNever } from "@/core/auth/types";

import { WorkspaceContent } from "./workspace-content";

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
            <WorkspaceContent>{children}</WorkspaceContent>
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
      // GatewayOfflineFallback supplies the AuthProvider; WorkspaceContent
      // already mounts the banner inside its sidebar layout, so renderBanner
      // stays false here to avoid double-mounting.
      return (
        <QueryClientProvider>
          <GatewayOfflineFallback>
            <WorkspaceContent gatewayUnavailable>{children}</WorkspaceContent>
          </GatewayOfflineFallback>
        </QueryClientProvider>
      );
    case "config_error":
      throw new Error(result.message);
    default:
      assertNever(result);
  }
}

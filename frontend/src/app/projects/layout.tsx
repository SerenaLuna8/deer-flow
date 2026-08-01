import { headers } from "next/headers";
import { notFound, redirect } from "next/navigation";

import { QueryClientProvider } from "@/components/query-client-provider";
import { GatewayOfflineFallback } from "@/components/workspace/gateway-offline-fallback";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { privateReturnPathFromHeaders } from "@/core/auth/private-return-path";
import { getServerSideUser } from "@/core/auth/server";
import { assertNever, buildLoginUrl } from "@/core/auth/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export const dynamic = "force-dynamic";

export default async function ProjectsLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  if (isStaticWebsiteOnly()) notFound();
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
            ["/projects"],
            "/workspace",
          ),
        ),
      );
    case "gateway_unavailable":
      return (
        <QueryClientProvider>
          <GatewayOfflineFallback renderBanner>
            <main className="flex min-h-screen items-center justify-center px-6 text-center">
              <p className="text-muted-foreground">
                项目服务暂时不可用，请稍后重试。
              </p>
            </main>
          </GatewayOfflineFallback>
        </QueryClientProvider>
      );
    case "config_error":
      throw new Error(result.message);
    default:
      assertNever(result);
  }
}

import { headers } from "next/headers";
import { notFound, redirect } from "next/navigation";

import { AdminOperationsShell } from "@/components/admin/operations/admin-operations-shell";
import { QueryClientProvider } from "@/components/query-client-provider";
import { adminReturnPathFromHeaders } from "@/core/auth/admin-return-path";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { assertNever, buildLoginUrl } from "@/core/auth/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export const dynamic = "force-dynamic";

export default async function AdminLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  if (isStaticWebsiteOnly()) notFound();

  const result = await getServerSideUser();
  switch (result.tag) {
    case "authenticated":
      if (result.user.system_role !== "system_admin") notFound();
      return (
        <QueryClientProvider>
          <AuthProvider initialUser={result.user}>
            <AdminOperationsShell>{children}</AdminOperationsShell>
          </AuthProvider>
        </QueryClientProvider>
      );
    case "needs_setup":
    case "system_setup_required":
      redirect("/setup");
    case "unauthenticated":
      redirect(buildLoginUrl(adminReturnPathFromHeaders(await headers())));
    case "gateway_unavailable":
      return (
        <main className="flex min-h-screen items-center justify-center px-6 text-center">
          <p className="text-muted-foreground">
            平台管理服务暂时不可用，请稍后重试。
          </p>
        </main>
      );
    case "config_error":
      throw new Error(result.message);
    default:
      assertNever(result);
  }
}

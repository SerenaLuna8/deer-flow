"use client";

import { ShieldCheckIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { useSystemAssetCatalog } from "@/core/shared-assets";

type LegacySystemAssetItem = {
  id: string;
  name: string;
  description: string;
};

export function LegacySystemAssetsCompatibilityView({
  kind,
  isSystemAdmin,
  items,
  isLoading = false,
  hasError = false,
}: {
  kind: "Agent" | "Skill" | "MCP";
  isSystemAdmin: boolean;
  items: LegacySystemAssetItem[];
  isLoading?: boolean;
  hasError?: boolean;
}) {
  return (
    <main className="mx-auto w-full max-w-4xl p-6 lg:p-10">
      <Card>
        <CardHeader>
          <div className="text-primary mb-2 flex items-center gap-2 text-sm font-medium">
            <ShieldCheckIcon aria-hidden className="size-4" />
            系统资产兼容视图
          </div>
          <CardTitle>系统 {kind}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-muted-foreground text-sm">
            此兼容入口只保留 PostgreSQL
            系统资产访问，不再提供基于文件的创建、编辑或启用开关。项目资产请从具体项目进入。
          </p>
          {isLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : hasError ? (
            <p role="alert" className="text-destructive text-sm">
              系统资产暂时无法加载，请稍后重试。
            </p>
          ) : items.length === 0 ? (
            <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
              暂无系统 {kind}。
            </p>
          ) : (
            <ul className="grid gap-3 sm:grid-cols-2">
              {items.map((item) => (
                <li key={item.id} className="rounded-lg border p-4">
                  <p className="font-medium">{item.name}</p>
                  {item.description && (
                    <p className="text-muted-foreground mt-1 text-sm">
                      {item.description}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
          {isSystemAdmin && (
            <Button asChild>
              <Link href="/admin/assets">前往平台资产管理</Link>
            </Button>
          )}
        </CardContent>
      </Card>
    </main>
  );
}

const CATALOG_KIND = {
  Agent: "agents",
  Skill: "skills",
  MCP: "mcp-servers",
} as const;

function LegacySystemCatalog({
  accountId,
  kind,
  isSystemAdmin,
}: {
  accountId: string;
  kind: "Agent" | "Skill" | "MCP";
  isSystemAdmin: boolean;
}) {
  const catalog = useSystemAssetCatalog(accountId, CATALOG_KIND[kind]);
  return (
    <LegacySystemAssetsCompatibilityView
      kind={kind}
      isSystemAdmin={isSystemAdmin}
      items={(catalog.data?.items ?? []).map((item) => ({
        id: item.id,
        name: item.display_name,
        description: item.slug,
      }))}
      isLoading={catalog.isLoading}
      hasError={Boolean(catalog.error)}
    />
  );
}

export function LegacySystemAssetsCompatibility({
  kind,
}: {
  kind: "Agent" | "Skill" | "MCP";
}) {
  const { user } = useAuth();
  if (!user) return null;
  return (
    <LegacySystemCatalog
      accountId={user.id}
      kind={kind}
      isSystemAdmin={user.system_role === "system_admin"}
    />
  );
}

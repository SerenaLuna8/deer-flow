import { notFound } from "next/navigation";

import { AdminProjectAssetPage } from "@/components/admin/assets/admin-project-asset-page";
import { assetIdSchema, type AssetListKind } from "@/core/shared-assets";

const ROUTE_KIND: Record<string, AssetListKind> = {
  agents: "agents",
  skills: "skills",
  mcp: "mcp-servers",
};

export default async function AdminProjectAssetKindPage({
  params,
}: {
  params: Promise<{ project_id: string; kind: string }>;
}) {
  const { project_id: projectId, kind: routeKind } = await params;
  const parsedProjectId = assetIdSchema.safeParse(projectId);
  const kind = ROUTE_KIND[routeKind];
  if (!parsedProjectId.success || !kind) notFound();
  return <AdminProjectAssetPage projectId={parsedProjectId.data} kind={kind} />;
}

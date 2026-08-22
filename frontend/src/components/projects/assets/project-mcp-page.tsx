"use client";

import {
  useProjectMcpToolInventory,
  useRequestProjectMcpToolDiscovery,
  type AssetVersion,
  type ProjectAssetItem,
} from "@/core/shared-assets";

import { McpAssetDetail } from "./mcp-asset-detail";
import { McpSecretConfiguration } from "./mcp-secret-configuration";
import type { ProjectAssetVersionRenderContext } from "./project-asset-detail-sheet";
import { ProjectAssetPageShell } from "./project-asset-page-shell";

type McpAssetVersion = Extract<AssetVersion, { mcp_server_id: string }>;

export function projectMcpCanTestService(
  item: Pick<ProjectAssetItem, "scope" | "capabilities">,
): boolean {
  return (
    item.scope === "project" &&
    item.capabilities.includes("shared_assets.edit") &&
    item.capabilities.includes("shared_assets.execute")
  );
}

function ProjectMcpVersionDetail({
  version,
  context,
}: {
  version: McpAssetVersion;
  context: ProjectAssetVersionRenderContext;
}) {
  const inventory = useProjectMcpToolInventory(
    context.accountId,
    context.projectId,
    context.item.id,
    version.id,
    version.workflow_status === "published",
  );
  const toolDiscovery = useRequestProjectMcpToolDiscovery(
    context.accountId,
    context.projectId,
  );
  const canTestService = projectMcpCanTestService(context.item);
  const exactBindingOwnedByProject =
    context.item.scope === "project" ||
    (context.item.binding?.enabled === true &&
      context.item.binding.current_version_id === version.id);

  return (
    <div className="space-y-6">
      <McpAssetDetail
        version={version}
        scope={context.item.scope}
        toolInventory={inventory.data?.data}
        toolInventoryLoading={
          !inventory.data && inventory.isFetching && !inventory.isError
        }
        toolInventoryError={inventory.error}
        toolInventoryRefreshing={inventory.isFetching}
        toolDiscoveryPending={toolDiscovery.isPending}
        toolDiscoveryError={toolDiscovery.error}
        onTestToolDiscovery={
          canTestService
            ? () =>
                toolDiscovery.mutate({
                  assetId: context.item.id,
                  versionId: version.id,
                })
            : undefined
        }
      />
      {exactBindingOwnedByProject ? (
        <McpSecretConfiguration
          accountId={context.accountId}
          projectId={context.projectId}
          assetId={context.item.id}
          versionId={version.id}
          canManage={context.item.capabilities.includes(
            "shared_assets.manage_bindings",
          )}
        />
      ) : null}
    </div>
  );
}

export function ProjectMcpPage() {
  return (
    <ProjectAssetPageShell
      kind="mcp-servers"
      title="MCP"
      renderVersion={(version, context) =>
        "mcp_server_id" in version ? (
          <ProjectMcpVersionDetail version={version} context={context} />
        ) : (
          <p role="alert" className="text-destructive text-sm">
            MCP 配置数据无效。
          </p>
        )
      }
    />
  );
}

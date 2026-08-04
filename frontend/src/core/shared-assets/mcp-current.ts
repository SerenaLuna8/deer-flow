import type { AssetScope, AssetVersion } from "./types";

type McpAssetVersion = Extract<AssetVersion, { mcp_server_id: string }>;

export type McpCurrentConfiguration =
  | { state: "ready"; version: McpAssetVersion }
  | { state: "empty" | "unconfirmed"; version: null };

export function resolveMcpCurrentConfiguration(
  versions: readonly AssetVersion[],
  scope: AssetScope,
  currentPublishedVersionId: string | null | undefined,
): McpCurrentConfiguration {
  const mcpVersions = versions.filter(
    (version): version is McpAssetVersion => "mcp_server_id" in version,
  );
  if (versions.length === 0) return { state: "empty", version: null };

  if (scope === "project") {
    const lineageParentId = currentPublishedVersionId ?? null;
    const pending = mcpVersions
      .filter(
        (version) =>
          version.workflow_status === "pending_approval" &&
          version.supersedes_version_id === lineageParentId,
      )
      .sort((left, right) => right.version_number - left.version_number)[0];
    if (pending) return { state: "ready", version: pending };
  }

  const published = mcpVersions.find(
    (version) =>
      version.id === currentPublishedVersionId &&
      version.workflow_status === "published",
  );
  return published
    ? { state: "ready", version: published }
    : { state: "unconfirmed", version: null };
}

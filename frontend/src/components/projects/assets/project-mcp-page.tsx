"use client";

import { McpAssetDetail } from "./mcp-asset-detail";
import { ProjectAssetPageShell } from "./project-asset-page-shell";

export function ProjectMcpPage() {
  return (
    <ProjectAssetPageShell
      kind="mcp-servers"
      title="MCP"
      renderVersion={(version, context) =>
        "mcp_server_id" in version ? (
          <McpAssetDetail version={version} scope={context.item.scope} />
        ) : (
          <p role="alert" className="text-destructive text-sm">
            MCP 版本数据无效。
          </p>
        )
      }
    />
  );
}

"use client";

import { McpAssetDetail } from "./mcp-asset-detail";
import { ProjectAssetPageShell } from "./project-asset-page-shell";

export function ProjectMcpPage() {
  return (
    <ProjectAssetPageShell
      kind="mcp-servers"
      title="MCP"
      description="查看 MCP 服务的连接定义、Credential 槽位与审批状态，并控制系统 MCP 在当前项目的启用版本。"
      renderVersion={(version) =>
        "mcp_server_id" in version ? (
          <McpAssetDetail version={version} />
        ) : (
          <p role="alert" className="text-destructive text-sm">
            MCP 版本数据无效。
          </p>
        )
      }
    />
  );
}

import { describe, expect, test } from "@rstest/core";

import {
  projectAssetDetailContentVersion,
  projectAssetDetailRevisionCopy,
  projectAssetDetailShowsVersionHistory,
  projectAssetDetailVersionPickerPlacement,
  projectAssetVersionPickerStatusLabel,
  projectMcpCurrentConfigurationLabel,
} from "@/components/projects/assets/project-asset-detail-sheet";
import type {
  AssetVersion,
  ProjectMcpEditableConfigurationResponse,
} from "@/core/shared-assets";

describe("Project asset detail version picker placement", () => {
  test("places the Agent picker before its editor while preserving the Skill history layout", () => {
    expect(projectAssetDetailVersionPickerPlacement("agents")).toBe(
      "before-editor",
    );
    expect(projectAssetDetailVersionPickerPlacement("skills")).toBe(
      "version-section",
    );
  });

  test("exposes independently addressable MCP configuration versions", () => {
    expect(
      projectAssetDetailShowsVersionHistory("mcp-servers", "project"),
    ).toBe(true);
    expect(projectAssetDetailShowsVersionHistory("mcp-servers", "system")).toBe(
      false,
    );
    expect(projectAssetDetailRevisionCopy("mcp-servers").label(3)).toBe(
      "配置 3",
    );
  });

  test("distinguishes the current MCP configuration from retained published versions", () => {
    const retained = {
      id: "retained-version",
      mcp_server_id: "mcp-id",
      workflow_status: "published",
    } as AssetVersion;
    const current = { ...retained, id: "current-version" } as AssetVersion;

    expect(
      projectAssetVersionPickerStatusLabel(
        "mcp-servers",
        retained,
        "current-version",
      ),
    ).toBe("已发布");
    expect(
      projectAssetVersionPickerStatusLabel(
        "mcp-servers",
        current,
        "current-version",
      ),
    ).toBe("当前配置");
    expect(
      projectMcpCurrentConfigurationLabel({
        version_number: 2,
        workflow_status: "published",
      }),
    ).toBe("配置 2 · 已发布");
  });

  test("does not replace a retained MCP selection with the current editable configuration", () => {
    const retained = { id: "retained-version" } as AssetVersion;
    const editable = {
      version: { id: "current-version" },
    } as ProjectMcpEditableConfigurationResponse;

    expect(
      projectAssetDetailContentVersion("mcp-servers", retained, editable, true),
    ).toBe(retained);
  });
});

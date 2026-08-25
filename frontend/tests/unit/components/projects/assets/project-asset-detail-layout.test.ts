import { describe, expect, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectAssetDetailHeader,
  projectAssetDetailContentVersion,
  projectAssetDetailShowsLifecycleControls,
  projectAssetDetailRevisionCopy,
  projectAssetDetailShowsVersionHistory,
  projectAssetDetailVersionPickerPlacement,
  projectAssetVersionPickerStatusLabel,
  projectMcpCurrentConfigurationLabel,
} from "@/components/projects/assets/project-asset-detail-sheet";
import { Sheet } from "@/components/ui/sheet";
import type {
  AssetVersion,
  ProjectAssetItem,
  ProjectMcpEditableConfigurationResponse,
} from "@/core/shared-assets";

describe("Project asset detail version picker placement", () => {
  test("keeps Skill status controls on the catalog instead of the detail surface", () => {
    const item = {
      capabilities: ["shared_assets.edit", "shared_assets.manage_bindings"],
      current_version_id: "version-2",
      display_name: "trans-text-route-query",
      scope: "project",
      status: "active",
    } as ProjectAssetItem;

    expect(projectAssetDetailShowsLifecycleControls("skills")).toBe(false);
    expect(
      renderToStaticMarkup(
        createElement(
          Sheet,
          { open: true },
          createElement(ProjectAssetDetailHeader, {
            kind: "skills",
            item,
          }),
        ),
      ),
    ).not.toContain('role="switch"');
  });

  test("shows the single Agent Definition without a version history picker", () => {
    expect(projectAssetDetailShowsVersionHistory("agents", "project")).toBe(
      false,
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

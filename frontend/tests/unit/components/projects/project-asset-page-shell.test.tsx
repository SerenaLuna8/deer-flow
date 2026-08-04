import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  createProjectAgentDeleteSnapshot,
  createProjectMcpDeleteSnapshot,
  createProjectSkillDeleteSnapshot,
  projectAssetEditBaseVersion,
  projectAssetDetailContentVersion,
  projectAssetDetailPreferredVersionId,
  projectAssetDetailShowsVersionHistory,
  projectAssetDetailSummaryGridColumns,
  projectAssetDiscardCopy,
  projectAssetDetailRevisionCopy,
  projectAssetDetailVersionTerms,
  projectAssetLifecycleActionLabel,
  projectMcpCurrentConfigurationLabel,
  projectMcpEditableConfigurationEnabled,
  projectMcpSystemUsageLabel,
  ProjectAssetDetailHeader,
  ProjectMcpDangerZone,
  ProjectSkillDetailActions,
  versionActionDisabled,
  versionPublishDisabled,
} from "@/components/projects/assets/project-asset-detail-sheet";
import {
  closeCompletedVersionDialog,
  configuredMcpSelectionReady,
  configuredMcpSuccessMessage,
  createdProjectAssetSelectionReady,
  createdSkillSelectionReady,
  defaultProjectAssetSource,
  filterProjectAssetItems,
  handleRequestedVersion,
  importedSkillSelectionReady,
  projectAssetSourceOptions,
  projectAssetEmptyMessage,
  projectAssetPrimaryActionLabel,
  ProjectAssetListView,
  projectSystemBindingListAction,
  rememberRequestedVersion,
  systemBindingToggleState,
  systemMcpBindingNeedsUpdate,
  versionDialogSubmissionMatches,
} from "@/components/projects/assets/project-asset-page-shell";
import {
  projectAssetCreateErrorMessage,
  projectConfiguredMcpErrorMessage,
  projectAssetCanDelete,
  projectAssetDetailLifecycleActions,
  projectMcpDeleteErrorMessage,
  projectMcpStatusToggleState,
  projectSkillStatusToggleState,
} from "@/components/projects/assets/project-asset-view-model";
import {
  ProjectAgentDeleteConfirmation,
  ProjectMcpDeleteConfirmation,
  ProjectSkillDeleteConfirmation,
  skillDeleteSecondsRemaining,
} from "@/components/projects/assets/project-skill-delete-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Sheet } from "@/components/ui/sheet";
import {
  resolveMcpCurrentConfiguration,
  SharedAssetApiError,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

const PROJECT_ID = "33333333-3333-4333-8333-333333333333";
const SYSTEM_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ASSET_ID = "22222222-2222-4222-8222-222222222222";
const LATEST_VERSION_ID = "44444444-4444-4444-8444-444444444444";
const PINNED_VERSION_ID = "55555555-5555-4555-8555-555555555555";

const catalog: ProjectAssetList = {
  system_items: [
    {
      id: SYSTEM_ID,
      scope: "system",
      project_id: null,
      slug: "research-agent",
      display_name: "Research Agent",
      description:
        "Review, analyze, critique, and summarize academic papers with structured methodology assessment and constructive feedback.",
      status: "active",
      current_published_version_id: LATEST_VERSION_ID,
      version: 19,
      created_by_user_id: "system",
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-21T00:00:00Z",
      capabilities: [
        "shared_assets.read",
        "shared_assets.execute",
        "shared_assets.manage_bindings",
      ],
      binding: {
        project_id: PROJECT_ID,
        kind: "agent",
        asset_id: SYSTEM_ID,
        version_id: PINNED_VERSION_ID,
        enabled: true,
        version: 7,
        created_by_user_id: "admin",
        updated_by_user_id: "admin",
        created_at: "2026-07-20T00:00:00Z",
        updated_at: "2026-07-21T00:00:00Z",
      },
    },
  ],
  project_items: [
    {
      id: PROJECT_ASSET_ID,
      scope: "project",
      project_id: PROJECT_ID,
      slug: "project-agent",
      display_name: "Project Agent",
      status: "active",
      current_published_version_id: null,
      version: 23,
      created_by_user_id: "editor",
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-21T00:00:00Z",
      capabilities: ["shared_assets.read", "shared_assets.edit"],
      binding: null,
    },
  ],
  request_id: "request-assets",
};

describe("project asset list", () => {
  test("uses one add action and clear success states for configured MCP creation", () => {
    const selection = {
      assetId: PROJECT_ASSET_ID,
      versionId: LATEST_VERSION_ID,
    };

    expect(projectAssetPrimaryActionLabel("mcp-servers")).toBe("添加 MCP");
    expect(projectAssetPrimaryActionLabel("skills")).toBe("新建 Skill");
    expect(projectAssetEmptyMessage("mcp-servers", "project")).toBe(
      "尚未添加项目 MCP。",
    );
    expect(configuredMcpSuccessMessage("published")).toContain("已添加");
    expect(configuredMcpSuccessMessage("published")).toContain("已添加并发布");
    expect(configuredMcpSuccessMessage("pending_approval")).toContain(
      "凭据尚未绑定",
    );
    expect(configuredMcpSuccessMessage("pending_approval")).not.toContain(
      "审批",
    );
    expect(configuredMcpSelectionReady(catalog, selection)).toEqual(selection);
    expect(
      configuredMcpSelectionReady({ ...catalog, project_items: [] }, selection),
    ).toBeNull();
  });

  test("uses configuration terms only for MCP details", () => {
    expect(projectAssetDetailVersionTerms("mcp-servers", "project")).toEqual({
      current: "当前配置",
      history: "",
      edit: "编辑配置",
      empty: "尚未保存配置。",
    });
    expect(projectAssetDetailVersionTerms("skills", "project")).toEqual({
      current: "当前发布",
      history: "版本",
      edit: "创建新版本",
      empty: "尚未创建版本。",
    });
    const mcpRevision = projectAssetDetailRevisionCopy("mcp-servers");
    const skillRevision = projectAssetDetailRevisionCopy("skills");
    expect(mcpRevision.label(3)).toBe("配置");
    expect(mcpRevision.publish).toBe("发布配置");
    expect(mcpRevision).not.toHaveProperty("approve");
    expect(mcpRevision.technical).toBe("配置技术信息");
    expect(
      [
        mcpRevision.label(3),
        mcpRevision.publishedFallback,
        mcpRevision.pinnedFallback,
        mcpRevision.updateAvailable,
        mcpRevision.viewAria,
        mcpRevision.loading,
        mcpRevision.publish,
        mcpRevision.technical,
      ].join(" "),
    ).not.toContain("版本");
    expect(skillRevision.label(3)).toBe("版本 3");
    expect(skillRevision.publish).toBe("发布版本");
    expect(skillRevision.technical).toBe("版本技术信息");
    expect(projectAssetDiscardCopy("mcp-servers")).toEqual({
      title: "放弃未保存的配置修改？",
      description: "关闭详情会清除当前编辑副本，已保存的 MCP 配置不会受影响。",
    });
    expect(
      Object.values(projectAssetDiscardCopy("mcp-servers")).join(" "),
    ).not.toContain("版本");
    expect(projectAssetDiscardCopy("skills").title).toBe(
      "放弃未保存的文件修改？",
    );
  });

  test("loads and uses only the successful editable Project MCP projection", () => {
    const editableItem: ProjectAssetItem = {
      ...catalog.project_items[0]!,
      capabilities: ["shared_assets.read", "shared_assets.edit"],
    };
    expect(
      projectMcpEditableConfigurationEnabled(true, "mcp-servers", editableItem),
    ).toBe(true);
    expect(
      projectMcpEditableConfigurationEnabled(
        false,
        "mcp-servers",
        editableItem,
      ),
    ).toBe(false);
    expect(
      projectMcpEditableConfigurationEnabled(true, "skills", editableItem),
    ).toBe(false);
    expect(
      projectMcpEditableConfigurationEnabled(true, "mcp-servers", {
        ...editableItem,
        scope: "system",
      }),
    ).toBe(false);
    expect(
      projectMcpEditableConfigurationEnabled(true, "mcp-servers", {
        ...editableItem,
        capabilities: ["shared_assets.read"],
      }),
    ).toBe(false);

    const historyVersion = {
      id: LATEST_VERSION_ID,
      definition: { url: "http://127.0.0.1:8771" },
    } as AssetVersion;
    const editableVersion = {
      ...historyVersion,
      definition: { url: "http://127.0.0.1:8771/api/mcp" },
    } as AssetVersion;
    const editableResponse = {
      version: editableVersion,
    } as never;

    expect(
      projectAssetEditBaseVersion(
        "mcp-servers",
        historyVersion,
        editableResponse,
        true,
      ),
    ).toBe(editableVersion);
    expect(
      projectAssetEditBaseVersion(
        "mcp-servers",
        historyVersion,
        editableResponse,
        false,
      ),
    ).toBeNull();
    expect(
      projectAssetDetailContentVersion(
        "mcp-servers",
        historyVersion,
        editableResponse,
        true,
      ),
    ).toBe(editableVersion);
    expect(
      projectAssetDetailContentVersion(
        "mcp-servers",
        historyVersion,
        editableResponse,
        false,
      ),
    ).toBe(historyVersion);
  });

  test("explains configured MCP conflicts and list failures without raw detail", () => {
    const conflict = new SharedAssetApiError(
      409,
      "ASSET_CONFLICT",
      "private backend detail",
    );
    const message = projectConfiguredMcpErrorMessage(conflict);

    expect(message).toContain("已存在");
    expect(message).toContain("标识");
    expect(message).not.toContain("private backend detail");
  });

  test("hides system-provided Agents without removing other system asset sources", () => {
    expect(projectAssetSourceOptions("agents")).toEqual([
      ["project", "项目自建"],
    ]);
    expect(projectAssetSourceOptions("skills")).toEqual([
      ["system", "系统提供"],
      ["project", "项目自建"],
    ]);
    expect(projectAssetSourceOptions("mcp-servers")).toEqual([
      ["system", "系统提供"],
      ["project", "项目自建"],
    ]);
  });

  test("defaults to the useful source when one tab is empty", () => {
    expect(defaultProjectAssetSource(catalog)).toBe("project");
    expect(
      defaultProjectAssetSource({
        system_items: catalog.system_items,
        project_items: [],
      }),
    ).toBe("system");
    expect(
      defaultProjectAssetSource({ system_items: [], project_items: [] }),
    ).toBe("project");
  });

  test("filters only the active source tab by display name or slug", () => {
    expect(
      filterProjectAssetItems(catalog, "research", "system").map(
        (item) => item.id,
      ),
    ).toEqual([SYSTEM_ID]);
    expect(filterProjectAssetItems(catalog, "research", "project")).toEqual([]);

    expect(
      filterProjectAssetItems(catalog, "PROJECT-AGENT", "project").map(
        (item) => item.id,
      ),
    ).toEqual([PROJECT_ASSET_ID]);
  });

  test("renders one scoped panel without repeating the source label on every row", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="agents"
        data={catalog}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );

    expect(html).toContain("Research Agent");
    expect(html).not.toContain("Project Agent");
    expect(html).not.toContain(">系统提供<");
    expect(html).not.toContain(">项目自建<");
    expect(html).toContain('role="switch"');
    expect(html).toContain("停用 Research Agent");
  });

  test("renders Skill descriptions with a direct binding switch while preserving detail access", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="skills"
        data={catalog}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );

    expect(html).toContain(
      "Review, analyze, critique, and summarize academic papers",
    );
    expect(html).toContain("查看 Research Agent 详情");
    expect(html).toContain('role="switch"');
    expect(html).toContain("停用 Research Agent");
    expect(html).not.toContain("已有发布版本");
    expect(html).not.toContain(
      new Date(catalog.system_items[0]!.updated_at).toLocaleDateString("zh-CN"),
    );
  });

  test("keeps system MCP enablement on the list with an inline current-config update", () => {
    const systemMcp = {
      ...catalog.system_items[0]!,
      binding: {
        ...catalog.system_items[0]!.binding!,
        kind: "mcp" as const,
      },
    };
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="mcp-servers"
        data={{ ...catalog, system_items: [systemMcp] }}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
        onSyncSystemMcpBinding={() => undefined}
      />,
    );

    expect(html).toContain('role="switch"');
    expect(html).toContain("停用 Research Agent");
    expect(html).toContain(">更新<");
    expect(html).toContain("查看 Research Agent 详情");
    expect(html).not.toContain("管理绑定");
    expect(html).not.toContain("有新配置");
    expect(html).not.toContain(
      new Date(systemMcp.updated_at).toLocaleDateString("zh-CN"),
    );
    expect(html).not.toContain("lucide-arrow-right");
  });

  test("renders project MCP with the same direct enable switch and compact row", () => {
    const projectMcp: ProjectAssetItem = {
      ...catalog.project_items[0]!,
      display_name: "Transfer MCP",
      slug: "transfer-mcp",
      status: "active",
      current_published_version_id: LATEST_VERSION_ID,
      capabilities: [
        "shared_assets.read",
        "shared_assets.edit",
        "shared_assets.manage_bindings",
      ],
    };
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="mcp-servers"
        data={{ ...catalog, project_items: [projectMcp] }}
        source="project"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
        onToggleProjectAssetStatus={() => undefined}
      />,
    );

    expect(html).toContain("查看 Transfer MCP 详情");
    expect(html).toContain('role="switch"');
    expect(html).toContain("停用 Transfer MCP");
    expect(html).not.toContain("已有发布配置");
    expect(html).not.toContain(
      new Date(projectMcp.updated_at).toLocaleDateString("zh-CN"),
    );
    expect(html).not.toContain("lucide-arrow-right");
  });

  test("maps project MCP lifecycle to a reversible enable switch", () => {
    const projectMcp: ProjectAssetItem = {
      ...catalog.project_items[0]!,
      scope: "project",
      status: "active",
      current_published_version_id: LATEST_VERSION_ID,
      capabilities: [
        "shared_assets.read",
        "shared_assets.edit",
        "shared_assets.manage_bindings",
      ],
    };

    expect(projectMcpStatusToggleState(projectMcp)).toEqual({
      checked: true,
      disabled: false,
      disabledReason: null,
    });
    expect(
      projectMcpStatusToggleState({ ...projectMcp, status: "suspended" }),
    ).toEqual({
      checked: false,
      disabled: false,
      disabledReason: null,
    });
    expect(
      projectMcpStatusToggleState({
        ...projectMcp,
        status: "suspended",
        current_published_version_id: null,
      }),
    ).toEqual({
      checked: false,
      disabled: true,
      disabledReason: "请先发布配置",
    });
  });

  test("renders an unbound system MCP as a directly enabled list switch", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="mcp-servers"
        data={{
          ...catalog,
          system_items: [{ ...catalog.system_items[0]!, binding: null }],
        }}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
        onSyncSystemMcpBinding={() => undefined}
      />,
    );

    expect(html).toContain('role="switch"');
    expect(html).toContain("启用 Research Agent");
    expect(html).not.toContain('disabled=""');
    expect(html).not.toContain("管理绑定");
  });

  test("keeps MCP list binding pending and conflict feedback on its row", () => {
    const optimisticDisable = renderToStaticMarkup(
      <ProjectAssetListView
        kind="mcp-servers"
        data={catalog}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
        onSyncSystemMcpBinding={() => undefined}
        bindingIntent={{ assetId: SYSTEM_ID, checked: false }}
      />,
    );
    expect(optimisticDisable).toContain('aria-busy="true"');
    expect(optimisticDisable).toContain('disabled=""');
    expect(optimisticDisable).toContain("启用 Research Agent");

    const conflict = renderToStaticMarkup(
      <ProjectAssetListView
        kind="mcp-servers"
        data={catalog}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
        onSyncSystemMcpBinding={() => undefined}
        bindingErrorAssetId={SYSTEM_ID}
        bindingError={
          new SharedAssetApiError(409, "ASSET_CONFLICT", "private raw")
        }
      />,
    );
    expect(conflict).toContain('role="alert"');
    expect(conflict).toContain("状态已变化");
    expect(conflict).not.toContain("private raw");
  });

  test("disables MCP enablement when there is no published configuration", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="mcp-servers"
        data={{
          ...catalog,
          system_items: [
            {
              ...catalog.system_items[0]!,
              binding: null,
              current_published_version_id: null,
            },
          ],
        }}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
        onSyncSystemMcpBinding={() => undefined}
      />,
    );

    expect(html).toContain('disabled=""');
    expect(html).toContain("没有可启用的已发布配置");
  });

  test("keeps MCP and Agent revisions internal while preserving Skill history", () => {
    expect(projectAssetDetailShowsVersionHistory("agents")).toBe(false);
    expect(projectAssetDetailShowsVersionHistory("skills")).toBe(true);
    expect(projectAssetDetailShowsVersionHistory("mcp-servers")).toBe(false);
  });

  test("shows the latest project MCP configuration without exposing its revision", () => {
    const latestPendingId = "66666666-6666-4666-8666-666666666666";
    const stalePendingId = "88888888-8888-4888-8888-888888888888";
    const priorPublishedId = "99999999-9999-4999-8999-999999999999";

    expect(
      projectAssetDetailPreferredVersionId(
        "mcp-servers",
        "project",
        [
          {
            id: "77777777-7777-4777-8777-777777777777",
            mcp_server_id: PROJECT_ASSET_ID,
            version_number: 4,
            workflow_status: "pending_approval",
            supersedes_version_id: LATEST_VERSION_ID,
          },
          {
            id: latestPendingId,
            mcp_server_id: PROJECT_ASSET_ID,
            version_number: 6,
            workflow_status: "pending_approval",
            supersedes_version_id: LATEST_VERSION_ID,
          },
          {
            id: stalePendingId,
            mcp_server_id: PROJECT_ASSET_ID,
            version_number: 7,
            workflow_status: "pending_approval",
            supersedes_version_id: priorPublishedId,
          },
          {
            id: LATEST_VERSION_ID,
            mcp_server_id: PROJECT_ASSET_ID,
            version_number: 5,
            workflow_status: "published",
            supersedes_version_id: priorPublishedId,
          },
        ] as never,
        LATEST_VERSION_ID,
      ),
    ).toBe(latestPendingId);
    expect(
      projectAssetDetailPreferredVersionId(
        "mcp-servers",
        "project",
        [
          {
            id: stalePendingId,
            mcp_server_id: PROJECT_ASSET_ID,
            version_number: 7,
            workflow_status: "pending_approval",
            supersedes_version_id: priorPublishedId,
          },
          {
            id: LATEST_VERSION_ID,
            mcp_server_id: PROJECT_ASSET_ID,
            version_number: 5,
            workflow_status: "published",
            supersedes_version_id: priorPublishedId,
          },
        ] as never,
        LATEST_VERSION_ID,
      ),
    ).toBe(LATEST_VERSION_ID);
    expect(
      projectAssetDetailPreferredVersionId(
        "skills",
        "project",
        [{ id: latestPendingId }, { id: LATEST_VERSION_ID }],
        LATEST_VERSION_ID,
      ),
    ).toBe(LATEST_VERSION_ID);
    expect(
      projectAssetDetailPreferredVersionId(
        "mcp-servers",
        "system",
        [
          {
            id: LATEST_VERSION_ID,
            mcp_server_id: SYSTEM_ID,
            version_number: 5,
            workflow_status: "published",
          },
        ] as never,
        null,
      ),
    ).toBe("");
    expect(
      resolveMcpCurrentConfiguration(
        [
          {
            id: LATEST_VERSION_ID,
            mcp_server_id: PROJECT_ASSET_ID,
            version_number: 5,
            workflow_status: "published",
          },
        ] as never,
        "project",
        priorPublishedId,
      ).state,
    ).toBe("unconfirmed");
    expect(projectMcpCurrentConfigurationLabel("pending_approval")).toBe(
      "凭据未绑定 · 尚未生效",
    );
    expect(projectMcpCurrentConfigurationLabel("published")).toBe("已发布");
  });

  test("summarizes system MCP use without configuration numbers", () => {
    const systemMcp = catalog.system_items[0]!;

    expect(projectMcpSystemUsageLabel({ ...systemMcp, binding: null })).toBe(
      "未启用",
    );
    expect(
      projectMcpSystemUsageLabel({
        ...systemMcp,
        binding: { ...systemMcp.binding!, enabled: false },
      }),
    ).toBe("未启用");
    expect(projectMcpSystemUsageLabel(systemMcp)).toBe("有配置更新");
    expect(
      projectMcpSystemUsageLabel({
        ...systemMcp,
        binding: {
          ...systemMcp.binding!,
          version_id: systemMcp.current_published_version_id!,
        },
      }),
    ).toBe("已启用");
  });

  test("places MCP configuration, update time and lifecycle status in one row", () => {
    expect(projectAssetDetailSummaryGridColumns("skills", "system")).toBe(
      "sm:grid-cols-3",
    );
    expect(projectAssetDetailSummaryGridColumns("skills", "project")).toBe(
      "sm:grid-cols-2",
    );
    expect(projectAssetDetailSummaryGridColumns("mcp-servers", "system")).toBe(
      "sm:grid-cols-3",
    );
    expect(projectAssetDetailSummaryGridColumns("mcp-servers", "project")).toBe(
      "sm:grid-cols-3",
    );
  });

  test("renders a project Skill enable switch and keeps detail access independent", () => {
    const suspendedSkill: ProjectAssetItem = {
      ...catalog.project_items[0]!,
      display_name: "Meeting Brief",
      status: "suspended" as const,
      current_published_version_id: LATEST_VERSION_ID,
      capabilities: [
        "shared_assets.read",
        "shared_assets.edit",
        "shared_assets.manage_bindings",
      ],
    };
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="skills"
        data={{ ...catalog, project_items: [suspendedSkill] }}
        source="project"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
        onToggleProjectAssetStatus={() => undefined}
      />,
    );

    expect(html).toContain("查看 Meeting Brief 详情");
    expect(html).toContain('role="switch"');
    expect(html).toContain("启用 Meeting Brief");
    expect(html).not.toContain("已暂停");
    expect(html).not.toContain(">暂停<");
  });

  test("keeps project MCP ownership and lifecycle status out of the detail header", () => {
    const projectMcp: ProjectAssetItem = {
      ...catalog.project_items[0]!,
      status: "active",
    };
    const html = renderToStaticMarkup(
      <Sheet open>
        <ProjectAssetDetailHeader
          kind="mcp-servers"
          item={projectMcp}
          statusPending={false}
          onToggleProjectSkillStatus={() => undefined}
        />
      </Sheet>,
    );

    expect(html).toContain(projectMcp.display_name);
    expect(html).toContain(projectMcp.slug);
    expect(html).not.toContain("项目自建");
    expect(html).not.toContain(">启用<");
  });

  test("blocks enabling an unpublished project Skill with a publish-first hint", () => {
    const unpublishedSkill: ProjectAssetItem = {
      ...catalog.project_items[0]!,
      display_name: "Meeting Brief",
      status: "suspended" as const,
      capabilities: [
        "shared_assets.read",
        "shared_assets.edit",
        "shared_assets.manage_bindings",
      ],
    };
    const state = projectSkillStatusToggleState(unpublishedSkill);
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="skills"
        data={{ ...catalog, project_items: [unpublishedSkill] }}
        source="project"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
        onToggleProjectAssetStatus={() => undefined}
      />,
    );

    expect(state).toEqual({
      checked: false,
      disabled: true,
      disabledReason: "请先发布版本",
    });
    expect(html).toContain("请先发布版本");
    expect(html).toContain('disabled=""');
  });

  test("maps active and published suspended project Skills to reversible switches", () => {
    const projectSkill: ProjectAssetItem = {
      ...catalog.project_items[0]!,
      current_published_version_id: LATEST_VERSION_ID,
      capabilities: [
        "shared_assets.read",
        "shared_assets.edit",
        "shared_assets.manage_bindings",
      ],
    };

    expect(projectSkillStatusToggleState(projectSkill)).toEqual({
      checked: true,
      disabled: false,
      disabledReason: null,
    });
    expect(
      projectSkillStatusToggleState({
        ...projectSkill,
        status: "suspended",
      }),
    ).toEqual({
      checked: false,
      disabled: false,
      disabledReason: null,
    });
  });

  test("keeps the Skill detail header minimal with the same status switch", () => {
    const projectSkill: ProjectAssetItem = {
      ...catalog.project_items[0]!,
      display_name: "Meeting Brief",
      slug: "meeting-brief",
      status: "suspended" as const,
      current_published_version_id: LATEST_VERSION_ID,
      capabilities: [
        "shared_assets.read",
        "shared_assets.edit",
        "shared_assets.manage_bindings",
      ],
    };
    const html = renderToStaticMarkup(
      <Sheet open>
        <ProjectAssetDetailHeader
          kind="skills"
          item={projectSkill}
          statusPending={false}
          onToggleProjectSkillStatus={() => undefined}
        />
      </Sheet>,
    );

    expect(html).toContain("Meeting Brief");
    expect(html).toContain("启用 Meeting Brief");
    expect(html).not.toContain("meeting-brief");
    expect(html).not.toContain("项目自建");
    expect(html).not.toContain("已暂停");
  });

  test("shows the Agent update time under its title instead of repeating the slug", () => {
    const projectAgent = catalog.project_items[0]!;
    const html = renderToStaticMarkup(
      <Sheet open>
        <ProjectAssetDetailHeader
          kind="agents"
          item={projectAgent}
          statusPending={false}
          onToggleProjectSkillStatus={() => undefined}
        />
      </Sheet>,
    );

    expect(html).toContain(projectAgent.display_name);
    expect(html).toContain(`dateTime="${projectAgent.updated_at}"`);
    expect(html).toContain(
      new Date(projectAgent.updated_at).toLocaleString("zh-CN"),
    );
    expect(html).not.toContain(projectAgent.slug);
    expect(html).not.toContain("最近更新");
  });

  test("places the selected-version editor immediately before permanent Skill deletion", () => {
    const html = renderToStaticMarkup(
      <ProjectSkillDetailActions
        actionPending={false}
        canAuthor
        canDelete
        editing={false}
        hasSelectedVersion
        versionDirty={false}
        versionSelectionPending={false}
        onCreateVersion={() => undefined}
        onDelete={() => undefined}
      />,
    );

    const createVersionIndex = html.indexOf("创建新版本");
    const deleteSkillIndex = html.indexOf("删除 Skill");
    expect(createVersionIndex).toBeGreaterThanOrEqual(0);
    expect(deleteSkillIndex).toBeGreaterThan(createVersionIndex);
    expect(html).not.toContain("从空白创建");
    expect(html).not.toContain("编辑为新版本");

    const editingHtml = renderToStaticMarkup(
      <ProjectSkillDetailActions
        actionPending={false}
        canAuthor
        canDelete
        editing
        hasSelectedVersion
        versionDirty={false}
        versionSelectionPending={false}
        onCreateVersion={() => undefined}
        onDelete={() => undefined}
      />,
    );
    expect(editingHtml).not.toContain("创建新版本");
    expect(editingHtml).toContain("删除 Skill");
  });

  test("does not present active project lifecycle as a fake enable control", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="agents"
        data={catalog}
        source="project"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );

    expect(html).toContain("Project Agent");
    expect(html).toContain("尚未发布");
    expect(html).not.toContain('role="switch"');
    expect(html).not.toContain(">启用<");
  });

  test("derives safe system binding switch targets without changing lifecycle semantics", () => {
    expect(systemBindingToggleState(catalog.system_items[0]!)).toEqual({
      checked: true,
      disabled: false,
      targetVersionId: PINNED_VERSION_ID,
    });

    expect(
      systemBindingToggleState({
        ...catalog.system_items[0]!,
        binding: null,
      }),
    ).toEqual({
      checked: false,
      disabled: false,
      targetVersionId: LATEST_VERSION_ID,
    });

    expect(
      systemBindingToggleState({
        ...catalog.system_items[0]!,
        binding: null,
        current_published_version_id: null,
      }),
    ).toEqual({
      checked: false,
      disabled: true,
      targetVersionId: null,
    });

    expect(systemBindingToggleState(catalog.project_items[0]!)).toEqual({
      checked: false,
      disabled: true,
      targetVersionId: null,
    });
  });

  test("derives server-authoritative MCP list binding mutations", () => {
    const systemMcp = {
      ...catalog.system_items[0]!,
      binding: {
        ...catalog.system_items[0]!.binding!,
        kind: "mcp" as const,
      },
    };
    const unbound = { ...systemMcp, binding: null };
    const disabled = {
      ...systemMcp,
      binding: { ...systemMcp.binding, enabled: false },
    };
    const current = {
      ...systemMcp,
      binding: {
        ...systemMcp.binding,
        version_id: systemMcp.current_published_version_id!,
      },
    };

    expect(
      projectSystemBindingListAction("mcp-servers", unbound, true),
    ).toEqual({
      type: "sync-current",
      assetId: SYSTEM_ID,
      input: {},
    });
    expect(
      projectSystemBindingListAction("mcp-servers", disabled, true),
    ).toEqual({
      type: "sync-current",
      assetId: SYSTEM_ID,
      input: { expected_binding_version: 7 },
    });
    expect(
      projectSystemBindingListAction("mcp-servers", current, true),
    ).toBeNull();
    expect(
      projectSystemBindingListAction("mcp-servers", systemMcp, true, true),
    ).toEqual({
      type: "sync-current",
      assetId: SYSTEM_ID,
      input: { expected_binding_version: 7 },
    });
    expect(
      projectSystemBindingListAction("mcp-servers", systemMcp, false),
    ).toEqual({
      type: "disable",
      assetId: SYSTEM_ID,
      input: { expected_binding_version: 7 },
    });
    expect(
      projectSystemBindingListAction(
        "mcp-servers",
        { ...unbound, current_published_version_id: null },
        true,
      ),
    ).toBeNull();
    expect(systemMcpBindingNeedsUpdate(systemMcp)).toBe(true);
    expect(systemMcpBindingNeedsUpdate(current)).toBe(false);
  });

  test("does not present optimistic revisions or UUID pointers as content versions", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="agents"
        data={catalog}
        source="system"
        selectedAssetId={SYSTEM_ID}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );

    expect(html).not.toContain("资产版本");
    expect(html).not.toContain("绑定修订版本");
    expect(html).not.toContain(LATEST_VERSION_ID);
    expect(html).not.toContain(PINNED_VERSION_ID);
    expect(html).not.toContain(">19<");
    expect(html).not.toContain(">7<");
  });

  test("a late version response cannot close a reopened dialog for the same asset", () => {
    const first = catalog.system_items[0]!;
    const second = catalog.project_items[0]!;
    const firstSubmission = { assetId: first.id, generation: 1 };
    const reopenedFirst = { assetId: first.id, generation: 2 };
    const secondSubmission = { assetId: second.id, generation: 3 };

    expect(versionDialogSubmissionMatches(reopenedFirst, firstSubmission)).toBe(
      false,
    );
    expect(
      closeCompletedVersionDialog(first, reopenedFirst, firstSubmission),
    ).toBe(first);
    expect(
      closeCompletedVersionDialog(first, reopenedFirst, reopenedFirst),
    ).toBeNull();
    expect(
      closeCompletedVersionDialog(second, secondSubmission, firstSubmission),
    ).toBe(second);
    expect(
      closeCompletedVersionDialog(null, reopenedFirst, reopenedFirst),
    ).toBeNull();
  });

  test("blocks every old-version action while the requested draft is pending", () => {
    expect(versionPublishDisabled(false, true)).toBe(true);
    expect(versionPublishDisabled(false, false, true)).toBe(true);
    expect(versionPublishDisabled(true, false)).toBe(true);
    expect(versionPublishDisabled(false, false)).toBe(false);
    expect(versionActionDisabled(false, true)).toBe(true);
    expect(versionActionDisabled(true, false)).toBe(true);
    expect(versionActionDisabled(false, false)).toBe(false);
  });

  test("keeps a forked draft request outside the detail sheet until history handles it", () => {
    const firstVersion = "77777777-7777-4777-8777-777777777777";
    const newerVersion = "88888888-8888-4888-8888-888888888888";
    const requested = rememberRequestedVersion(
      {},
      PROJECT_ASSET_ID,
      firstVersion,
    );
    const superseded = rememberRequestedVersion(
      requested,
      PROJECT_ASSET_ID,
      newerVersion,
    );

    expect(superseded).toEqual({ [PROJECT_ASSET_ID]: newerVersion });
    expect(
      handleRequestedVersion(superseded, PROJECT_ASSET_ID, firstVersion),
    ).toBe(superseded);
    expect(
      handleRequestedVersion(superseded, PROJECT_ASSET_ID, newerVersion),
    ).toEqual({});
  });

  test("waits for the refreshed project Skill list before opening an imported version", () => {
    const selection = {
      assetId: PROJECT_ASSET_ID,
      versionId: LATEST_VERSION_ID,
    };

    expect(
      importedSkillSelectionReady(
        { system_items: catalog.system_items, project_items: [] },
        selection,
      ),
    ).toBeNull();
    expect(importedSkillSelectionReady(catalog, selection)).toEqual(selection);
    expect(importedSkillSelectionReady(catalog, null)).toBeNull();
  });

  test("waits for the refreshed project Agent or Skill list before opening a newly created asset", () => {
    expect(
      createdProjectAssetSelectionReady(
        { project_items: [] },
        PROJECT_ASSET_ID,
      ),
    ).toBeNull();
    expect(createdProjectAssetSelectionReady(catalog, PROJECT_ASSET_ID)).toBe(
      PROJECT_ASSET_ID,
    );
    expect(createdProjectAssetSelectionReady(catalog, null)).toBeNull();
    expect(createdSkillSelectionReady(catalog, PROJECT_ASSET_ID)).toBe(
      PROJECT_ASSET_ID,
    );
  });

  test("explains same-project name conflicts only for project Skill list creation", () => {
    const conflict = new SharedAssetApiError(
      409,
      "ASSET_CONFLICT",
      "Asset state conflict",
    );

    expect(projectAssetCreateErrorMessage("skills", conflict)).toBe(
      "当前项目已存在同名 Skill，请更换名称或标识。",
    );
    expect(projectAssetCreateErrorMessage("agents", conflict)).toBe(
      "资产状态已变化，请刷新后重试。",
    );
    expect(projectAssetCreateErrorMessage("mcp-servers", conflict)).toBe(
      "资产状态已变化，请刷新后重试。",
    );
  });

  test("replaces project Skill archive with a delayed permanent-package delete confirmation", () => {
    const projectSkill = catalog.project_items[0]!;
    const systemSkill = catalog.system_items[0]!;

    expect(
      projectAssetDetailLifecycleActions(
        "skills",
        projectSkill,
        projectSkill.capabilities,
      ),
    ).toEqual([]);
    expect(
      projectAssetDetailLifecycleActions(
        "agents",
        projectSkill,
        projectSkill.capabilities,
      ),
    ).toEqual([]);
    expect(projectAssetCanDelete("skills", projectSkill)).toBe(true);
    expect(projectAssetCanDelete("agents", projectSkill)).toBe(true);
    expect(projectAssetCanDelete("skills", systemSkill)).toBe(false);

    expect(skillDeleteSecondsRemaining(1_000, 1_000)).toBe(5);
    expect(skillDeleteSecondsRemaining(1_000, 5_001)).toBe(1);
    expect(skillDeleteSecondsRemaining(1_000, 6_000)).toBe(0);

    const waiting = renderToStaticMarkup(
      <Dialog open>
        <ProjectSkillDeleteConfirmation
          skillName="Project Skill"
          remainingSeconds={5}
          pending={false}
          errorMessage={null}
          onCancel={() => undefined}
          onConfirm={() => undefined}
        />
      </Dialog>,
    );
    expect(waiting).toContain("永久删除整个 Skill 包");
    expect(waiting).toContain("所有版本");
    expect(waiting).toContain("不可恢复");
    expect(waiting).toContain("确认删除（5 秒）");
    expect(waiting).toContain('disabled=""');

    const ready = renderToStaticMarkup(
      <Dialog open>
        <ProjectSkillDeleteConfirmation
          skillName="Project Skill"
          remainingSeconds={0}
          pending={false}
          errorMessage="资产状态已变化，请刷新后重试。"
          onCancel={() => undefined}
          onConfirm={() => undefined}
        />
      </Dialog>,
    );
    expect(ready).toContain(">确认永久删除<");
    expect(ready).toContain('role="alert"');
    expect(ready).toContain("资产状态已变化，请刷新后重试。");
    expect(ready).not.toContain('disabled=""');

    const agentWaiting = renderToStaticMarkup(
      <Dialog open>
        <ProjectAgentDeleteConfirmation
          agentName="Project Agent"
          remainingSeconds={5}
          pending={false}
          errorMessage={null}
          onCancel={() => undefined}
          onConfirm={() => undefined}
        />
      </Dialog>,
    );
    expect(agentWaiting).toContain("永久删除 Agent");
    expect(agentWaiting).toContain("全部设置");
    expect(agentWaiting).not.toContain("版本");
    expect(agentWaiting).toContain("确认删除（5 秒）");
  });

  test("places project MCP deletion in a dedicated danger zone and confirms permanently", () => {
    const projectMcp = {
      ...catalog.project_items[0]!,
      display_name: "Project MCP",
    };
    const systemMcp = catalog.system_items[0]!;

    expect(projectAssetCanDelete("mcp-servers", projectMcp)).toBe(true);
    expect(projectAssetCanDelete("mcp-servers", systemMcp)).toBe(false);
    expect(projectAssetLifecycleActionLabel("mcp-servers", "suspend")).toBe(
      "停用",
    );
    expect(projectAssetLifecycleActionLabel("mcp-servers", "activate")).toBe(
      "重新启用",
    );

    const dangerZone = renderToStaticMarkup(
      <ProjectMcpDangerZone
        actionPending={false}
        canDelete
        onDelete={() => undefined}
      />,
    );
    expect(dangerZone).toContain("危险区");
    expect(dangerZone).toContain("删除 MCP");
    expect(dangerZone).not.toContain("归档");
    expect(
      renderToStaticMarkup(
        <ProjectMcpDangerZone
          actionPending={false}
          canDelete={false}
          onDelete={() => undefined}
        />,
      ),
    ).toBe("");

    const confirmation = renderToStaticMarkup(
      <Dialog open>
        <ProjectMcpDeleteConfirmation
          mcpName="Project MCP"
          remainingSeconds={0}
          pending={false}
          errorMessage={null}
          onCancel={() => undefined}
          onConfirm={() => undefined}
        />
      </Dialog>,
    );
    expect(confirmation).toContain("永久删除 MCP");
    expect(confirmation).toContain("配置与 Credential 槽位");
    expect(confirmation).toContain("不可恢复");
    expect(confirmation).toContain("不会级联删除");
    expect(confirmation).not.toContain("版本");

    const snapshot = createProjectMcpDeleteSnapshot(projectMcp, 3_000);
    expect(snapshot).toEqual({
      assetId: projectMcp.id,
      mcpName: "Project MCP",
      expectedAssetVersion: projectMcp.version,
      startedAt: 3_000,
    });
    expect(Object.isFrozen(snapshot)).toBe(true);

    expect(
      projectMcpDeleteErrorMessage(
        new SharedAssetApiError(409, "ASSET_CONFLICT", "Asset state conflict"),
      ),
    ).toBe(
      "该 MCP 状态已变化，或仍被 Agent、历史运行或 Credential 授权快照引用；刷新并解除引用后重试。",
    );
  });

  test("freezes the confirmed Skill identity and revision for the full delay", () => {
    const projectSkill = catalog.project_items[0]!;
    const snapshot = createProjectSkillDeleteSnapshot(projectSkill, 1_000);
    const refreshedItem = {
      ...projectSkill,
      display_name: "Project Skill from collaborator",
      version: projectSkill.version + 1,
    };

    expect(snapshot).toEqual({
      assetId: projectSkill.id,
      skillName: projectSkill.display_name,
      expectedAssetVersion: projectSkill.version,
      startedAt: 1_000,
    });
    expect(Object.isFrozen(snapshot)).toBe(true);
    expect(snapshot.expectedAssetVersion).not.toBe(refreshedItem.version);
    expect(snapshot.skillName).not.toBe(refreshedItem.display_name);
  });

  test("freezes the confirmed Agent identity and revision for the full delay", () => {
    const projectAgent = {
      ...catalog.project_items[0]!,
      display_name: "Project Agent",
    };
    const snapshot = createProjectAgentDeleteSnapshot(projectAgent, 2_000);

    expect(snapshot).toEqual({
      assetId: projectAgent.id,
      agentName: "Project Agent",
      expectedAssetVersion: projectAgent.version,
      startedAt: 2_000,
    });
    expect(Object.isFrozen(snapshot)).toBe(true);
  });
});

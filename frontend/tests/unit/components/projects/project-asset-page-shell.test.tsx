import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  createProjectAgentDeleteSnapshot,
  createProjectSkillDeleteSnapshot,
  projectAssetDetailCanManageSystemBinding,
  projectAssetDetailShowsVersionHistory,
  ProjectAssetDetailHeader,
  ProjectSkillDetailActions,
  versionActionDisabled,
  versionPublishDisabled,
} from "@/components/projects/assets/project-asset-detail-sheet";
import {
  closeCompletedVersionDialog,
  createdProjectAssetSelectionReady,
  createdSkillSelectionReady,
  defaultProjectAssetSource,
  filterProjectAssetItems,
  handleRequestedVersion,
  importedSkillSelectionReady,
  projectAssetSourceOptions,
  ProjectAssetListView,
  rememberRequestedVersion,
  systemBindingToggleState,
  versionDialogSubmissionMatches,
} from "@/components/projects/assets/project-asset-page-shell";
import {
  projectAssetCreateErrorMessage,
  projectAssetCanDelete,
  projectAssetDetailLifecycleActions,
  projectSkillStatusToggleState,
} from "@/components/projects/assets/project-asset-view-model";
import {
  ProjectAgentDeleteConfirmation,
  ProjectSkillDeleteConfirmation,
  skillDeleteSecondsRemaining,
} from "@/components/projects/assets/project-skill-delete-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Sheet } from "@/components/ui/sheet";
import {
  SharedAssetApiError,
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

  test("routes system MCP binding through the version-aware dialog instead of a quick switch", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetListView
        kind="mcp-servers"
        data={catalog}
        source="system"
        selectedAssetId={null}
        onSelect={() => undefined}
        onToggleSystemBinding={() => undefined}
      />,
    );

    expect(html).toContain("管理绑定");
    expect(html).toContain("查看 Research Agent 详情");
    expect(html).not.toContain('role="switch"');
  });

  test("keeps system Skill binding controls on the list only", () => {
    const systemAsset = catalog.system_items[0]!;

    expect(
      projectAssetDetailCanManageSystemBinding("skills", systemAsset),
    ).toBe(false);
    expect(
      projectAssetDetailCanManageSystemBinding("agents", systemAsset),
    ).toBe(false);
    expect(
      projectAssetDetailCanManageSystemBinding("mcp-servers", systemAsset),
    ).toBe(true);
  });

  test("keeps Agent revisions internal while preserving Skill and MCP version history", () => {
    expect(projectAssetDetailShowsVersionHistory("agents")).toBe(false);
    expect(projectAssetDetailShowsVersionHistory("skills")).toBe(true);
    expect(projectAssetDetailShowsVersionHistory("mcp-servers")).toBe(true);
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
        onToggleProjectSkillStatus={() => undefined}
      />,
    );

    expect(html).toContain("查看 Meeting Brief 详情");
    expect(html).toContain('role="switch"');
    expect(html).toContain("启用 Meeting Brief");
    expect(html).not.toContain("已暂停");
    expect(html).not.toContain(">暂停<");
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
        onToggleProjectSkillStatus={() => undefined}
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

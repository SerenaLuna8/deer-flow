import { describe, expect, test } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import {
  cacheProjectAgentAuthoringReload,
  projectAgentAuthoringCacheEpochs,
  resolveProjectAgentAuthoringState,
  type AgentAssetVersion,
  type ProjectAgentAuthoringReload,
} from "@/components/projects/assets/agent-authoring-recovery";
import {
  AgentDependencySection,
  agentAuthoringBaseVersion,
  agentDependencyOptionCanToggle,
  agentDependencyOptions,
} from "@/components/projects/assets/agent-capability-workbench";
import { I18nProvider } from "@/core/i18n/context";
import { zhCN } from "@/core/i18n/locales/zh-CN";
import {
  type ProjectAssetItem,
  type ProjectAssetList,
  type VersionHistoryResponse,
  projectAssetKey,
} from "@/core/shared-assets";

const PROJECT_ID = "00000000-0000-4000-8000-000000000002";
const AGENT_ID = "00000000-0000-4000-8000-000000000003";
const SYSTEM_READY_ID = "00000000-0000-4000-8000-000000000010";
const SYSTEM_READY_VERSION_ID = "00000000-0000-4000-8000-000000000011";
const SYSTEM_DISABLED_ID = "00000000-0000-4000-8000-000000000012";
const SYSTEM_DISABLED_VERSION_ID = "00000000-0000-4000-8000-000000000013";
const PROJECT_READY_ID = "00000000-0000-4000-8000-000000000020";
const PROJECT_READY_VERSION_ID = "00000000-0000-4000-8000-000000000021";
const PROJECT_DRAFT_ID = "00000000-0000-4000-8000-000000000022";
const TIMESTAMP = "2026-08-13T00:00:00Z";

function catalog(): ProjectAssetList {
  return {
    request_id: "request-1",
    system_items: [
      {
        id: SYSTEM_READY_ID,
        scope: "system",
        project_id: null,
        slug: "system-ready",
        display_name: "System Ready",
        description: null,
        status: "active",
        current_version_id: SYSTEM_READY_VERSION_ID,
        revision: 2,
        capabilities: ["shared_assets.read"],
        binding: {
          project_id: PROJECT_ID,
          kind: "skill",
          asset_id: SYSTEM_READY_ID,
          current_version_id: SYSTEM_READY_VERSION_ID,
          enabled: true,
          version: 1,
          created_by_user_id: "user-1",
          updated_by_user_id: "user-1",
          created_at: TIMESTAMP,
          updated_at: TIMESTAMP,
        },
        created_by_user_id: "system",
        created_at: TIMESTAMP,
        updated_at: TIMESTAMP,
      },
      {
        id: SYSTEM_DISABLED_ID,
        scope: "system",
        project_id: null,
        slug: "system-disabled",
        display_name: "System Disabled",
        description: null,
        status: "suspended",
        current_version_id: SYSTEM_DISABLED_VERSION_ID,
        revision: 3,
        capabilities: ["shared_assets.read"],
        binding: null,
        created_by_user_id: "system",
        created_at: TIMESTAMP,
        updated_at: TIMESTAMP,
      },
    ],
    project_items: [
      {
        id: PROJECT_READY_ID,
        scope: "project",
        project_id: PROJECT_ID,
        slug: "project-ready",
        display_name: "Project Ready",
        description: null,
        status: "active",
        current_version_id: PROJECT_READY_VERSION_ID,
        revision: 4,
        capabilities: ["shared_assets.read", "shared_assets.edit"],
        binding: null,
        created_by_user_id: "user-1",
        created_at: TIMESTAMP,
        updated_at: TIMESTAMP,
      },
      {
        id: PROJECT_DRAFT_ID,
        scope: "project",
        project_id: PROJECT_ID,
        slug: "project-draft",
        display_name: "Project Draft",
        description: null,
        status: "active",
        current_version_id: null,
        revision: 1,
        capabilities: ["shared_assets.read", "shared_assets.edit"],
        binding: null,
        created_by_user_id: "user-1",
        created_at: TIMESTAMP,
        updated_at: TIMESTAMP,
      },
    ],
  };
}

describe("Agent capability workbench recovery", () => {
  test("keeps every visible dependency and explains why ineligible assets are disabled", () => {
    const options = agentDependencyOptions(
      "skill",
      catalog(),
      zhCN.agents.capabilities,
    );

    expect(options).toHaveLength(4);
    expect(
      options.find((option) => option.assetId === SYSTEM_READY_ID),
    ).toEqual(
      expect.objectContaining({
        versionId: `system:${SYSTEM_READY_ID}`,
        disabled: false,
        reason: null,
        remediation: null,
      }),
    );
    expect(
      options.find((option) => option.assetId === SYSTEM_DISABLED_ID),
    ).toEqual(
      expect.objectContaining({
        versionId: `system:${SYSTEM_DISABLED_ID}`,
        disabled: true,
        reason: expect.stringMatching(/未激活.*系统绑定未启用/),
        remediation: expect.stringContaining("管理员"),
      }),
    );
    expect(
      options.find((option) => option.assetId === PROJECT_DRAFT_ID),
    ).toEqual(
      expect.objectContaining({
        versionId: null,
        disabled: true,
        reason: expect.stringContaining("尚无当前版本"),
        remediation: expect.stringContaining("激活"),
      }),
    );
  });

  test("renders the unavailable reason while allowing an existing disabled binding to be removed", () => {
    const option = agentDependencyOptions(
      "skill",
      catalog(),
      zhCN.agents.capabilities,
    ).find((candidate) => candidate.assetId === SYSTEM_DISABLED_ID);
    expect(option).toBeDefined();
    if (!option) return;

    expect(agentDependencyOptionCanToggle(option, false, true)).toBe(false);
    expect(agentDependencyOptionCanToggle(option, true, true)).toBe(true);
    expect(agentDependencyOptionCanToggle(option, true, false)).toBe(false);

    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AgentDependencySection
          title="Skill"
          emptyLabel="empty"
          options={[option]}
          selectedIds={[]}
          editing
          query=""
          onToggle={() => undefined}
        />
      </I18nProvider>,
    );
    expect(html).toContain("System Disabled");
    expect(html).toContain('disabled=""');
    expect(html).toContain("系统绑定未启用");
    expect(html).toContain("管理员");
  });

  test("adopts only a consistent post-conflict catalog and complete authoring history", () => {
    const live = {
      id: "00000000-0000-4000-8000-000000000030",
      agent_id: AGENT_ID,
      version_number: 1,
      relation: "current",
      supersedes_version_id: null,
    } as AgentAssetVersion;
    const draft = {
      ...live,
      id: "00000000-0000-4000-8000-000000000031",
      version_number: 2,
      relation: "candidate",
      supersedes_version_id: live.id,
    } as AgentAssetVersion;
    const item = {
      id: AGENT_ID,
      revision: 6,
      current_version_id: live.id,
    } as ProjectAssetItem;
    const snapshot: ProjectAssetList = {
      system_items: [],
      project_items: [item],
      request_id: "catalog",
    };
    const history = {
      data: [live, draft],
      request_id: "history",
    } as VersionHistoryResponse;

    expect(
      resolveProjectAgentAuthoringState({
        beforeCatalog: snapshot,
        history,
        afterCatalog: snapshot,
        assetId: AGENT_ID,
        attemptedRevision: 5,
      }),
    ).toEqual({ item, version: draft });
    expect(
      resolveProjectAgentAuthoringState({
        beforeCatalog: snapshot,
        history,
        afterCatalog: snapshot,
        assetId: AGENT_ID,
        minimumRevision: 6,
      }),
    ).toEqual({ item, version: draft });
    expect(() =>
      resolveProjectAgentAuthoringState({
        beforeCatalog: snapshot,
        history,
        afterCatalog: snapshot,
        assetId: AGENT_ID,
        minimumRevision: 7,
      }),
    ).toThrow(/changed while/);
    expect(() =>
      resolveProjectAgentAuthoringState({
        beforeCatalog: snapshot,
        history,
        afterCatalog: {
          ...snapshot,
          project_items: [{ ...item, revision: 7 }],
        },
        assetId: AGENT_ID,
        attemptedRevision: 5,
      }),
    ).toThrow(/changed while/);
    expect(() =>
      resolveProjectAgentAuthoringState({
        beforeCatalog: snapshot,
        history: { data: [], request_id: "stale-history" },
        afterCatalog: snapshot,
        assetId: AGENT_ID,
        attemptedRevision: 5,
      }),
    ).toThrow(/unavailable/);
  });

  test("uses the same live-pointer authoring base as the backend after a conflict", () => {
    const live = {
      id: "00000000-0000-4000-8000-000000000030",
      agent_id: AGENT_ID,
      version_number: 1,
      relation: "current" as const,
      supersedes_version_id: null,
    };
    const staleNewerDraft = {
      id: "00000000-0000-4000-8000-000000000032",
      agent_id: AGENT_ID,
      version_number: 3,
      relation: "historical" as const,
      supersedes_version_id: "00000000-0000-4000-8000-000000000029",
    };

    expect(agentAuthoringBaseVersion([staleNewerDraft, live], live.id)).toBe(
      live,
    );

    const currentDraft = {
      ...staleNewerDraft,
      relation: "candidate" as const,
      supersedes_version_id: live.id,
    };
    expect(agentAuthoringBaseVersion([live, currentDraft], live.id)).toBe(
      currentDraft,
    );
  });

  test("never lets a late recovery overwrite a newer cached Agent catalog", async () => {
    const queryClient = new QueryClient();
    const live = {
      id: "00000000-0000-4000-8000-000000000030",
      agent_id: AGENT_ID,
      version_number: 1,
      relation: "current",
      supersedes_version_id: null,
    } as AgentAssetVersion;
    const loadedItem = {
      id: AGENT_ID,
      revision: 6,
      current_version_id: live.id,
    } as ProjectAssetItem;
    const loadedCatalog = {
      system_items: [],
      project_items: [loadedItem],
      request_id: "loaded",
    } as ProjectAssetList;
    const newerCatalog = {
      ...loadedCatalog,
      project_items: [{ ...loadedItem, revision: 7 }],
      request_id: "newer",
    } as ProjectAssetList;
    const reload = {
      item: loadedItem,
      version: live,
      agentCatalog: loadedCatalog,
      history: { data: [live], request_id: "history" },
      skillCatalog: null,
      mcpCatalog: null,
    } as ProjectAgentAuthoringReload;
    const key = projectAssetKey("account-1", PROJECT_ID, "agents");
    const startedAt = projectAgentAuthoringCacheEpochs({
      queryClient,
      accountId: "account-1",
      projectId: PROJECT_ID,
      assetId: AGENT_ID,
    });
    queryClient.setQueryData(key, newerCatalog);

    await expect(
      cacheProjectAgentAuthoringReload({
        queryClient,
        accountId: "account-1",
        projectId: PROJECT_ID,
        assetId: AGENT_ID,
        reload,
        startedAt,
        isCurrent: () => true,
      }),
    ).rejects.toThrow(/older than/);
    expect(queryClient.getQueryData(key)).toBe(newerCatalog);
  });

  test("accepts a bracketed catalog that legitimately removed an unrelated asset", async () => {
    const queryClient = new QueryClient();
    const live = {
      id: "00000000-0000-4000-8000-000000000030",
      agent_id: AGENT_ID,
      version_number: 1,
      relation: "current",
      supersedes_version_id: null,
    } as AgentAssetVersion;
    const target = {
      id: AGENT_ID,
      revision: 6,
      current_version_id: live.id,
    } as ProjectAssetItem;
    const removed = {
      id: "00000000-0000-4000-8000-000000000099",
      revision: 1,
      current_version_id: null,
    } as ProjectAssetItem;
    const key = projectAssetKey("account-1", PROJECT_ID, "agents");
    queryClient.setQueryData(key, {
      system_items: [],
      project_items: [target, removed],
      request_id: "before",
    } as ProjectAssetList);
    const startedAt = projectAgentAuthoringCacheEpochs({
      queryClient,
      accountId: "account-1",
      projectId: PROJECT_ID,
      assetId: AGENT_ID,
    });
    const nextCatalog = {
      system_items: [],
      project_items: [target],
      request_id: "after",
    } as ProjectAssetList;

    await cacheProjectAgentAuthoringReload({
      queryClient,
      accountId: "account-1",
      projectId: PROJECT_ID,
      assetId: AGENT_ID,
      reload: {
        item: target,
        version: live,
        agentCatalog: nextCatalog,
        history: { data: [live], request_id: "history" },
        skillCatalog: null,
        mcpCatalog: null,
      },
      startedAt,
      isCurrent: () => true,
    });

    expect(queryClient.getQueryData(key)).toStrictEqual(nextCatalog);
  });

  test("uses the cache update counter when two writes share the same millisecond", async () => {
    const queryClient = new QueryClient();
    const live = {
      id: "00000000-0000-4000-8000-000000000030",
      agent_id: AGENT_ID,
      version_number: 1,
      relation: "current",
      supersedes_version_id: null,
    } as AgentAssetVersion;
    const target = {
      id: AGENT_ID,
      revision: 6,
      current_version_id: live.id,
    } as ProjectAssetItem;
    const key = projectAssetKey("account-1", PROJECT_ID, "agents");
    const first = {
      system_items: [],
      project_items: [target],
      request_id: "first",
    } as ProjectAssetList;
    queryClient.setQueryData(key, first, { updatedAt: 1234 });
    const startedAt = projectAgentAuthoringCacheEpochs({
      queryClient,
      accountId: "account-1",
      projectId: PROJECT_ID,
      assetId: AGENT_ID,
    });
    queryClient.setQueryData(
      key,
      { ...first, request_id: "second" },
      {
        updatedAt: 1234,
      },
    );

    await expect(
      cacheProjectAgentAuthoringReload({
        queryClient,
        accountId: "account-1",
        projectId: PROJECT_ID,
        assetId: AGENT_ID,
        reload: {
          item: target,
          version: live,
          agentCatalog: first,
          history: { data: [live], request_id: "history" },
          skillCatalog: null,
          mcpCatalog: null,
        },
        startedAt,
        isCurrent: () => true,
      }),
    ).rejects.toThrow(/older than/);
  });
});

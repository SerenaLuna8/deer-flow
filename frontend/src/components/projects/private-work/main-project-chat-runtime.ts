import {
  enableProjectSystemBinding,
  listProjectAssets,
  listProjectAssetVersions,
  rollbackProjectSystemBinding,
  upgradeProjectSystemBinding,
  type AssetKind,
  type AssetVersion,
  type EnableSystemBindingInput,
  type MoveSystemBindingInput,
  type ProjectAssetItem,
  type ProjectAssetList,
  type VersionHistoryResponse,
} from "@/core/shared-assets";
import type { ScopedMcpVersion } from "@/core/shared-assets/mcp-runtime";

import {
  ensureMainSystemAgentBindings,
  type MainDependencyTarget,
} from "./agent-selector-dialog";

type DependencyKind = "skills" | "mcp-servers";
type BindingKind = "agent" | "skill" | "mcp";

type MainRuntimeDependencies = {
  listAssets?: (
    projectId: string,
    kind: DependencyKind,
  ) => Promise<ProjectAssetList>;
  listVersions?: (
    projectId: string,
    kind: "agents" | DependencyKind,
    assetId: string,
  ) => Promise<VersionHistoryResponse>;
  enableBinding?: (
    projectId: string,
    kind: AssetKind,
    input: EnableSystemBindingInput,
  ) => Promise<unknown>;
  moveBinding?: (
    projectId: string,
    kind: Exclude<BindingKind, "agent">,
    assetId: string,
    action: "upgrade" | "rollback",
    input: MoveSystemBindingInput,
  ) => Promise<unknown>;
};

function catalogItems(catalog: ProjectAssetList): ProjectAssetItem[] {
  return [...catalog.system_items, ...catalog.project_items];
}

function isDependencyVersion(
  kind: DependencyKind,
  version: AssetVersion,
): boolean {
  return kind === "skills" ? "skill_id" in version : "mcp_server_id" in version;
}

export function mainDependencyTargets(
  kind: DependencyKind,
  requiredVersionIds: readonly string[],
  items: readonly ProjectAssetItem[],
  histories: readonly VersionHistoryResponse[],
): MainDependencyTarget[] {
  return requiredVersionIds.map((versionId) => {
    for (const [index, item] of items.entries()) {
      const history = histories[index];
      const version = history?.data.find(
        (candidate) =>
          candidate.id === versionId &&
          isDependencyVersion(kind, candidate) &&
          "workflow_status" in candidate &&
          candidate.workflow_status === "published",
      );
      if (!version || item.status !== "active") continue;
      const boundVersion =
        item.binding?.enabled === true
          ? history?.data.find(
              (candidate) =>
                candidate.id === item.binding?.version_id &&
                isDependencyVersion(kind, candidate),
            )
          : undefined;
      return {
        item,
        versionId,
        versionNumber: version.version_number,
        boundVersionNumber: boundVersion?.version_number ?? null,
      };
    }
    throw new Error("Main 的依赖版本尚未就绪");
  });
}

export function scopedMcpVersions(
  items: readonly ProjectAssetItem[],
  histories: readonly VersionHistoryResponse[],
): ScopedMcpVersion[] {
  return histories.flatMap((history, index) => {
    const item = items[index];
    if (!item) return [];
    return history.data.flatMap((version) =>
      "mcp_server_id" in version
        ? [{ scope: item.scope, version } satisfies ScopedMcpVersion]
        : [],
    );
  });
}

async function defaultMoveBinding(
  projectId: string,
  kind: Exclude<BindingKind, "agent">,
  assetId: string,
  action: "upgrade" | "rollback",
  input: MoveSystemBindingInput,
): Promise<unknown> {
  return action === "upgrade"
    ? upgradeProjectSystemBinding(projectId, kind, assetId, input)
    : rollbackProjectSystemBinding(projectId, kind, assetId, input);
}

export async function prepareMainProjectChatRuntime({
  projectId,
  agent,
  listAssets = listProjectAssets,
  listVersions = listProjectAssetVersions,
  enableBinding = enableProjectSystemBinding,
  moveBinding = defaultMoveBinding,
}: {
  projectId: string;
  agent: ProjectAssetItem;
} & MainRuntimeDependencies): Promise<{ bindingsChanged: boolean }> {
  const [agentHistory, skillCatalog, mcpCatalog] = await Promise.all([
    listVersions(projectId, "agents", agent.id),
    listAssets(projectId, "skills"),
    listAssets(projectId, "mcp-servers"),
  ]);
  const selectedVersionId =
    agent.binding?.enabled === true
      ? agent.binding.version_id
      : agent.current_published_version_id;
  const currentVersion = agentHistory.data.find(
    (version) =>
      "agent_id" in version &&
      version.id === selectedVersionId &&
      version.workflow_status === "published",
  );
  if (!currentVersion || !("agent_id" in currentVersion)) {
    throw new Error("Main 智能体当前版本不可用");
  }

  const skillItems = catalogItems(skillCatalog);
  const mcpItems = catalogItems(mcpCatalog);
  const [skillHistories, mcpHistories] = await Promise.all([
    currentVersion.skill_version_ids.length === 0
      ? []
      : Promise.all(
          skillItems.map((item) => listVersions(projectId, "skills", item.id)),
        ),
    currentVersion.mcp_version_ids.length === 0
      ? []
      : Promise.all(
          mcpItems.map((item) =>
            listVersions(projectId, "mcp-servers", item.id),
          ),
        ),
  ]);
  const skillDependencies = mainDependencyTargets(
    "skills",
    currentVersion.skill_version_ids,
    skillItems,
    skillHistories,
  );
  const mcpDependencies = mainDependencyTargets(
    "mcp-servers",
    currentVersion.mcp_version_ids,
    mcpItems,
    mcpHistories,
  );
  const bindingsChanged = await ensureMainSystemAgentBindings({
    agent,
    requiredSkillVersionIds: currentVersion.skill_version_ids,
    requiredMcpVersionIds: currentVersion.mcp_version_ids,
    skillDependencies,
    mcpDependencies,
    mcpVersions: scopedMcpVersions(mcpItems, mcpHistories),
    enableBinding: (kind, input) => enableBinding(projectId, kind, input),
    moveBinding: (kind, assetId, action, input) =>
      moveBinding(projectId, kind, assetId, action, input),
  });
  return { bindingsChanged };
}

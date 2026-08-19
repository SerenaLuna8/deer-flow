import {
  assetIdSchema,
  assetListKindSchema,
  type AssetListKind,
} from "./types";

function requireKeyPart(value: string, label: string): string {
  if (value.trim() === "") throw new Error(`${label} is required`);
  return value;
}

export const sharedAssetKeys = {
  account: (accountId: string) =>
    [
      "account",
      requireKeyPart(accountId, "Account ID"),
      "shared-assets",
    ] as const,
  admin: (accountId: string) =>
    [...sharedAssetKeys.account(accountId), "admin"] as const,
  projects: (accountId: string) =>
    [...sharedAssetKeys.account(accountId), "project"] as const,
};

export function projectSharedAssetRoot(scope: {
  accountId: string;
  projectId: string;
}) {
  return [
    ...sharedAssetKeys.projects(scope.accountId),
    requireKeyPart(scope.projectId, "Project ID"),
  ] as const;
}

export function adminAssetKey(accountId: string, kind: AssetListKind) {
  return [
    ...sharedAssetKeys.admin(accountId),
    assetListKindSchema.parse(kind),
  ] as const;
}

export function adminProjectAssetKey(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
) {
  return [
    ...sharedAssetKeys.admin(accountId),
    "project",
    assetIdSchema.parse(projectId),
    assetListKindSchema.parse(kind),
  ] as const;
}

export function systemCatalogKey(
  accountId: string,
  kind: Exclude<AssetListKind, "credentials">,
) {
  return [
    ...sharedAssetKeys.account(accountId),
    "catalog",
    assetListKindSchema.parse(kind),
  ] as const;
}

export function projectAssetKey(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
) {
  return [
    ...projectSharedAssetRoot({ accountId, projectId }),
    assetListKindSchema.parse(kind),
  ] as const;
}

export function projectDefaultAgentKey(accountId: string, projectId: string) {
  return [
    ...projectAssetKey(accountId, projectId, "agents"),
    "default",
  ] as const;
}

export function projectAgentRuntimeAssessmentsRoot(
  accountId: string,
  projectId: string,
) {
  return [
    ...projectAssetKey(accountId, projectId, "agents"),
    "runtime-assessments",
  ] as const;
}

export function projectAgentRuntimeAssessmentsKey(
  accountId: string,
  projectId: string,
  agentIds: readonly string[],
) {
  return [
    ...projectAgentRuntimeAssessmentsRoot(accountId, projectId),
    ...agentIds.map((agentId) => assetIdSchema.parse(agentId)),
  ] as const;
}

export function projectAssetMutationKey(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
  action: string,
) {
  return [
    ...projectAssetKey(accountId, projectId, kind),
    "mutation",
    requireKeyPart(action, "Mutation action"),
  ] as const;
}

export function adminAssetVersionsKey(
  accountId: string,
  kind: AssetListKind,
  assetId: string,
) {
  return [
    ...adminAssetKey(accountId, kind),
    "asset",
    requireKeyPart(assetId, "Asset ID"),
    "versions",
  ] as const;
}

export function adminProjectAssetVersionsKey(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
  assetId: string,
) {
  return [
    ...adminProjectAssetKey(accountId, projectId, kind),
    "asset",
    assetIdSchema.parse(assetId),
    "versions",
  ] as const;
}

export function projectAssetVersionsKey(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
  assetId: string,
) {
  return [
    ...projectAssetKey(accountId, projectId, kind),
    "asset",
    requireKeyPart(assetId, "Asset ID"),
    "versions",
  ] as const;
}

export function projectSkillVersionFileKey(
  accountId: string,
  projectId: string,
  assetId: string,
  versionId: string,
  path: string,
) {
  return [
    ...projectAssetVersionsKey(accountId, projectId, "skills", assetId),
    "version",
    requireKeyPart(versionId, "Version ID"),
    "file",
    requireKeyPart(path, "Skill file path"),
  ] as const;
}

export function projectMcpToolInventoryKey(
  accountId: string,
  projectId: string,
  assetId: string,
  versionId: string,
) {
  return [
    ...projectAssetVersionsKey(
      accountId,
      projectId,
      "mcp-servers",
      assetIdSchema.parse(assetId),
    ),
    "version",
    assetIdSchema.parse(versionId),
    "tools",
  ] as const;
}

export function projectMcpEditableConfigurationKey(
  accountId: string,
  projectId: string,
  assetId: string,
) {
  return [
    ...projectAssetKey(accountId, projectId, "mcp-servers"),
    "asset",
    assetIdSchema.parse(assetId),
    "editable-configuration",
  ] as const;
}

export function projectSkillCredentialBindingsKey(
  accountId: string,
  projectId: string,
  skillId: string,
) {
  return [
    ...projectAssetVersionsKey(
      accountId,
      projectId,
      "skills",
      assetIdSchema.parse(skillId),
    ),
    "credential-bindings",
  ] as const;
}

export function projectSkillCredentialBindingsMutationKey(
  accountId: string,
  projectId: string,
  skillId: string,
) {
  return [
    ...projectSkillCredentialBindingsKey(accountId, projectId, skillId),
    "mutation",
    "replace",
  ] as const;
}

export function projectSkillPublishPlanKey(
  accountId: string,
  projectId: string,
  skillId: string,
  versionId: string,
) {
  return [
    ...projectAssetVersionsKey(
      accountId,
      projectId,
      "skills",
      assetIdSchema.parse(skillId),
    ),
    "version",
    assetIdSchema.parse(versionId),
    "publish-plan",
  ] as const;
}

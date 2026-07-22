import { assetListKindSchema, type AssetListKind } from "./types";

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

export function adminAssetKey(accountId: string, kind: AssetListKind) {
  return [
    ...sharedAssetKeys.admin(accountId),
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
    ...sharedAssetKeys.projects(accountId),
    requireKeyPart(projectId, "Project ID"),
    assetListKindSchema.parse(kind),
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

export function adminCredentialRotationStatusKey(accountId: string) {
  return [
    ...adminAssetKey(accountId, "credentials"),
    "rotation-status",
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

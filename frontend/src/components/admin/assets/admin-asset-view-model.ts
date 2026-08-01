import type { Translations } from "@/core/i18n/locales/types";
import {
  SharedAssetApiError,
  type AssetListKind,
  type AssetSummary,
  type AssetStatus,
} from "@/core/shared-assets";

export const ADMIN_ASSET_PAGE_SIZE = 20;

type CredentialTypeCopy =
  Translations["adminAssets"]["common"]["credentialTypes"];
type McpTransportCopy = Translations["adminAssets"]["common"]["transportTypes"];
type CredentialPayloadGroupCopy =
  Translations["adminAssets"]["common"]["credentialPayloadGroups"];

export function adminCredentialTypeLabel(
  credentialType: string,
  copy: CredentialTypeCopy,
): string {
  switch (credentialType) {
    case "model_api_key":
      return copy.modelApiKey;
    case "api_key":
      return copy.apiKey;
    case "token":
      return copy.token;
    case "oauth":
      return copy.oauth;
    case "database":
      return copy.database;
    default:
      return credentialType;
  }
}

export function adminMcpTransportLabel(
  transport: string,
  copy: McpTransportCopy,
): string {
  switch (transport) {
    case "stdio":
      return copy.stdio;
    case "sse":
      return copy.sse;
    case "http":
      return copy.http;
    default:
      return transport;
  }
}

export function adminCredentialPayloadGroupLabel(
  group: string,
  copy: CredentialPayloadGroupCopy,
): string {
  switch (group) {
    case "env":
      return copy.env;
    case "headers":
      return copy.headers;
    case "oauth":
      return copy.oauth;
    default:
      return group;
  }
}

export type AdminAssetPublicationFilter = "all" | "published" | "unpublished";
export type AdminAssetUpdatedSort = "newest" | "oldest";

export interface AdminAssetCatalogFilters {
  query: string;
  status: "all" | AssetStatus;
  publication: AdminAssetPublicationFilter;
  updatedSort: AdminAssetUpdatedSort;
}

export interface AdminAssetCatalogSummary {
  total: number;
  active: number;
  suspended: number;
  archived: number;
  unpublished: number;
  latestUpdatedAt: string | null;
}

export interface AdminAssetCatalogPage<T> {
  items: T[];
  page: number;
  totalPages: number;
  totalItems: number;
}

export function adminAssetCatalogSummary(
  items: readonly AssetSummary[],
): AdminAssetCatalogSummary {
  let active = 0;
  let suspended = 0;
  let archived = 0;
  let unpublished = 0;
  let latestUpdatedAt: string | null = null;
  let latestUpdatedTime = Number.NEGATIVE_INFINITY;

  for (const item of items) {
    if (item.status === "active") active += 1;
    if (item.status === "suspended") suspended += 1;
    if (item.status === "archived") archived += 1;
    if (item.current_published_version_id === null) unpublished += 1;

    const updatedTime = Date.parse(item.updated_at);
    if (updatedTime > latestUpdatedTime) {
      latestUpdatedAt = item.updated_at;
      latestUpdatedTime = updatedTime;
    }
  }

  return {
    total: items.length,
    active,
    suspended,
    archived,
    unpublished,
    latestUpdatedAt,
  };
}

function matchesAdminAssetQuery(item: AssetSummary, query: string): boolean {
  if (query.length === 0) return true;
  return [item.display_name, item.slug, item.id].some((value) =>
    value.toLowerCase().includes(query),
  );
}

function matchesAdminAssetPublication(
  item: AssetSummary,
  publication: AdminAssetPublicationFilter,
): boolean {
  if (publication === "all") return true;
  const isPublished = item.current_published_version_id !== null;
  return publication === "published" ? isPublished : !isPublished;
}

export function filterAndSortAdminAssets(
  items: readonly AssetSummary[],
  filters: AdminAssetCatalogFilters,
): AssetSummary[] {
  const query = filters.query.trim().toLowerCase();
  const direction = filters.updatedSort === "newest" ? -1 : 1;

  return items
    .map((item, index) => ({ item, index }))
    .filter(
      ({ item }) =>
        matchesAdminAssetQuery(item, query) &&
        (filters.status === "all" || item.status === filters.status) &&
        matchesAdminAssetPublication(item, filters.publication),
    )
    .sort((left, right) => {
      const timeDifference =
        Date.parse(left.item.updated_at) - Date.parse(right.item.updated_at);
      return timeDifference === 0
        ? left.index - right.index
        : timeDifference * direction;
    })
    .map(({ item }) => item);
}

function normalizedPageSize(pageSize: number): number {
  return Number.isFinite(pageSize) && pageSize >= 1
    ? Math.floor(pageSize)
    : ADMIN_ASSET_PAGE_SIZE;
}

export function clampAdminAssetPage(
  requestedPage: number,
  totalItems: number,
  pageSize = ADMIN_ASSET_PAGE_SIZE,
): number {
  const size = normalizedPageSize(pageSize);
  const normalizedTotal = Number.isFinite(totalItems)
    ? Math.max(0, Math.floor(totalItems))
    : 0;
  const totalPages = Math.max(1, Math.ceil(normalizedTotal / size));
  const page = Number.isFinite(requestedPage)
    ? Math.max(1, Math.floor(requestedPage))
    : 1;
  return Math.min(page, totalPages);
}

export function adminAssetCatalogPage<T>(
  items: readonly T[],
  requestedPage: number,
  pageSize = ADMIN_ASSET_PAGE_SIZE,
): AdminAssetCatalogPage<T> {
  const size = normalizedPageSize(pageSize);
  const page = clampAdminAssetPage(requestedPage, items.length, size);
  const totalPages = Math.max(1, Math.ceil(items.length / size));
  const start = (page - 1) * size;

  return {
    items: items.slice(start, start + size),
    page,
    totalPages,
    totalItems: items.length,
  };
}

function adminAssetFiltersEqual(
  left: AdminAssetCatalogFilters,
  right: AdminAssetCatalogFilters,
): boolean {
  return (
    left.query === right.query &&
    left.status === right.status &&
    left.publication === right.publication &&
    left.updatedSort === right.updatedSort
  );
}

export function resetAdminAssetPage(
  currentPage: number,
  previousFilters: AdminAssetCatalogFilters,
  nextFilters: AdminAssetCatalogFilters,
  totalItems: number,
  pageSize = ADMIN_ASSET_PAGE_SIZE,
): number {
  if (!adminAssetFiltersEqual(previousFilters, nextFilters)) return 1;
  return clampAdminAssetPage(currentPage, totalItems, pageSize);
}

export type AssetLifecycleAction = "archive" | "suspend";
export type VersionWorkflowAction = "publish" | "submit" | "approve";

export function assetLifecycleActions(
  status: AssetStatus,
): AssetLifecycleAction[] {
  if (status === "active") return ["archive", "suspend"];
  if (status === "archived") return ["suspend"];
  return ["archive"];
}

export function versionWorkflowActions(
  kind: Exclude<AssetListKind, "credentials">,
  workflowStatus: "draft" | "pending_approval" | "published" | "rejected",
  hasCredentialSlots: boolean,
): VersionWorkflowAction[] {
  if (workflowStatus === "published" || workflowStatus === "rejected") {
    return [];
  }
  if (kind !== "mcp-servers") {
    return workflowStatus === "draft" ? ["publish"] : [];
  }
  if (!hasCredentialSlots) {
    return workflowStatus === "draft" ? ["publish"] : [];
  }
  return workflowStatus === "draft" ? ["submit"] : ["approve"];
}

const ERROR_MESSAGES: Partial<Record<SharedAssetApiError["code"], string>> = {
  ASSET_NOT_FOUND: "资产不存在或已不可见。",
  ASSET_FORBIDDEN: "当前账户没有执行此操作的权限。",
  ASSET_CONFLICT: "资产状态已变化，请刷新后重试。",
  ASSET_VALIDATION_FAILED: "提交内容不符合资产要求。",
  ASSET_STORAGE_QUOTA_EXCEEDED:
    "项目 Skill 存储配额已用尽，请清理不再需要的 Skill 后重试。",
  ASSET_STORAGE_UNAVAILABLE: "资产存储暂时不可用，请稍后重试。",
  AUTH_REQUIRED: "登录状态已失效，请重新登录。",
  ASSET_NETWORK_ERROR: "暂时无法连接资产服务，请稍后重试。",
  ASSET_RESPONSE_INVALID: "资产服务返回了无效数据。",
  ASSET_ERROR_RESPONSE_INVALID: "操作失败，请稍后重试。",
};

type AdminAssetErrorCopy = Translations["adminAssets"]["errors"];

function localizedErrorMessage(
  code: SharedAssetApiError["code"],
  copy: AdminAssetErrorCopy,
): string {
  const messages: Partial<
    Record<SharedAssetApiError["code"], keyof AdminAssetErrorCopy>
  > = {
    ASSET_NOT_FOUND: "notFound",
    ASSET_FORBIDDEN: "forbidden",
    ASSET_CONFLICT: "conflict",
    ASSET_VALIDATION_FAILED: "validationFailed",
    ASSET_STORAGE_QUOTA_EXCEEDED: "storageQuota",
    ASSET_STORAGE_UNAVAILABLE: "storageUnavailable",
    AUTH_REQUIRED: "authRequired",
    ASSET_NETWORK_ERROR: "network",
    ASSET_RESPONSE_INVALID: "invalidResponse",
    ASSET_ERROR_RESPONSE_INVALID: "invalidErrorResponse",
  };
  const key = messages[code];
  return key ? copy[key] : copy.fallback;
}

export function adminAssetErrorMessage(
  error: unknown,
  copy?: AdminAssetErrorCopy,
): string {
  if (error instanceof SharedAssetApiError) {
    if (copy) return localizedErrorMessage(error.code, copy);
    return ERROR_MESSAGES[error.code] ?? "操作失败，请稍后重试。";
  }
  return copy?.fallback ?? "操作失败，请稍后重试。";
}

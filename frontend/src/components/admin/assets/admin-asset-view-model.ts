import {
  SharedAssetApiError,
  type AssetListKind,
  type AssetStatus,
} from "@/core/shared-assets";

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

export function adminAssetErrorMessage(error: unknown): string {
  if (error instanceof SharedAssetApiError) {
    return ERROR_MESSAGES[error.code] ?? "操作失败，请稍后重试。";
  }
  return "操作失败，请稍后重试。";
}

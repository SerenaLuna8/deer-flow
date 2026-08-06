import { ProjectApiError } from "@/core/projects/api";
import type { Project, ProjectQuotaSummary } from "@/core/projects/types";

export type ProjectListFilter = "all" | "pinned";

export function canUpdateProject(project: Project): boolean {
  return project.capabilities.includes("project.update");
}

export function filterAndSortProjects(
  projects: readonly Project[],
  query: string,
  filter: ProjectListFilter = "all",
): Project[] {
  const normalized = query.trim().toLocaleLowerCase();
  return [...projects]
    .filter(
      (project) =>
        (filter === "all" || project.is_pinned) &&
        (!normalized ||
          project.display_name.toLocaleLowerCase().includes(normalized) ||
          project.slug.toLocaleLowerCase().includes(normalized)),
    )
    .sort((left, right) => {
      if (left.is_pinned !== right.is_pinned) return left.is_pinned ? -1 : 1;
      const leftEntered = left.last_entered_at ?? "";
      const rightEntered = right.last_entered_at ?? "";
      return rightEntered.localeCompare(leftEntered);
    });
}

function formatQuotaCount(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(
    value,
  );
}

function formatStorageBytes(value: number): string {
  const gibibytes = value / 1_073_741_824;
  return `${new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 1,
  }).format(gibibytes)} GiB`;
}

export function formatProjectQuota(summary: ProjectQuotaSummary) {
  const total = (dimension: ProjectQuotaSummary["members"]) =>
    dimension.used + dimension.reserved;
  return {
    members: `成员 ${formatQuotaCount(total(summary.members))} / ${formatQuotaCount(summary.members.limit)}`,
    storage: `存储 ${formatStorageBytes(total(summary.storage_bytes))} / ${formatStorageBytes(summary.storage_bytes.limit)}`,
    runs: `运行 ${formatQuotaCount(total(summary.concurrent_runs))} / ${formatQuotaCount(summary.concurrent_runs.limit)}`,
    mcp: `MCP ${formatQuotaCount(total(summary.mcp_calls_daily))} / ${formatQuotaCount(summary.mcp_calls_daily.limit)}`,
  };
}

export function projectErrorMessage(error: unknown): string {
  if (error instanceof ProjectApiError) {
    switch (error.code) {
      case "PROJECT_SLUG_CONFLICT":
        return "这个项目标识已存在，请换一个后重试。";
      case "PROJECT_NOT_FOUND":
      case "PROJECT_FORBIDDEN":
      case "PROJECT_OR_MEMBER_NOT_FOUND":
      case "PROJECT_MEMBERSHIP_FORBIDDEN":
        return "项目不可用或成员关系已失效，请返回工作空间。";
      case "PROJECT_LAST_ADMIN":
        return "不能移除或降级最后一名 Admin，请先指定其他 Admin。";
      case "PROJECT_MEMBER_QUOTA_EXCEEDED":
        return "项目成员容量已满，请联系项目管理员调整成员上限后，重新打开邀请链接。";
      case "PROJECT_MEMBERSHIP_VERSION_CONFLICT":
        return "成员信息已更新，请刷新后重试。";
      case "PROJECT_QUOTA_STATE_CONFLICT":
        return "成员配额状态不一致，请刷新后重试；若问题持续，请联系管理员。";
      case "PROJECT_INVITATION_CONFLICT":
        return "该邀请已存在或刚刚被处理，请刷新后重试。";
      case "PROJECT_INVITATION_INVALID":
        return "邀请已失效、撤销或不适用于当前账户。";
      case "PROJECT_DELETION_STATE_CONFLICT":
        return "项目状态已变化，请刷新工作空间后重试。";
      case "PROJECT_VALIDATION_FAILED":
        return "项目信息不符合要求，请检查后重试。";
      case "AUTH_REQUIRED":
        return "登录状态已失效，请重新登录。";
      case "DATABASE_UNAVAILABLE":
      case "PROJECT_NETWORK_ERROR":
        return "项目服务暂时不可用，请稍后重试。";
      default:
        return "项目请求失败，请稍后重试。";
    }
  }
  return "项目请求失败，请稍后重试。";
}

export function formatProjectTime(value: string | null): string {
  if (!value) return "尚未进入";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "最近进入";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

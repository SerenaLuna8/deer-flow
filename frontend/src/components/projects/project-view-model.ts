import { zhCN, type Translations } from "@/core/i18n";
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

export function projectErrorMessage(
  error: unknown,
  messages: Translations["projectWorkspace"]["errors"] = zhCN.projectWorkspace
    .errors,
): string {
  if (error instanceof ProjectApiError) {
    switch (error.code) {
      case "PROJECT_SLUG_CONFLICT":
        return messages.slugConflict;
      case "PROJECT_NOT_FOUND":
      case "PROJECT_FORBIDDEN":
      case "PROJECT_OR_MEMBER_NOT_FOUND":
      case "PROJECT_MEMBERSHIP_FORBIDDEN":
        return messages.unavailable;
      case "PROJECT_LAST_ADMIN":
        return messages.lastAdmin;
      case "PROJECT_MEMBER_QUOTA_EXCEEDED":
        return messages.memberQuotaExceeded;
      case "PROJECT_MEMBERSHIP_VERSION_CONFLICT":
        return messages.membershipVersionConflict;
      case "PROJECT_QUOTA_STATE_CONFLICT":
        return messages.quotaStateConflict;
      case "PROJECT_INVITATION_CONFLICT":
        return messages.invitationConflict;
      case "PROJECT_INVITATION_INVALID":
        return messages.invitationInvalid;
      case "PROJECT_DELETION_STATE_CONFLICT":
        return messages.deletionStateConflict;
      case "PROJECT_VALIDATION_FAILED":
        return messages.validationFailed;
      case "AUTH_REQUIRED":
        return messages.authRequired;
      case "DATABASE_UNAVAILABLE":
      case "PROJECT_NETWORK_ERROR":
        return messages.serviceUnavailable;
      default:
        return messages.requestFailed;
    }
  }
  return messages.requestFailed;
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

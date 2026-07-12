import { ProjectApiError } from "@/core/projects/api";
import type { Project } from "@/core/projects/types";

export function canUpdateProject(project: Project): boolean {
  return project.capabilities.includes("project.update");
}

export function filterAndSortProjects(
  projects: readonly Project[],
  query: string,
): Project[] {
  const normalized = query.trim().toLocaleLowerCase();
  return [...projects]
    .filter(
      (project) =>
        !normalized ||
        project.display_name.toLocaleLowerCase().includes(normalized) ||
        project.slug.toLocaleLowerCase().includes(normalized),
    )
    .sort((left, right) => {
      if (left.is_pinned !== right.is_pinned) return left.is_pinned ? -1 : 1;
      const leftEntered = left.last_entered_at ?? "";
      const rightEntered = right.last_entered_at ?? "";
      return rightEntered.localeCompare(leftEntered);
    });
}

export function projectErrorMessage(error: unknown): string {
  if (error instanceof ProjectApiError) {
    switch (error.code) {
      case "PROJECT_SLUG_CONFLICT":
        return "这个项目标识已存在，请换一个后重试。";
      case "PROJECT_NOT_FOUND":
      case "PROJECT_FORBIDDEN":
        return "项目不可用或成员关系已失效，请返回项目工作台。";
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

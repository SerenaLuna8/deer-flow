import type { Locale } from "@/core/i18n";
import type { ProjectAuditItem } from "@/core/project-governance/audit";

type LocalizedLabel = Record<Locale, string>;
type AuditAction = ProjectAuditItem["action"];

const ACTION_LABELS: Record<AuditAction, LocalizedLabel> = {
  "project.created": { "zh-CN": "已创建项目", "en-US": "Project created" },
  "project.updated": {
    "zh-CN": "已更新项目信息",
    "en-US": "Project details updated",
  },
  "project.suspended": { "zh-CN": "已暂停项目", "en-US": "Project suspended" },
  "project.resumed": { "zh-CN": "已恢复项目", "en-US": "Project resumed" },
  "project.deletion_requested": {
    "zh-CN": "已申请删除项目",
    "en-US": "Project deletion requested",
  },
  "project.recovered": {
    "zh-CN": "已撤销项目删除",
    "en-US": "Project recovered",
  },
  "invitation.created": {
    "zh-CN": "已创建成员邀请",
    "en-US": "Member invitation created",
  },
  "invitation.revoked": {
    "zh-CN": "已撤销成员邀请",
    "en-US": "Member invitation revoked",
  },
  "invitation.redeemed": {
    "zh-CN": "成员已接受邀请",
    "en-US": "Member invitation accepted",
  },
  "member.joined": { "zh-CN": "成员已加入项目", "en-US": "Member joined" },
  "member.role_changed": {
    "zh-CN": "已调整成员角色",
    "en-US": "Member role changed",
  },
  "member.removed": { "zh-CN": "已移除成员", "en-US": "Member removed" },
  "member.left": { "zh-CN": "成员已离开项目", "en-US": "Member left" },
  "asset.created": { "zh-CN": "已创建资产", "en-US": "Asset created" },
  "asset.updated": { "zh-CN": "已更新资产", "en-US": "Asset updated" },
  "asset.published": {
    "zh-CN": "已发布资产版本",
    "en-US": "Asset version published",
  },
  "asset.deprecated": {
    "zh-CN": "已停用资产版本",
    "en-US": "Asset version deprecated",
  },
  "asset.deleted": {
    "zh-CN": "已删除资产",
    "en-US": "Asset deleted",
  },
  "asset.bound": { "zh-CN": "已绑定项目资产", "en-US": "Project asset bound" },
  "asset.unbound": {
    "zh-CN": "已解除资产绑定",
    "en-US": "Asset binding removed",
  },
  "asset.credential_created": {
    "zh-CN": "已创建资产凭据",
    "en-US": "Asset credential created",
  },
  "asset.credential_replaced": {
    "zh-CN": "已替换资产凭据",
    "en-US": "Asset credential replaced",
  },
  "asset.credential_revoked": {
    "zh-CN": "已撤销资产凭据",
    "en-US": "Asset credential revoked",
  },
  "asset.credential_deleted": {
    "zh-CN": "已删除资产凭据",
    "en-US": "Asset credential deleted",
  },
  "asset.credential_grants_migrated": {
    "zh-CN": "已迁移凭据授权",
    "en-US": "Credential grants migrated",
  },
  "automation.created": {
    "zh-CN": "已创建自动化",
    "en-US": "Automation created",
  },
  "automation.updated": {
    "zh-CN": "已更新自动化",
    "en-US": "Automation updated",
  },
  "automation.deleted": {
    "zh-CN": "已删除自动化",
    "en-US": "Automation deleted",
  },
  "automation.triggered": {
    "zh-CN": "已触发自动化",
    "en-US": "Automation triggered",
  },
  "quota.policy_updated": {
    "zh-CN": "已更新项目配额",
    "en-US": "Quota policy updated",
  },
  "quota.reconciled": {
    "zh-CN": "已校准项目用量",
    "en-US": "Project usage reconciled",
  },
  "run.admitted": { "zh-CN": "已接纳运行", "en-US": "Run admitted" },
  "run.cancel_requested": {
    "zh-CN": "已申请取消运行",
    "en-US": "Run cancellation requested",
  },
  "run.terminal": { "zh-CN": "运行已结束", "en-US": "Run finished" },
  "job.dead": { "zh-CN": "任务已停止重试", "en-US": "Job retries exhausted" },
  "job.requeued": { "zh-CN": "任务已重新入队", "en-US": "Job requeued" },
  "purge.completed": {
    "zh-CN": "已完成数据清理",
    "en-US": "Data purge completed",
  },
  "audit.corrected": {
    "zh-CN": "已更正审计记录",
    "en-US": "Audit record corrected",
  },
};

const ACTOR_LABELS: Record<ProjectAuditItem["actor"], LocalizedLabel> = {
  user: { "zh-CN": "用户", "en-US": "User" },
  gateway: { "zh-CN": "网关服务", "en-US": "Gateway" },
  worker: { "zh-CN": "执行服务", "en-US": "Worker" },
  scheduler: { "zh-CN": "调度服务", "en-US": "Scheduler" },
  operator: { "zh-CN": "平台运维", "en-US": "Operator" },
  migration: { "zh-CN": "迁移程序", "en-US": "Migration" },
  system_admin: { "zh-CN": "系统管理员", "en-US": "System administrator" },
};

const TARGET_LABELS: Record<ProjectAuditItem["target_kind"], LocalizedLabel> = {
  project: { "zh-CN": "项目", "en-US": "Project" },
  invitation: { "zh-CN": "邀请", "en-US": "Invitation" },
  membership: { "zh-CN": "成员关系", "en-US": "Membership" },
  asset: { "zh-CN": "资产", "en-US": "Asset" },
  automation: { "zh-CN": "自动化", "en-US": "Automation" },
  quota: { "zh-CN": "配额", "en-US": "Quota" },
  run: { "zh-CN": "运行", "en-US": "Run" },
  job: { "zh-CN": "任务", "en-US": "Job" },
  purge: { "zh-CN": "数据清理", "en-US": "Purge" },
  audit: { "zh-CN": "审计记录", "en-US": "Audit record" },
};

const OUTCOME_LABELS: Record<ProjectAuditItem["outcome"], LocalizedLabel> = {
  success: { "zh-CN": "成功", "en-US": "Succeeded" },
  rejected: { "zh-CN": "已拒绝", "en-US": "Rejected" },
  failed: { "zh-CN": "执行失败", "en-US": "Failed" },
};

const METADATA_LABELS: Record<string, LocalizedLabel> = {
  role: { "zh-CN": "角色", "en-US": "Role" },
  previous_role: { "zh-CN": "原角色", "en-US": "Previous role" },
  asset_kind: { "zh-CN": "资产类型", "en-US": "Asset type" },
  trigger_kind: { "zh-CN": "触发方式", "en-US": "Trigger" },
  member_limit: { "zh-CN": "成员上限", "en-US": "Member limit" },
  storage_bytes_limit: { "zh-CN": "存储上限", "en-US": "Storage limit" },
  concurrent_run_limit: {
    "zh-CN": "并发运行上限",
    "en-US": "Concurrent run limit",
  },
  mcp_calls_daily_limit: {
    "zh-CN": "每日 MCP 调用上限",
    "en-US": "Daily MCP call limit",
  },
  version: { "zh-CN": "策略版本", "en-US": "Policy version" },
  changed_dimensions: {
    "zh-CN": "校准维度数",
    "en-US": "Dimensions reconciled",
  },
  job_type: { "zh-CN": "任务类型", "en-US": "Job type" },
  non_interactive: { "zh-CN": "运行方式", "en-US": "Run mode" },
  status: { "zh-CN": "结束状态", "en-US": "Terminal status" },
  public_error_code: { "zh-CN": "公开错误代码", "en-US": "Public error code" },
  attempt_count: { "zh-CN": "尝试次数", "en-US": "Attempt count" },
  retry_safety: { "zh-CN": "重试安全性", "en-US": "Retry safety" },
  resource_kind: { "zh-CN": "资源类型", "en-US": "Resource type" },
  purged_count: { "zh-CN": "清理数量", "en-US": "Purged count" },
  correction_kind: { "zh-CN": "更正内容", "en-US": "Correction type" },
};

const VALUE_LABELS: Record<string, LocalizedLabel> = {
  admin: { "zh-CN": "管理员", "en-US": "Administrator" },
  editor: { "zh-CN": "编辑者", "en-US": "Editor" },
  runner: { "zh-CN": "运行者", "en-US": "Runner" },
  viewer: { "zh-CN": "查看者", "en-US": "Viewer" },
  agent: { "zh-CN": "Agent", "en-US": "Agent" },
  skill: { "zh-CN": "Skill", "en-US": "Skill" },
  mcp: { "zh-CN": "MCP", "en-US": "MCP" },
  manual: { "zh-CN": "手动", "en-US": "Manual" },
  scheduled: { "zh-CN": "定时", "en-US": "Scheduled" },
  private_run: { "zh-CN": "项目对话运行", "en-US": "Project chat run" },
  automation_run: { "zh-CN": "自动化运行", "en-US": "Automation run" },
  retention_purge: { "zh-CN": "保留期清理", "en-US": "Retention purge" },
  completed: { "zh-CN": "已完成", "en-US": "Completed" },
  failed: { "zh-CN": "失败", "en-US": "Failed" },
  cancelled: { "zh-CN": "已取消", "en-US": "Cancelled" },
  safe: { "zh-CN": "可安全重试", "en-US": "Safe to retry" },
  unknown: { "zh-CN": "待确认", "en-US": "Unknown" },
  unsafe: { "zh-CN": "不可安全重试", "en-US": "Unsafe to retry" },
  project: { "zh-CN": "项目", "en-US": "Project" },
  account: { "zh-CN": "账户", "en-US": "Account" },
  file: { "zh-CN": "文件", "en-US": "File" },
  outcome: { "zh-CN": "结果", "en-US": "Outcome" },
  metadata: { "zh-CN": "元数据", "en-US": "Metadata" },
  target: { "zh-CN": "目标", "en-US": "Target" },
};

const METADATA_ORDER = Object.keys(METADATA_LABELS);

function formatMetadataValue(
  key: string,
  value: unknown,
  locale: Locale,
): string {
  if (value === null) {
    return key === "public_error_code"
      ? locale === "zh-CN"
        ? "无"
        : "None"
      : locale === "zh-CN"
        ? "继承平台上限"
        : "Inherits platform limit";
  }
  if (key === "storage_bytes_limit" && typeof value === "number") {
    const units = ["B", "KiB", "MiB", "GiB", "TiB"] as const;
    if (value === 0) return "0 B";
    const index = Math.min(
      Math.floor(Math.log(value) / Math.log(1024)),
      units.length - 1,
    );
    const normalized = value / 1024 ** index;
    return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(normalized)} ${units[index]}`;
  }
  if (key === "non_interactive" && typeof value === "boolean") {
    if (locale === "zh-CN") return value ? "后台执行" : "交互运行";
    return value ? "Background" : "Interactive";
  }
  if (typeof value === "number")
    return new Intl.NumberFormat(locale).format(value);
  if (typeof value === "boolean") {
    if (locale === "zh-CN") return value ? "是" : "否";
    return value ? "Yes" : "No";
  }
  if (typeof value === "string" && VALUE_LABELS[value]) {
    return VALUE_LABELS[value][locale];
  }
  if (typeof value === "string") return value;
  return locale === "zh-CN" ? "不可用" : "Unavailable";
}

export interface AuditItemPresentation {
  action: string;
  actor: string;
  target: string;
  outcome: string;
  occurredAt: string;
  metadata: Array<{ label: string; value: string }>;
  publicErrorCode: string | null;
}

export function describeAuditItem(
  item: ProjectAuditItem,
  locale: Locale,
): AuditItemPresentation {
  const metadata = Object.entries(item.metadata)
    .filter(
      ([key]) => key !== "public_error_code" || item.public_error_code === null,
    )
    .sort(
      ([left], [right]) =>
        METADATA_ORDER.indexOf(left) - METADATA_ORDER.indexOf(right),
    )
    .map(([key, value]) => ({
      label: METADATA_LABELS[key]?.[locale] ?? key,
      value: formatMetadataValue(key, value, locale),
    }));

  return {
    action: ACTION_LABELS[item.action][locale],
    actor: ACTOR_LABELS[item.actor][locale],
    target: TARGET_LABELS[item.target_kind][locale],
    outcome: OUTCOME_LABELS[item.outcome][locale],
    occurredAt: new Intl.DateTimeFormat(locale, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(item.occurred_at)),
    metadata,
    publicErrorCode: item.public_error_code,
  };
}

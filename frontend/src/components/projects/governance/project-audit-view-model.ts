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
  "asset.exported": {
    "zh-CN": "已导出资产版本",
    "en-US": "Asset version exported",
  },
  "asset.bound": { "zh-CN": "已绑定项目资产", "en-US": "Project asset bound" },
  "asset.unbound": {
    "zh-CN": "已解除资产绑定",
    "en-US": "Asset binding removed",
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
  "run.files_finalized": {
    "zh-CN": "已完成运行文件入库",
    "en-US": "Run files finalized",
  },
  "run.terminal": { "zh-CN": "运行已结束", "en-US": "Run finished" },
  "host_execution.approval_requested": {
    "zh-CN": "已创建宿主机命令审批",
    "en-US": "Host command approval requested",
  },
  "host_execution.approval_available": {
    "zh-CN": "宿主机命令正在等待审批",
    "en-US": "Host command awaiting approval",
  },
  "host_execution.approval_decided": {
    "zh-CN": "已处理宿主机命令审批",
    "en-US": "Host command approval decided",
  },
  "host_execution.approval_claimed": {
    "zh-CN": "已领取一次性宿主机执行权限",
    "en-US": "One-time host execution authority claimed",
  },
  "host_execution.approval_terminal": {
    "zh-CN": "宿主机命令审批已结束",
    "en-US": "Host command approval finished",
  },
  "memory.remember": {
    "zh-CN": "已登记记忆条目",
    "en-US": "Memory entry remembered",
  },
  "memory.recall.executed": {
    "zh-CN": "已执行记忆检索",
    "en-US": "Memory recall executed",
  },
  "memory.seal.admitted": {
    "zh-CN": "已准入记忆空闲封存",
    "en-US": "Memory idle seal admitted",
  },
  "memory.seal.settled": {
    "zh-CN": "记忆空闲封存已结算",
    "en-US": "Memory idle seal settled",
  },
  "memory.dream.admitted": {
    "zh-CN": "记忆整理已准入",
    "en-US": "Memory organization admitted",
  },
  "memory.dream.settled": {
    "zh-CN": "记忆整理已结算",
    "en-US": "Memory organization settled",
  },
  "memory.injection.skipped": {
    "zh-CN": "记忆注入已降级跳过",
    "en-US": "Memory injection skipped",
  },
  "memory.dream.review_flagged": {
    "zh-CN": "记忆整理已标记复核",
    "en-US": "Memory organization flagged for review",
  },
  "memory.restore.executed": {
    "zh-CN": "已恢复记忆版本",
    "en-US": "Memory version restored",
  },
  "memory.reset.executed": {
    "zh-CN": "已重置账户记忆",
    "en-US": "Account Memory reset",
  },
  "job.dead": { "zh-CN": "作业已停止重试", "en-US": "Job retries exhausted" },
  "job.requeued": { "zh-CN": "作业已重新入队", "en-US": "Job requeued" },
  "purge.completed": {
    "zh-CN": "已完成数据清理",
    "en-US": "Data purge completed",
  },
  "audit.corrected": {
    "zh-CN": "已更正审计记录",
    "en-US": "Audit record corrected",
  },
  "system_setting.updated": {
    "zh-CN": "已更新系统设置",
    "en-US": "System setting updated",
  },
  "knowledge_settings.update": {
    "zh-CN": "已更新知识库配置",
    "en-US": "Knowledge settings updated",
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
  job: { "zh-CN": "作业", "en-US": "Job" },
  purge: { "zh-CN": "数据清理", "en-US": "Purge" },
  audit: { "zh-CN": "审计记录", "en-US": "Audit record" },
  system_setting: { "zh-CN": "系统设置", "en-US": "System setting" },
  account: { "zh-CN": "账户", "en-US": "Account" },
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
  operation: { "zh-CN": "具体操作", "en-US": "Operation" },
  definition_revision: {
    "zh-CN": "Definition 修订",
    "en-US": "Definition revision",
  },
  version_number: { "zh-CN": "资产版本", "en-US": "Asset version" },
  affected_agent_count: {
    "zh-CN": "受影响 Agent 数",
    "en-US": "Affected Agents",
  },
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
  version: { "zh-CN": "版本", "en-US": "Version" },
  changed_dimensions: {
    "zh-CN": "校准维度数",
    "en-US": "Dimensions reconciled",
  },
  job_type: { "zh-CN": "作业类型", "en-US": "Job type" },
  non_interactive: { "zh-CN": "运行方式", "en-US": "Run mode" },
  created_count: { "zh-CN": "新增文件数", "en-US": "Files created" },
  modified_count: { "zh-CN": "修改文件数", "en-US": "Files modified" },
  deleted_count: { "zh-CN": "删除文件数", "en-US": "Files deleted" },
  artifact_count: { "zh-CN": "产物数", "en-US": "Artifacts" },
  committed_bytes: { "zh-CN": "提交字节数", "en-US": "Committed bytes" },
  status: { "zh-CN": "结束状态", "en-US": "Terminal status" },
  decision: { "zh-CN": "审批决定", "en-US": "Approval decision" },
  origin: { "zh-CN": "准入来源", "en-US": "Admission origin" },
  trigger: { "zh-CN": "整理触发方式", "en-US": "Organization trigger" },
  history_count: { "zh-CN": "历史条目数", "en-US": "History entries" },
  disposition: { "zh-CN": "结算结果", "en-US": "Disposition" },
  source_version: { "zh-CN": "来源版本", "en-US": "Source version" },
  previous_version: { "zh-CN": "恢复前版本", "en-US": "Previous version" },
  published_version: { "zh-CN": "发布版本", "en-US": "Published version" },
  changed: { "zh-CN": "内容有变化", "en-US": "Content changed" },
  scope: { "zh-CN": "重置范围", "en-US": "Reset scope" },
  projects_affected: { "zh-CN": "受影响项目数", "en-US": "Projects affected" },
  scopes_reset: { "zh-CN": "重置记忆范围数", "en-US": "Memory scopes reset" },
  history_entries: { "zh-CN": "历史条目数", "en-US": "History entries" },
  documents: { "zh-CN": "文档数", "en-US": "Documents" },
  versions: { "zh-CN": "版本数", "en-US": "Versions" },
  dream_runs: { "zh-CN": "整理运行数", "en-US": "Organization runs" },
  prepare_runs: {
    "zh-CN": "Dream 准备运行数",
    "en-US": "Dream preparation runs",
  },
  snapshots: { "zh-CN": "运行快照数", "en-US": "Run snapshots" },
  episodes: { "zh-CN": "归档条目数", "en-US": "Archived episodes" },
  jobs_cancelled: { "zh-CN": "取消作业数", "en-US": "Jobs cancelled" },
  result_bucket: { "zh-CN": "检索结果数", "en-US": "Result count" },
  matched_stage: { "zh-CN": "命中阶段", "en-US": "Matched stage" },
  tags_filtered: { "zh-CN": "标签过滤", "en-US": "Tag filter applied" },
  query_len_bucket: { "zh-CN": "查询长度", "en-US": "Query length" },
  public_error_code: { "zh-CN": "公开错误代码", "en-US": "Public error code" },
  attempt_count: { "zh-CN": "尝试次数", "en-US": "Attempt count" },
  retry_safety: { "zh-CN": "重试安全性", "en-US": "Retry safety" },
  resource_kind: { "zh-CN": "资源类型", "en-US": "Resource type" },
  purged_count: { "zh-CN": "清理数量", "en-US": "Purged count" },
  correction_kind: { "zh-CN": "更正内容", "en-US": "Correction type" },
  section: { "zh-CN": "设置分区", "en-US": "Section" },
  revision: { "zh-CN": "修订版本", "en-US": "Revision" },
  schema_version: { "zh-CN": "结构版本", "en-US": "Schema version" },
  payload_checksum: { "zh-CN": "配置校验和", "en-US": "Payload checksum" },
  effect_scope: { "zh-CN": "生效范围", "en-US": "Effect scope" },
};

const VALUE_LABELS: Record<string, LocalizedLabel> = {
  admin: { "zh-CN": "管理员", "en-US": "Administrator" },
  editor: { "zh-CN": "编辑者", "en-US": "Editor" },
  runner: { "zh-CN": "运行者", "en-US": "Runner" },
  viewer: { "zh-CN": "查看者", "en-US": "Viewer" },
  agent: { "zh-CN": "智能体", "en-US": "Agent" },
  skill: { "zh-CN": "技能", "en-US": "Skill" },
  mcp: { "zh-CN": "工具", "en-US": "MCP" },
  manual: { "zh-CN": "手动", "en-US": "Manual" },
  scheduled: { "zh-CN": "定时", "en-US": "Scheduled" },
  auto_dream: { "zh-CN": "自动整理", "en-US": "Automatic organization" },
  manual_dream: { "zh-CN": "手动整理", "en-US": "Manual organization" },
  budget_rewrite: { "zh-CN": "预算压缩", "en-US": "Budget rewrite" },
  published: { "zh-CN": "已发布", "en-US": "Published" },
  dead: { "zh-CN": "停止重试", "en-US": "Retries exhausted" },
  private_run: { "zh-CN": "项目对话运行", "en-US": "Project chat run" },
  automation_run: { "zh-CN": "自动化运行", "en-US": "Automation run" },
  retention_purge: { "zh-CN": "保留期清理", "en-US": "Retention purge" },
  mcp_discovery: { "zh-CN": "MCP 工具发现", "en-US": "MCP tool discovery" },
  memory_dream: { "zh-CN": "Dream 记忆整理", "en-US": "Memory Dream" },
  memory_dream_prepare: {
    "zh-CN": "Dream 记忆准备",
    "en-US": "Memory Dream preparation",
  },
  memory_seal: { "zh-CN": "记忆空闲封存", "en-US": "Memory idle seal" },
  exact: { "zh-CN": "精确匹配", "en-US": "Exact match" },
  similarity: { "zh-CN": "相似度匹配", "en-US": "Similarity match" },
  none: { "zh-CN": "无命中", "en-US": "No match" },
  completed: { "zh-CN": "已完成", "en-US": "Completed" },
  failed: { "zh-CN": "失败", "en-US": "Failed" },
  cancelled: { "zh-CN": "已取消", "en-US": "Cancelled" },
  allow_once: { "zh-CN": "允许一次", "en-US": "Allow once" },
  deny: { "zh-CN": "拒绝", "en-US": "Deny" },
  finished: { "zh-CN": "执行完成", "en-US": "Execution finished" },
  launch_failed: { "zh-CN": "启动失败", "en-US": "Launch failed" },
  expired: { "zh-CN": "已过期", "en-US": "Expired" },
  safe: { "zh-CN": "可安全重试", "en-US": "Safe to retry" },
  unknown: { "zh-CN": "待确认", "en-US": "Unknown" },
  unsafe: { "zh-CN": "不可安全重试", "en-US": "Unsafe to retry" },
  project: { "zh-CN": "项目", "en-US": "Project" },
  account: { "zh-CN": "账户", "en-US": "Account" },
  file: { "zh-CN": "文件", "en-US": "File" },
  former_owner: { "zh-CN": "原所有者", "en-US": "Former owner" },
  outcome: { "zh-CN": "结果", "en-US": "Outcome" },
  metadata: { "zh-CN": "元数据", "en-US": "Metadata" },
  target: { "zh-CN": "目标", "en-US": "Target" },
  agent_runtime: { "zh-CN": "Agent 运行时", "en-US": "Agent runtime" },
  auth: { "zh-CN": "认证", "en-US": "Authentication" },
  quotas: { "zh-CN": "配额", "en-US": "Quotas" },
  memory_document: { "zh-CN": "记忆文档", "en-US": "Memory document" },
  new_requests_and_runs: {
    "zh-CN": "新请求与新运行",
    "en-US": "New requests and runs",
  },
  new_requests: { "zh-CN": "新请求", "en-US": "New requests" },
  new_memory_documents: {
    "zh-CN": "新记忆文档",
    "en-US": "New Memory documents",
  },
  next_authoritative_check: {
    "zh-CN": "下次权威校验",
    "en-US": "Next authoritative check",
  },
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

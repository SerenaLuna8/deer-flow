"use client";

import {
  ArrowDownIcon,
  ArrowUpIcon,
  BotIcon,
  BrainIcon,
  CalendarClockIcon,
  CheckCircle2Icon,
  ChevronDownIcon,
  DatabaseIcon,
  GaugeIcon,
  ImageIcon,
  InfoIcon,
  MessageSquareIcon,
  RefreshCwIcon,
  SaveIcon,
  ShieldCheckIcon,
  Undo2Icon,
  UserPlusIcon,
  UsersIcon,
  WrenchIcon,
  XIcon,
} from "lucide-react";
import { useRouter } from "next/navigation";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import type { z } from "zod";

import {
  AdminPage,
  AdminPageHeader,
  AdminSection,
} from "@/components/admin/ui/admin-page";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
  InputGroupText,
} from "@/components/ui/input-group";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  AdminSystemSettingsApiError,
  agentRuntimeSettingsValueSchema,
  authSettingsValueSchema,
  automationsSettingsValueSchema,
  memoryDocumentSettingsValueSchema,
  quotaSettingsValueSchema,
  useAdminSystemSettings,
  useReplaceAdminSystemSettingsSection,
  validateAgentRuntimeModelReferences,
  type AgentRuntimeSettingsValue,
  type AuthSettingsValue,
  type AutomationsSettingsValue,
  type MemoryDocumentSettingsValue,
  type QuotaSettingsValue,
  type SystemSettingsCatalog,
  type SystemSettingsEffectScope,
  type SystemSettingsMutationResponse,
  type SystemSettingsSectionName,
  type SystemSettingsSectionValueMap,
} from "@/core/admin-settings/system";
import { useAuth } from "@/core/auth/AuthProvider";
import type { Locale } from "@/core/i18n";
import { useI18n } from "@/core/i18n/hooks";
import { useModels } from "@/core/models/hooks";
import type { Model } from "@/core/models/types";
import { cn } from "@/lib/utils";

export type AdminSystemSettingsCatalogState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: SystemSettingsCatalog };

type ModelsStatus = "error" | "loading" | "ready";
type SectionErrors = Partial<Record<SystemSettingsSectionName, string>>;
type LastResults = Partial<
  Record<SystemSettingsSectionName, SystemSettingsMutationResponse>
>;
type SaveSection = (
  section: SystemSettingsSectionName,
  value: SystemSettingsSectionValueMap[SystemSettingsSectionName],
  expectedRevision: number,
) => Promise<SystemSettingsMutationResponse | null>;

type ContextSize = NonNullable<
  AgentRuntimeSettingsValue["summarization"]["trigger"]
>[number];

type LocalizedCopy = {
  en: string;
  hintEn?: string;
  hintZh?: string;
  unit?: string;
  zh: string;
};

const GIB = 1024 ** 3;

const FIELD_COPY: Record<string, LocalizedCopy> = {
  "auth.allow_registration": {
    zh: "开放本地账号注册",
    en: "Allow local account registration",
    hintZh: "开启后，访客可以创建本地账号；管理员初始化和 OIDC 不受影响。",
    hintEn:
      "When enabled, visitors can create local accounts. Administrator setup and OIDC are unchanged.",
  },
  "automations.enabled": {
    zh: "启用自动轮询",
    en: "Enable scheduled polling",
    hintZh:
      "关闭后 Scheduler 不再自动准入到期的自动化，项目内手动触发仍然可用。",
    hintEn:
      "When off, the Scheduler stops admitting due Automations. Manual project triggers still work.",
  },
  "automations.poll_interval_seconds": {
    zh: "轮询间隔",
    en: "Poll interval",
    unit: "秒",
    hintZh: "Scheduler 检查到期自动化的间隔，范围 1–300 秒。",
    hintEn:
      "How often the Scheduler looks for due Automations (1–300 seconds).",
  },
  "automations.max_concurrent_runs": {
    zh: "全局并发自动化上限",
    en: "Global concurrent Automation limit",
    unit: "个",
    hintZh:
      "计划触发和手动触发共用的进行中自动化上限，范围 1–32。收紧后不影响已准入的运行。",
    hintEn:
      "Shared cap for scheduled and manual in-flight Automations (1–32). Tightening does not interrupt already admitted work.",
  },
  "automations.min_once_delay_seconds": {
    zh: "一次性计划最短提前量",
    en: "Minimum one-time delay",
    unit: "秒",
    hintZh: "一次性计划必须晚于当前时间的最短秒数，范围 0–86400。",
    hintEn:
      "Minimum seconds into the future accepted for one-time schedules (0–86400).",
  },
  "quotas.default_member_limit": {
    zh: "默认项目成员上限",
    en: "Default project member limit",
    unit: "人",
    hintZh: "新项目采用的成员容量默认值。",
    hintEn: "The initial member capacity assigned to new projects.",
  },
  "quotas.default_storage_bytes_limit": {
    zh: "默认项目存储空间",
    en: "Default project storage",
    unit: "GiB",
    hintZh: "新项目可使用的文件与工作区存储总量。",
    hintEn: "Total file and workspace storage assigned to new projects.",
  },
  "quotas.default_concurrent_run_limit": {
    zh: "默认并发任务上限",
    en: "Default concurrent Run limit",
    unit: "个",
    hintZh: "一个项目可同时运行的任务数量。",
    hintEn: "How many Runs a project can execute at the same time.",
  },
  "quotas.default_mcp_calls_daily_limit": {
    zh: "默认每日 MCP 调用上限",
    en: "Default daily MCP call limit",
    unit: "次/天",
    hintZh: "项目每天可发起的 MCP 工具调用总数。",
    hintEn: "Total MCP tool calls a project can make each day.",
  },
  "quotas.warning_threshold": {
    zh: "配额用量预警线",
    en: "Quota usage warning",
    unit: "%",
    hintZh: "达到此用量比例时向管理员提示。",
    hintEn: "Administrators are warned when usage reaches this percentage.",
  },
  "agent_runtime.token_usage.enabled": {
    zh: "记录 Token 用量",
    en: "Track Token usage",
    hintZh: "在任务与子 Agent 中记录并展示输入、输出和总 Token。",
    hintEn:
      "Track and display input, output, and total Tokens for Runs and subagents.",
  },
  "agent_runtime.token_budget.enabled": {
    zh: "启用单次 Run Token 预算",
    en: "Enable per-Run Token budget",
    hintZh: "对新任务设置用量预警和强制收尾边界。",
    hintEn: "Apply warning and hard-stop boundaries to newly admitted Runs.",
  },
  "agent_runtime.token_budget.max_tokens": {
    zh: "总 Token 上限",
    en: "Total Token limit",
    unit: "Token",
  },
  "agent_runtime.token_budget.max_input_tokens": {
    zh: "输入 Token 上限（可选）",
    en: "Input Token limit (optional)",
    unit: "Token",
  },
  "agent_runtime.token_budget.max_output_tokens": {
    zh: "输出 Token 上限（可选）",
    en: "Output Token limit (optional)",
    unit: "Token",
  },
  "agent_runtime.token_budget.warn_threshold": {
    zh: "用量预警线",
    en: "Usage warning",
    unit: "%",
    hintZh: "达到此比例时提示 Agent 尽快收尾。",
    hintEn: "Ask the Agent to wrap up after usage reaches this percentage.",
  },
  "agent_runtime.token_budget.hard_stop_threshold": {
    zh: "强制收尾线",
    en: "Hard-stop threshold",
    unit: "%",
    hintZh: "达到此比例时停止继续消耗预算。",
    hintEn: "Stop further budget consumption at this percentage.",
  },
  "agent_runtime.max_recursion_limit": {
    zh: "单次 Run 最大执行步数",
    en: "Maximum steps per Run",
    unit: "步",
  },
  "agent_runtime.vision_bridge.model_name": {
    zh: "视觉模型",
    en: "Vision model",
    hintZh:
      "选择支持视觉输入的模型后，为不支持视觉输入的主模型提供 inspect_image 工具；留空即关闭，不会回退到系统默认模型。",
    hintEn:
      "Select a model that supports vision input to expose inspect_image to lead models without vision input. Empty means off and never falls back to the system default model.",
  },
  "agent_runtime.vision_bridge.timeout_seconds": {
    zh: "单次图片识别超时",
    en: "Image inspection timeout",
    unit: "秒",
    hintZh: "从读取图片到返回结构化证据共用一个截止时间，范围 5–120 秒。",
    hintEn:
      "One deadline covers image reading through structured evidence (5–120 seconds).",
  },
  "agent_runtime.vision_bridge.contract_version": {
    zh: "图片识别结果版本",
    en: "Image inspection result version",
    hintZh: "固定版本，由服务端校验图片识别返回的结构化结果。",
    hintEn:
      "Fixed version used by the server to validate structured image-inspection results.",
  },
  "agent_runtime.subagents.max_total_per_run": {
    zh: "每个 Run 最多调用子 Agent",
    en: "Maximum subagents per Run",
    unit: "个",
  },
  "agent_runtime.title.enabled": {
    zh: "自动生成对话标题",
    en: "Generate conversation titles",
    hintZh: "新对话开始后自动生成便于识别的标题。",
    hintEn: "Generate a recognizable title after a new conversation starts.",
  },
  "agent_runtime.title.max_words": {
    zh: "标题最多词数",
    en: "Maximum title words",
    unit: "词",
  },
  "agent_runtime.title.max_chars": {
    zh: "标题最多字符数",
    en: "Maximum title characters",
    unit: "字符",
  },
  "agent_runtime.title.model_name": {
    zh: "标题生成模型",
    en: "Title model",
    hintZh: "留空则使用系统默认模型。模型调用失败时回退到本地标题。",
    hintEn:
      "Leave empty to use the system default model. Falls back to a local title if the model call fails.",
  },
  "agent_runtime.suggestions.enabled": {
    zh: "显示后续问题建议",
    en: "Show follow-up suggestions",
    hintZh: "回答完成后提供可继续追问的建议。",
    hintEn: "Offer useful follow-up prompts after an answer completes.",
  },
  "agent_runtime.input_polish.enabled": {
    zh: "启用输入润色",
    en: "Enable input polish",
    hintZh: "允许用户在发送前改写和澄清输入。",
    hintEn: "Allow users to rewrite and clarify a prompt before sending.",
  },
  "agent_runtime.input_polish.max_chars": {
    zh: "最长润色文本",
    en: "Maximum polish input",
    unit: "字符",
  },
  "agent_runtime.input_polish.model_name": {
    zh: "输入润色模型",
    en: "Input polish model",
  },
  "agent_runtime.summarization.enabled": {
    zh: "自动压缩长对话",
    en: "Summarize long conversations",
    hintZh: "上下文达到阈值后自动生成摘要，避免对话超出模型窗口。",
    hintEn:
      "Create a summary when context reaches a limit to stay within the model window.",
  },
  "agent_runtime.summarization.model_name": {
    zh: "摘要模型",
    en: "Summarization model",
  },
  "agent_runtime.summarization.trim_tokens_to_summarize": {
    zh: "单次待摘要内容上限",
    en: "Maximum content per summary",
    unit: "Token",
  },
  "agent_runtime.summarization.skill_file_read_tool_names": {
    zh: "视为 Skill 文件读取的工具",
    en: "Skill file-read tools",
    hintZh: "这些工具的读取结果会按 Skill 文件内容参与摘要。使用英文逗号分隔。",
    hintEn:
      "Results from these tools are summarized as Skill file content. Separate with commas.",
  },
  "agent_runtime.memory.enabled": {
    zh: "启用记忆",
    en: "Enable Memory",
    hintZh: "允许学习、Dream 整理和在新 Run 中召回用户私有的长期记忆文档。",
    hintEn:
      "Allow learning, Dream organization, and document recall for new Runs.",
  },
  "agent_runtime.memory.model_name": {
    zh: "Dream 模型",
    en: "Dream model",
  },
  "agent_runtime.memory.dream_interval_minutes": {
    zh: "自动 Dream 周期",
    en: "Automatic Dream interval",
    unit: "分钟",
  },
  "agent_runtime.memory.max_injection_tokens": {
    zh: "记忆文档注入上限",
    en: "Memory document injection limit",
    unit: "Token",
  },
  "agent_runtime.memory.idle_seal_minutes": {
    zh: "空闲封存阈值",
    en: "Idle seal threshold",
    unit: "分钟",
    hintZh: "线程空闲超过该时长后自动封存未归档回合进记忆，0 表示关闭。",
    hintEn:
      "Idle threads older than this are sealed into Memory capture; 0 disables.",
  },
  "agent_runtime.memory.episode_retention_days": {
    zh: "归档条目保留期",
    en: "Archived episode retention",
    unit: "天",
    hintZh: "历史归档条目的可检索保留天数，0 表示永久保留。",
    hintEn: "Days archived episodes stay searchable; 0 keeps them forever.",
  },
  "agent_runtime.tool_search.enabled": {
    zh: "启用工具按需发现",
    en: "Enable on-demand tool discovery",
    hintZh: "仅在需要时向 Agent 推荐相关工具。",
    hintEn: "Recommend relevant tools to the Agent only when needed.",
  },
  "agent_runtime.tool_search.auto_promote_top_k": {
    zh: "每次自动推荐工具数",
    en: "Tools promoted per search",
    unit: "个",
  },
  "agent_runtime.tool_output.enabled": {
    zh: "自动外置超长工具结果",
    en: "Externalize long tool results",
    hintZh: "把过长结果保存为文件，只在对话中保留首尾预览。",
    hintEn:
      "Save long results as files and keep only a head-and-tail preview in context.",
  },
  "agent_runtime.tool_output.externalize_min_chars": {
    zh: "超过此长度时外置",
    en: "Externalize after",
    unit: "字符",
  },
  "agent_runtime.tool_output.preview_head_chars": {
    zh: "预览开头保留",
    en: "Preview head",
    unit: "字符",
  },
  "agent_runtime.tool_output.preview_tail_chars": {
    zh: "预览结尾保留",
    en: "Preview tail",
    unit: "字符",
  },
  "agent_runtime.tool_output.fallback_max_chars": {
    zh: "无法外置时最大保留长度",
    en: "Fallback maximum length",
    unit: "字符",
  },
  "agent_runtime.tool_output.fallback_head_chars": {
    zh: "回退预览开头保留",
    en: "Fallback preview head",
    unit: "字符",
  },
  "agent_runtime.tool_output.fallback_tail_chars": {
    zh: "回退预览结尾保留",
    en: "Fallback preview tail",
    unit: "字符",
  },
  "agent_runtime.tool_output.exempt_tools": {
    zh: "始终保留完整结果的工具",
    en: "Tools exempt from externalization",
    hintZh: "使用英文逗号分隔。",
    hintEn: "Separate values with commas.",
  },
  "agent_runtime.loop_detection.enabled": {
    zh: "检测重复工具调用",
    en: "Detect repeated tool calls",
    hintZh: "发现循环或异常高频调用时提醒并阻止继续执行。",
    hintEn:
      "Warn and stop execution when repeated or unusually frequent calls are detected.",
  },
  "agent_runtime.loop_detection.warn_threshold": {
    zh: "重复调用提醒阈值",
    en: "Repeated-call warning",
    unit: "次",
  },
  "agent_runtime.loop_detection.hard_limit": {
    zh: "重复调用终止阈值",
    en: "Repeated-call hard limit",
    unit: "次",
  },
  "agent_runtime.loop_detection.window_size": {
    zh: "检测最近操作数",
    en: "Recent operations inspected",
    unit: "次",
  },
  "agent_runtime.loop_detection.max_tracked_threads": {
    zh: "最多跟踪任务数",
    en: "Maximum tracked Runs",
    unit: "个",
  },
  "agent_runtime.loop_detection.tool_freq_warn": {
    zh: "同一工具调用次数预警",
    en: "Tool-frequency warning",
    unit: "次",
  },
  "agent_runtime.loop_detection.tool_freq_hard_limit": {
    zh: "同一工具调用次数上限",
    en: "Tool-frequency hard limit",
    unit: "次",
  },
  "agent_runtime.read_before_write.enabled": {
    zh: "修改已有文件前必须先读取",
    en: "Require read before write",
    hintZh: "减少基于过期内容覆盖文件的风险。",
    hintEn: "Reduce the risk of overwriting files from stale context.",
  },
  "agent_runtime.safety_finish_reason.enabled": {
    zh: "阻止被安全策略截断的工具调用",
    en: "Block safety-truncated tool calls",
    hintZh: "模型因安全策略终止时，不继续执行未完成的工具请求。",
    hintEn: "Do not execute incomplete tool requests after a safety stop.",
  },
};

function fieldCopy(name: string, locale: Locale): LocalizedCopy {
  const copy = FIELD_COPY[name];
  if (copy) return copy;
  return locale === "zh-CN"
    ? { zh: "高级配置项", en: "Advanced setting" }
    : { zh: "高级配置项", en: "Advanced setting" };
}

function localizedText(copy: LocalizedCopy, locale: Locale): string {
  return locale === "zh-CN" ? copy.zh : copy.en;
}

function localizedHint(
  copy: LocalizedCopy,
  locale: Locale,
): string | undefined {
  return locale === "zh-CN" ? copy.hintZh : copy.hintEn;
}

function localizedUnit(
  copy: LocalizedCopy,
  locale: Locale,
): string | undefined {
  if (locale === "zh-CN" || !copy.unit) return copy.unit;
  return (
    {
      人: "people",
      个: "items",
      步: "steps",
      词: "words",
      字符: "characters",
      秒: "seconds",
      分钟: "minutes",
      条: "items",
      天: "days",
      次: "times",
      "次/天": "calls/day",
    }[copy.unit] ?? copy.unit
  );
}

function formatUpdatedAt(value: string, locale: Locale): string {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function effectLabel(
  scope: SystemSettingsEffectScope,
  labels: ReturnType<typeof useI18n>["t"]["adminSystemSettings"]["effects"],
): string {
  switch (scope) {
    case "new_requests":
      return labels.newRequests;
    case "new_runs":
      return labels.newRuns;
    case "new_requests_and_runs":
      return labels.newRequestsAndRuns;
    case "new_memory_documents":
      return labels.newMemoryDocuments;
    case "next_authoritative_check":
      return labels.nextAuthoritativeCheck;
    case "restart_required":
      return labels.restartRequired;
  }
}

export function SystemSettingsEffectBadge({
  scope,
}: {
  scope: SystemSettingsEffectScope;
}) {
  const labels = useI18n().t.adminSystemSettings.effects;
  return (
    <Badge
      variant="outline"
      data-effect-scope={scope}
      className={cn(
        "bg-muted/30",
        scope === "restart_required" &&
          "border-warning/40 bg-warning/10 text-warning-foreground",
      )}
    >
      {effectLabel(scope, labels)}
    </Badge>
  );
}

function safeActionError(error: unknown, fallback: string): string {
  if (error instanceof AdminSystemSettingsApiError) {
    if (error.code === "AUTH_REQUIRED") return "auth";
    if (error.status === 409) return "conflict";
    if (error.status === 422) return "invalid";
  }
  return fallback;
}

function FieldShell({
  children,
  disabled = false,
  label,
  name,
  hint,
}: {
  children: ReactNode;
  disabled?: boolean;
  label?: string;
  name: string;
  hint?: string;
}) {
  const { locale } = useI18n();
  const copy = fieldCopy(name, locale);
  const resolvedHint = hint ?? localizedHint(copy, locale);
  const displayHint =
    resolvedHint ??
    (locale === "zh-CN" ? "平台级默认值。" : "Platform-wide default.");
  return (
    <label
      data-setting-key={name}
      data-settings-field-row={name}
      className={cn(
        "border-border/70 bg-background grid min-h-20 min-w-0 gap-3 rounded-lg border px-4 py-3 text-sm sm:grid-cols-[minmax(0,1fr)_minmax(14rem,20rem)] sm:items-center",
        disabled && "text-muted-foreground",
      )}
    >
      <span className="min-w-0">
        <span className="block font-medium [overflow-wrap:anywhere]">
          {label ?? localizedText(copy, locale)}
        </span>
        <span
          id={`${name.replaceAll(".", "-")}-hint`}
          className="text-muted-foreground mt-1 block text-xs leading-5"
        >
          {displayHint}
        </span>
      </span>
      <div className="min-w-0">{children}</div>
    </label>
  );
}

function BooleanField({
  checked,
  disabled = false,
  label,
  name,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label?: string;
  name: string;
  onChange: (checked: boolean) => void;
}) {
  const { locale } = useI18n();
  const copy = fieldCopy(name, locale);
  const controlId = `${name.replaceAll(".", "-")}-control`;
  const hintId = `${name.replaceAll(".", "-")}-hint`;
  const hint = localizedHint(copy, locale);
  return (
    <div
      data-setting-key={name}
      data-settings-field-row={name}
      className={cn(
        "border-border/70 bg-background grid min-h-20 min-w-0 gap-3 rounded-lg border px-4 py-3 text-sm transition-colors sm:grid-cols-[minmax(0,1fr)_minmax(14rem,20rem)] sm:items-center",
        !disabled && "hover:bg-muted/20",
        disabled && "opacity-60",
      )}
    >
      <div className="min-w-0 flex-1">
        <label htmlFor={controlId} className="font-medium">
          {label ?? localizedText(copy, locale)}
        </label>
        {hint ? (
          <p
            id={hintId}
            className="text-muted-foreground mt-1 text-xs leading-5"
          >
            {hint}
          </p>
        ) : null}
      </div>
      <div className="flex min-h-9 items-center justify-end">
        <Switch
          id={controlId}
          name={name}
          checked={checked}
          disabled={disabled}
          aria-describedby={hint ? hintId : undefined}
          aria-label={label ?? localizedText(copy, locale)}
          onCheckedChange={onChange}
        />
      </div>
    </div>
  );
}

function NumberField({
  dataDependency,
  disabled = false,
  label,
  max,
  min,
  name,
  onChange,
  scale = 1,
  step = 1,
  unit,
  value,
}: {
  dataDependency?: string;
  disabled?: boolean;
  label?: string;
  max?: number;
  min?: number;
  name: string;
  onChange: (value: number) => void;
  scale?: number;
  step?: number;
  unit?: string;
  value: number;
}) {
  const { locale } = useI18n();
  const copy = fieldCopy(name, locale);
  const resolvedUnit = unit ?? localizedUnit(copy, locale);
  return (
    <FieldShell label={label} name={name} disabled={disabled}>
      <InputGroup data-disabled={disabled || undefined}>
        <InputGroupInput
          type="number"
          name={name}
          data-dependency={dataDependency}
          value={Number.isFinite(value) ? value / scale : ""}
          min={min}
          max={max}
          step={step}
          disabled={disabled}
          aria-describedby={`${name.replaceAll(".", "-")}-hint`}
          onChange={(event) => {
            const next = event.currentTarget.valueAsNumber;
            if (Number.isFinite(next)) onChange(next * scale);
          }}
        />
        {resolvedUnit ? (
          <InputGroupAddon align="inline-end">
            <InputGroupText>{resolvedUnit}</InputGroupText>
          </InputGroupAddon>
        ) : null}
      </InputGroup>
    </FieldShell>
  );
}

function NullableNumberField({
  disabled = false,
  max,
  min,
  name,
  onChange,
  unit,
  value,
}: {
  disabled?: boolean;
  max?: number;
  min?: number;
  name: string;
  onChange: (value: number | null) => void;
  unit?: string;
  value: number | null;
}) {
  const { locale } = useI18n();
  const copy = fieldCopy(name, locale);
  const resolvedUnit = unit ?? localizedUnit(copy, locale);
  return (
    <FieldShell name={name} disabled={disabled}>
      <InputGroup data-disabled={disabled || undefined}>
        <InputGroupInput
          type="number"
          name={name}
          value={value ?? ""}
          min={min}
          max={max}
          step="1"
          disabled={disabled}
          aria-describedby={`${name.replaceAll(".", "-")}-hint`}
          onChange={(event) => {
            if (event.currentTarget.value === "") {
              onChange(null);
              return;
            }
            const next = event.currentTarget.valueAsNumber;
            if (Number.isFinite(next)) onChange(next);
          }}
        />
        {resolvedUnit ? (
          <InputGroupAddon align="inline-end">
            <InputGroupText>{resolvedUnit}</InputGroupText>
          </InputGroupAddon>
        ) : null}
      </InputGroup>
    </FieldShell>
  );
}

export function formatSystemDefaultModelOption(
  fallbackLabel: string,
  defaultModelDisplayName: string | undefined,
  locale: Locale = "en-US",
): string {
  if (!defaultModelDisplayName) return fallbackLabel;
  if (locale === "zh-CN") {
    return `${fallbackLabel}（${defaultModelDisplayName}）`;
  }
  return `${fallbackLabel} (${defaultModelDisplayName})`;
}

export function selectVisionInputModels(models: Model[]): Model[] {
  return models.filter((model) => model.supports_vision);
}

function ModelField({
  activeModels,
  disabled = false,
  emptyOptionLabel,
  modelsStatus,
  name,
  onChange,
  value,
}: {
  activeModels: Model[];
  disabled?: boolean;
  emptyOptionLabel?: string;
  modelsStatus: ModelsStatus;
  name: string;
  onChange: (value: string | null) => void;
  value: string | null;
}) {
  const { locale, t } = useI18n();
  const labels = t.adminSystemSettings.fields;
  const defaultModel = activeModels.find((model) => model.is_default);
  const available =
    value === null || activeModels.some((model) => model.name === value);
  return (
    <FieldShell
      name={name}
      disabled={disabled}
      hint={!available ? labels.unavailableModel : undefined}
    >
      <select
        name={name}
        value={value ?? ""}
        disabled={disabled || modelsStatus !== "ready"}
        aria-invalid={!available || undefined}
        onChange={(event) => onChange(event.target.value || null)}
        className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
      >
        <option value="">
          {emptyOptionLabel ??
            formatSystemDefaultModelOption(
              labels.defaultModel,
              defaultModel?.display_name,
              locale,
            )}
        </option>
        {!available && value ? (
          <option value={value} disabled>
            {labels.unavailableModel}
          </option>
        ) : null}
        {activeModels.map((model) => (
          <option key={model.name} value={model.name}>
            {model.display_name}
          </option>
        ))}
      </select>
    </FieldShell>
  );
}

export function moveMemoryDocumentSection(
  value: MemoryDocumentSettingsValue,
  index: number,
  offset: -1 | 1,
): MemoryDocumentSettingsValue {
  const target = index + offset;
  if (
    index < 0 ||
    index >= value.sections.length ||
    target < 0 ||
    target >= value.sections.length
  ) {
    return value;
  }
  const sections = [...value.sections];
  [sections[index], sections[target]] = [sections[target]!, sections[index]!];
  return { sections };
}

export function MemoryDocumentSectionsEditor({
  onChange,
  value,
}: {
  onChange: (value: MemoryDocumentSettingsValue) => void;
  value: MemoryDocumentSettingsValue;
}) {
  const labels = useI18n().t.adminSystemSettings.fields;
  const minimumReached = value.sections.length <= 2;
  const maximumReached = value.sections.length >= 8;

  return (
    <div
      data-setting-key="memory_document.sections"
      data-settings-field-row="memory_document.sections"
      className="border-border/70 bg-background space-y-4 rounded-lg border p-4"
    >
      <div>
        <p className="text-sm font-medium">{labels.memoryDocumentSections}</p>
        <p className="text-muted-foreground mt-1 text-xs leading-5">
          {labels.memoryDocumentSectionsHint}
        </p>
      </div>

      <div className="space-y-2">
        {value.sections.map((section, index) => (
          <div
            key={index}
            className="grid gap-2 sm:grid-cols-[auto_minmax(0,1fr)_auto_auto] sm:items-center"
          >
            <span className="text-muted-foreground w-6 text-right text-xs tabular-nums">
              {index + 1}
            </span>
            <Input
              name={`memory_document.sections.${index}`}
              aria-label={labels.memoryDocumentSectionInput(index + 1)}
              value={section}
              onChange={(event) => {
                const sections = [...value.sections];
                sections[index] = event.currentTarget.value;
                onChange({ sections });
              }}
            />
            <span className="text-muted-foreground text-right text-xs tabular-nums">
              {Array.from(section.trim()).length}/80
            </span>
            <div className="flex items-center justify-end gap-1">
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                disabled={index === 0}
                aria-label={labels.moveMemoryDocumentSectionUp(index + 1)}
                onClick={() =>
                  onChange(moveMemoryDocumentSection(value, index, -1))
                }
              >
                <ArrowUpIcon aria-hidden />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                disabled={index === value.sections.length - 1}
                aria-label={labels.moveMemoryDocumentSectionDown(index + 1)}
                onClick={() =>
                  onChange(moveMemoryDocumentSection(value, index, 1))
                }
              >
                <ArrowDownIcon aria-hidden />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                disabled={minimumReached}
                aria-label={labels.removeMemoryDocumentSection(index + 1)}
                onClick={() =>
                  onChange({
                    sections: value.sections.filter(
                      (_item, current) => current !== index,
                    ),
                  })
                }
              >
                <XIcon aria-hidden />
              </Button>
            </div>
          </div>
        ))}
      </div>

      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={maximumReached}
        onClick={() => onChange({ sections: [...value.sections, ""] })}
      >
        {labels.addMemoryDocumentSection}
      </Button>
    </div>
  );
}

function StringListField({
  disabled = false,
  name,
  onChange,
  value,
}: {
  disabled?: boolean;
  name: string;
  onChange: (value: string[]) => void;
  value: string[];
}) {
  return (
    <FieldShell name={name} disabled={disabled}>
      <Input
        name={name}
        value={value.join(", ")}
        disabled={disabled}
        aria-describedby={`${name.replaceAll(".", "-")}-hint`}
        onChange={(event) => {
          const items = event.target.value
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean);
          onChange([...new Set(items)]);
        }}
      />
    </FieldShell>
  );
}

function RuntimeGroup({
  activeValue,
  children,
  description,
  value,
  title,
}: {
  activeValue: string;
  children: ReactNode;
  description: string;
  value: string;
  title: string;
}) {
  return (
    <section data-agent-settings-group={value} hidden={activeValue !== value}>
      <header className="border-border/70 border-b pb-4">
        <h3 className="text-sm font-semibold tracking-tight">{title}</h3>
        <p className="text-muted-foreground mt-1 max-w-3xl text-xs leading-5">
          {description}
        </p>
      </header>
      <div className="mt-4 grid gap-3">{children}</div>
    </section>
  );
}

function cloneWithPath<T>(value: T, path: string, next: unknown): T {
  const clone = structuredClone(value) as Record<string, unknown>;
  const parts = path.split(".");
  let current: Record<string, unknown> = clone;
  parts.forEach((part, index) => {
    if (index === parts.length - 1) {
      current[part] = next;
      return;
    }
    current = current[part] as Record<string, unknown>;
  });
  return clone as T;
}

function pathValue(value: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((current, part) => {
    if (typeof current !== "object" || current === null) return undefined;
    return (current as Record<string, unknown>)[part];
  }, value);
}

function ContextSizeRow({
  name,
  onChange,
  onRemove,
  value,
}: {
  name: string;
  onChange: (value: ContextSize) => void;
  onRemove?: () => void;
  value: ContextSize;
}) {
  const { locale, t } = useI18n();
  const labels = t.adminSystemSettings.fields;
  const isKeep = name.includes(".keep");
  const typeLabel = locale === "zh-CN" ? "计算方式" : "Measure by";
  const valueLabel = isKeep
    ? locale === "zh-CN"
      ? "保留数量"
      : "Number to keep"
    : locale === "zh-CN"
      ? "触发阈值"
      : "Trigger threshold";
  const unit =
    value.type === "tokens"
      ? "Token"
      : value.type === "messages"
        ? locale === "zh-CN"
          ? "条消息"
          : "messages"
        : "%";
  return (
    <div className="grid gap-3">
      <FieldShell name={`${name}.type`} label={typeLabel}>
        <select
          name={`${name}.type`}
          value={value.type}
          aria-label={typeLabel}
          onChange={(event) => {
            const type = event.target.value as ContextSize["type"];
            onChange({ type, value: type === "fraction" ? 0.8 : 1 });
          }}
          className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
        >
          <option value="tokens">
            {locale === "zh-CN" ? "Token 数" : "Token count"}
          </option>
          <option value="messages">
            {locale === "zh-CN" ? "消息数" : "Message count"}
          </option>
          <option value="fraction">
            {locale === "zh-CN" ? "上下文占比" : "Context percentage"}
          </option>
        </select>
      </FieldShell>
      <NumberField
        name={`${name}.value`}
        label={valueLabel}
        value={value.value}
        min={value.type === "fraction" ? 1 : 1}
        max={value.type === "fraction" ? 100 : 2_000_000}
        scale={value.type === "fraction" ? 0.01 : 1}
        step={1}
        unit={unit}
        onChange={(next) => onChange({ ...value, value: next } as ContextSize)}
      />
      {onRemove ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label={
            locale === "zh-CN" ? "删除此条件" : "Remove this condition"
          }
          title={labels.removeRow}
          className="justify-self-end"
          onClick={onRemove}
        >
          <XIcon aria-hidden />
          {locale === "zh-CN" ? "删除条件" : "Remove condition"}
        </Button>
      ) : null}
    </div>
  );
}

function ToolThresholdOverrides({
  name,
  onChange,
  value,
}: {
  name: string;
  onChange: (value: Record<string, number>) => void;
  value: Record<string, number>;
}) {
  const { locale, t } = useI18n();
  const labels = t.adminSystemSettings.fields;
  const entries = Object.entries(value);
  return (
    <div
      data-setting-key={name}
      data-settings-field-row={name}
      className="border-border/70 bg-background space-y-3 rounded-lg border p-4"
    >
      <div>
        <p className="text-sm font-medium">
          {locale === "zh-CN"
            ? "按工具单独设置外置阈值"
            : "Per-tool externalization thresholds"}
        </p>
        <p className="text-muted-foreground mt-1 text-xs leading-5">
          {locale === "zh-CN"
            ? "仅在某个工具需要不同长度边界时添加规则。"
            : "Add a rule only when a tool needs a different length boundary."}
        </p>
      </div>
      {entries.length ? (
        <div className="text-muted-foreground hidden grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-2 px-1 text-xs sm:grid">
          <span>{locale === "zh-CN" ? "工具名称" : "Tool"}</span>
          <span>
            {locale === "zh-CN" ? "外置阈值（字符）" : "Threshold (characters)"}
          </span>
          <span className="sr-only">{labels.removeRow}</span>
        </div>
      ) : null}
      {entries.map(([tool, threshold], index) => (
        <div
          key={`${tool}-${index}`}
          className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]"
        >
          <Input
            aria-label={locale === "zh-CN" ? "工具名称" : "Tool name"}
            value={tool}
            onChange={(event) => {
              const next = { ...value };
              delete next[tool];
              next[event.target.value] = threshold;
              onChange(next);
            }}
          />
          <Input
            type="number"
            aria-label={
              locale === "zh-CN"
                ? `${tool} 的外置阈值`
                : `Externalization threshold for ${tool}`
            }
            value={threshold}
            min={0}
            max={10_000_000}
            onChange={(event) => {
              const next = event.currentTarget.valueAsNumber;
              if (Number.isFinite(next)) onChange({ ...value, [tool]: next });
            }}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={
              locale === "zh-CN" ? `删除 ${tool} 规则` : `Remove ${tool} rule`
            }
            onClick={() => {
              const next = { ...value };
              delete next[tool];
              onChange(next);
            }}
          >
            <XIcon aria-hidden />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={entries.length >= 64}
        onClick={() => {
          let index = entries.length + 1;
          while (`tool_${index}` in value) index += 1;
          onChange({ ...value, [`tool_${index}`]: 0 });
        }}
      >
        {locale === "zh-CN" ? "添加工具规则" : "Add tool rule"}
      </Button>
    </div>
  );
}

function ToolFrequencyOverrides({
  name,
  onChange,
  value,
}: {
  name: string;
  onChange: (
    value: Record<string, { warn: number; hard_limit: number }>,
  ) => void;
  value: Record<string, { warn: number; hard_limit: number }>;
}) {
  const { locale, t } = useI18n();
  const labels = t.adminSystemSettings.fields;
  const entries = Object.entries(value);
  return (
    <div
      data-setting-key={name}
      data-settings-field-row={name}
      className="border-border/70 bg-background space-y-3 rounded-lg border p-4"
    >
      <div>
        <p className="text-sm font-medium">
          {locale === "zh-CN" ? "工具调用例外规则" : "Tool-frequency overrides"}
        </p>
        <p className="text-muted-foreground mt-1 text-xs leading-5">
          {locale === "zh-CN"
            ? "为确实需要重复调用的工具设置单独的提醒与终止次数。"
            : "Set separate warning and stop counts for tools that legitimately repeat."}
        </p>
      </div>
      {entries.length ? (
        <div className="text-muted-foreground hidden grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)_auto] gap-2 px-1 text-xs sm:grid">
          <span>{locale === "zh-CN" ? "工具" : "Tool"}</span>
          <span>{locale === "zh-CN" ? "提醒次数" : "Warn at"}</span>
          <span>{locale === "zh-CN" ? "终止次数" : "Stop at"}</span>
          <span className="sr-only">{labels.removeRow}</span>
        </div>
      ) : null}
      {entries.map(([tool, limits], index) => (
        <div
          key={`${tool}-${index}`}
          className="grid gap-2 sm:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)_auto]"
        >
          <Input
            aria-label={locale === "zh-CN" ? "工具名称" : "Tool name"}
            value={tool}
            onChange={(event) => {
              const next = { ...value };
              delete next[tool];
              next[event.target.value] = limits;
              onChange(next);
            }}
          />
          <Input
            type="number"
            aria-label={
              locale === "zh-CN"
                ? `${tool} 的提醒次数`
                : `Warning count for ${tool}`
            }
            value={limits.warn}
            min={1}
            max={100_000}
            onChange={(event) => {
              const next = event.currentTarget.valueAsNumber;
              if (Number.isFinite(next)) {
                onChange({ ...value, [tool]: { ...limits, warn: next } });
              }
            }}
          />
          <Input
            type="number"
            aria-label={
              locale === "zh-CN"
                ? `${tool} 的终止次数`
                : `Stop count for ${tool}`
            }
            value={limits.hard_limit}
            min={1}
            max={100_000}
            onChange={(event) => {
              const next = event.currentTarget.valueAsNumber;
              if (Number.isFinite(next)) {
                onChange({
                  ...value,
                  [tool]: { ...limits, hard_limit: next },
                });
              }
            }}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={
              locale === "zh-CN" ? `删除 ${tool} 规则` : `Remove ${tool} rule`
            }
            onClick={() => {
              const next = { ...value };
              delete next[tool];
              onChange(next);
            }}
          >
            <XIcon aria-hidden />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={entries.length >= 64}
        onClick={() => {
          let index = entries.length + 1;
          while (`tool_${index}` in value) index += 1;
          onChange({
            ...value,
            [`tool_${index}`]: { warn: 1, hard_limit: 1 },
          });
        }}
      >
        {locale === "zh-CN" ? "添加工具规则" : "Add tool rule"}
      </Button>
    </div>
  );
}

function agentRuntimeGroups(locale: Locale) {
  return [
    {
      value: "run-limits",
      title: locale === "zh-CN" ? "运行预算" : "Run budget",
      description:
        locale === "zh-CN"
          ? "限制单次任务的 Token、执行步数和子 Agent 数量，避免资源占用或执行时间失控。"
          : "Limit Tokens, steps, and subagents per Run to keep resource usage and execution time under control.",
      icon: GaugeIcon,
    },
    {
      value: "assistant-experience",
      title: locale === "zh-CN" ? "对话体验" : "Conversation experience",
      description:
        locale === "zh-CN"
          ? "配置自动标题、后续建议和输入润色。"
          : "Configure automatic titles, follow-up suggestions, and input polish.",
      icon: MessageSquareIcon,
    },
    {
      value: "summarization",
      title: locale === "zh-CN" ? "上下文与摘要" : "Context and summaries",
      description:
        locale === "zh-CN"
          ? "长对话达到阈值时自动压缩，并保留必要的最近历史。"
          : "Compress long conversations at a defined threshold while retaining necessary recent history.",
      icon: BotIcon,
    },
    {
      value: "memory",
      title: locale === "zh-CN" ? "记忆" : "Memory",
      description:
        locale === "zh-CN"
          ? "管理 Dream 模型、自动整理周期、注入上限、空闲封存与归档保留期。"
          : "Manage the Dream model, organization interval, injection limit, idle sealing, and episode retention.",
      icon: BrainIcon,
    },
    {
      value: "vision-bridge",
      title: locale === "zh-CN" ? "图片识别" : "Image inspection",
      description:
        locale === "zh-CN"
          ? "为不支持视觉输入的主模型提供受控的 inspect_image 工具。选择支持视觉输入的模型即启用，留空即关闭。"
          : "Give lead models without vision input a governed inspect_image tool. Selecting a model that supports vision input enables it; empty disables it.",
      icon: ImageIcon,
    },
    {
      value: "tools",
      title: locale === "zh-CN" ? "工具输出" : "Tool output",
      description:
        locale === "zh-CN"
          ? "控制工具发现，以及超长工具结果的外置、预览和例外规则。"
          : "Control tool discovery plus externalization, previews, and exceptions for long results.",
      icon: WrenchIcon,
    },
    {
      value: "safeguards",
      title: locale === "zh-CN" ? "安全防护" : "Safeguards",
      description:
        locale === "zh-CN"
          ? "检测重复执行和高频工具调用，并保护文件写入和安全拦截。"
          : "Detect repeated execution and high-frequency tool use while protecting file writes and safety stops.",
      icon: ShieldCheckIcon,
    },
  ] as const;
}

type AgentRuntimeGroupValue = ReturnType<
  typeof agentRuntimeGroups
>[number]["value"];
export type SettingsDestination =
  | "auth"
  | "automations"
  | "quotas"
  | AgentRuntimeGroupValue;

type PendingSystemSettingsLeave = {
  href: string;
  viaHistory: boolean;
};

type DirtyDestinationsBySection = Partial<
  Record<SystemSettingsSectionName, readonly SettingsDestination[]>
>;

const AGENT_RUNTIME_DESTINATION_FIELDS: Record<
  AgentRuntimeGroupValue,
  readonly (keyof AgentRuntimeSettingsValue)[]
> = {
  "run-limits": [
    "token_usage",
    "token_budget",
    "max_recursion_limit",
    "subagents",
  ],
  "assistant-experience": ["title", "suggestions", "input_polish"],
  summarization: ["summarization"],
  memory: ["memory"],
  "vision-bridge": ["vision_bridge"],
  tools: ["tool_search", "tool_output"],
  safeguards: ["loop_detection", "read_before_write", "safety_finish_reason"],
};

function sameDraftValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function dirtySystemSettingsDestinations(
  section: SystemSettingsSectionName,
  base: SystemSettingsSectionValueMap[SystemSettingsSectionName],
  draft: SystemSettingsSectionValueMap[SystemSettingsSectionName],
): SettingsDestination[] {
  if (sameDraftValue(base, draft)) return [];
  if (section === "memory_document") return ["memory"];
  if (section !== "agent_runtime") return [section];

  const runtimeBase = base as AgentRuntimeSettingsValue;
  const runtimeDraft = draft as AgentRuntimeSettingsValue;
  return Object.entries(AGENT_RUNTIME_DESTINATION_FIELDS).flatMap(
    ([destination, fields]) =>
      fields.some(
        (field) => !sameDraftValue(runtimeBase[field], runtimeDraft[field]),
      )
        ? [destination as AgentRuntimeGroupValue]
        : [],
  );
}

export function collectDirtySystemSettingsDestinations(
  sections: DirtyDestinationsBySection,
): ReadonlySet<SettingsDestination> {
  return new Set(Object.values(sections).flatMap((items) => items ?? []));
}

export function isSameDocumentNavigation(
  currentHref: string,
  destinationHref: string,
): boolean {
  const current = new URL(currentHref);
  const destination = new URL(destinationHref, current);
  return (
    destination.origin === current.origin &&
    destination.pathname === current.pathname &&
    destination.search === current.search
  );
}

function isAgentRuntimeGroup(
  destination: SettingsDestination,
): destination is AgentRuntimeGroupValue {
  return (
    destination !== "auth" &&
    destination !== "automations" &&
    destination !== "quotas"
  );
}

function AgentRuntimeEditor({
  activeGroup,
  activeModels,
  modelsStatus,
  onChange,
  value,
}: {
  activeGroup: AgentRuntimeGroupValue;
  activeModels: Model[];
  modelsStatus: ModelsStatus;
  onChange: (value: AgentRuntimeSettingsValue) => void;
  value: AgentRuntimeSettingsValue;
}) {
  const { locale } = useI18n();
  const update = (path: string, next: unknown) =>
    onChange(cloneWithPath(value, path, next));
  const bool = (path: string) => Boolean(pathValue(value, path));
  const number = (path: string) => Number(pathValue(value, path));
  const groups = agentRuntimeGroups(locale);
  const group = (value: (typeof groups)[number]["value"]) =>
    groups.find((item) => item.value === value)!;

  return (
    <div>
      <RuntimeGroup
        activeValue={activeGroup}
        value="run-limits"
        title={group("run-limits").title}
        description={group("run-limits").description}
      >
        {["token_usage.enabled", "token_budget.enabled"].map((path) => (
          <BooleanField
            key={path}
            name={`agent_runtime.${path}`}
            checked={bool(path)}
            onChange={(next) => update(path, next)}
          />
        ))}
        <NumberField
          name="agent_runtime.token_budget.max_tokens"
          value={number("token_budget.max_tokens")}
          min={1_000}
          max={2_000_000}
          disabled={!value.token_budget.enabled}
          dataDependency="token-budget"
          onChange={(next) => update("token_budget.max_tokens", next)}
        />
        <NullableNumberField
          name="agent_runtime.token_budget.max_input_tokens"
          value={value.token_budget.max_input_tokens}
          min={1}
          max={2_000_000}
          disabled={!value.token_budget.enabled}
          onChange={(next) => update("token_budget.max_input_tokens", next)}
        />
        <NullableNumberField
          name="agent_runtime.token_budget.max_output_tokens"
          value={value.token_budget.max_output_tokens}
          min={1}
          max={2_000_000}
          disabled={!value.token_budget.enabled}
          onChange={(next) => update("token_budget.max_output_tokens", next)}
        />
        {[
          "token_budget.warn_threshold",
          "token_budget.hard_stop_threshold",
        ].map((path) => (
          <NumberField
            key={path}
            name={`agent_runtime.${path}`}
            value={number(path)}
            min={0}
            max={100}
            step={1}
            scale={0.01}
            disabled={!value.token_budget.enabled}
            onChange={(next) => update(path, next)}
          />
        ))}
        <NumberField
          name="agent_runtime.max_recursion_limit"
          value={value.max_recursion_limit}
          min={1}
          max={100_000}
          onChange={(next) => update("max_recursion_limit", next)}
        />
        <NumberField
          name="agent_runtime.subagents.max_total_per_run"
          value={value.subagents.max_total_per_run}
          min={1}
          max={50}
          onChange={(next) => update("subagents.max_total_per_run", next)}
        />
      </RuntimeGroup>

      <RuntimeGroup
        activeValue={activeGroup}
        value="assistant-experience"
        title={group("assistant-experience").title}
        description={group("assistant-experience").description}
      >
        <BooleanField
          name="agent_runtime.title.enabled"
          checked={value.title.enabled}
          onChange={(next) => update("title.enabled", next)}
        />
        <NumberField
          name="agent_runtime.title.max_words"
          value={value.title.max_words}
          min={1}
          max={20}
          disabled={!value.title.enabled}
          onChange={(next) => update("title.max_words", next)}
        />
        <NumberField
          name="agent_runtime.title.max_chars"
          value={value.title.max_chars}
          min={10}
          max={200}
          disabled={!value.title.enabled}
          onChange={(next) => update("title.max_chars", next)}
        />
        <ModelField
          name="agent_runtime.title.model_name"
          value={value.title.model_name}
          activeModels={activeModels}
          modelsStatus={modelsStatus}
          disabled={!value.title.enabled}
          onChange={(next) => update("title.model_name", next)}
        />
        <BooleanField
          name="agent_runtime.suggestions.enabled"
          checked={value.suggestions.enabled}
          onChange={(next) => update("suggestions.enabled", next)}
        />
        <BooleanField
          name="agent_runtime.input_polish.enabled"
          checked={value.input_polish.enabled}
          onChange={(next) => update("input_polish.enabled", next)}
        />
        <NumberField
          name="agent_runtime.input_polish.max_chars"
          value={value.input_polish.max_chars}
          min={1}
          max={100_000}
          disabled={!value.input_polish.enabled}
          onChange={(next) => update("input_polish.max_chars", next)}
        />
        <ModelField
          name="agent_runtime.input_polish.model_name"
          value={value.input_polish.model_name}
          activeModels={activeModels}
          modelsStatus={modelsStatus}
          disabled={!value.input_polish.enabled}
          onChange={(next) => update("input_polish.model_name", next)}
        />
      </RuntimeGroup>

      <RuntimeGroup
        activeValue={activeGroup}
        value="summarization"
        title={group("summarization").title}
        description={group("summarization").description}
      >
        <BooleanField
          name="agent_runtime.summarization.enabled"
          checked={value.summarization.enabled}
          onChange={(next) => update("summarization.enabled", next)}
        />
        <fieldset
          disabled={!value.summarization.enabled}
          className="contents disabled:opacity-60"
        >
          <legend className="sr-only">
            {locale === "zh-CN"
              ? "上下文摘要详细设置"
              : "Summarization details"}
          </legend>
          <ModelField
            name="agent_runtime.summarization.model_name"
            value={value.summarization.model_name}
            activeModels={activeModels}
            modelsStatus={modelsStatus}
            onChange={(next) => update("summarization.model_name", next)}
          />
          <NullableNumberField
            name="agent_runtime.summarization.trim_tokens_to_summarize"
            value={value.summarization.trim_tokens_to_summarize}
            min={1}
            max={2_000_000}
            onChange={(next) =>
              update("summarization.trim_tokens_to_summarize", next)
            }
          />
          <StringListField
            name="agent_runtime.summarization.skill_file_read_tool_names"
            value={value.summarization.skill_file_read_tool_names}
            onChange={(next) =>
              update("summarization.skill_file_read_tool_names", next)
            }
          />
          <div className="space-y-3">
            <p className="text-sm font-medium">
              {locale === "zh-CN" ? "摘要触发条件" : "Summary triggers"}
            </p>
            {(value.summarization.trigger ?? []).map((trigger, index) => (
              <ContextSizeRow
                key={index}
                name={`agent_runtime.summarization.trigger.${index}`}
                value={trigger}
                onChange={(next) => {
                  const triggers = [...(value.summarization.trigger ?? [])];
                  triggers[index] = next;
                  update("summarization.trigger", triggers);
                }}
                onRemove={() => {
                  const triggers = (value.summarization.trigger ?? []).filter(
                    (_item, current) => current !== index,
                  );
                  update(
                    "summarization.trigger",
                    triggers.length ? triggers : null,
                  );
                }}
              />
            ))}
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={(value.summarization.trigger?.length ?? 0) >= 8}
              onClick={() =>
                update("summarization.trigger", [
                  ...(value.summarization.trigger ?? []),
                  { type: "tokens", value: 1 },
                ])
              }
            >
              {locale === "zh-CN" ? "添加触发条件" : "Add trigger"}
            </Button>
          </div>
          <div>
            <p className="mb-2 text-sm font-medium">
              {locale === "zh-CN"
                ? "摘要后保留的最近历史"
                : "Recent history retained after summarizing"}
            </p>
            <ContextSizeRow
              name="agent_runtime.summarization.keep"
              value={value.summarization.keep}
              onChange={(next) => update("summarization.keep", next)}
            />
          </div>
        </fieldset>
      </RuntimeGroup>

      <RuntimeGroup
        activeValue={activeGroup}
        value="memory"
        title={group("memory").title}
        description={group("memory").description}
      >
        <BooleanField
          name="agent_runtime.memory.enabled"
          checked={value.memory.enabled}
          onChange={(next) => update("memory.enabled", next)}
        />
        <fieldset disabled={!value.memory.enabled} className="contents">
          <legend className="sr-only">
            {locale === "zh-CN" ? "记忆策略详细设置" : "Memory policy details"}
          </legend>
          <ModelField
            name="agent_runtime.memory.model_name"
            value={value.memory.model_name}
            activeModels={activeModels}
            modelsStatus={modelsStatus}
            onChange={(next) => update("memory.model_name", next)}
          />
          <NumberField
            name="agent_runtime.memory.dream_interval_minutes"
            value={value.memory.dream_interval_minutes}
            min={15}
            max={1_440}
            onChange={(next) => update("memory.dream_interval_minutes", next)}
          />
          <NumberField
            name="agent_runtime.memory.max_injection_tokens"
            value={value.memory.max_injection_tokens}
            min={100}
            max={8_000}
            onChange={(next) => update("memory.max_injection_tokens", next)}
          />
          <NumberField
            name="agent_runtime.memory.idle_seal_minutes"
            value={value.memory.idle_seal_minutes}
            min={0}
            max={10_080}
            onChange={(next) => update("memory.idle_seal_minutes", next)}
          />
          <NumberField
            name="agent_runtime.memory.episode_retention_days"
            value={value.memory.episode_retention_days}
            min={0}
            max={3_650}
            onChange={(next) => update("memory.episode_retention_days", next)}
          />
        </fieldset>
      </RuntimeGroup>

      <RuntimeGroup
        activeValue={activeGroup}
        value="tools"
        title={group("tools").title}
        description={group("tools").description}
      >
        <BooleanField
          name="agent_runtime.tool_search.enabled"
          checked={value.tool_search.enabled}
          onChange={(next) => update("tool_search.enabled", next)}
        />
        <NumberField
          name="agent_runtime.tool_search.auto_promote_top_k"
          value={value.tool_search.auto_promote_top_k}
          min={1}
          max={5}
          disabled={!value.tool_search.enabled}
          onChange={(next) => update("tool_search.auto_promote_top_k", next)}
        />
        <BooleanField
          name="agent_runtime.tool_output.enabled"
          checked={value.tool_output.enabled}
          onChange={(next) => update("tool_output.enabled", next)}
        />
        <fieldset disabled={!value.tool_output.enabled} className="contents">
          <legend className="sr-only">
            {locale === "zh-CN" ? "工具结果详细设置" : "Tool output details"}
          </legend>
          {[
            "externalize_min_chars",
            "preview_head_chars",
            "preview_tail_chars",
            "fallback_max_chars",
            "fallback_head_chars",
            "fallback_tail_chars",
          ].map((field) => (
            <NumberField
              key={field}
              name={`agent_runtime.tool_output.${field}`}
              value={number(`tool_output.${field}`)}
              min={0}
              max={10_000_000}
              onChange={(next) => update(`tool_output.${field}`, next)}
            />
          ))}
          <StringListField
            name="agent_runtime.tool_output.exempt_tools"
            value={value.tool_output.exempt_tools}
            onChange={(next) => update("tool_output.exempt_tools", next)}
          />
          <ToolThresholdOverrides
            name="agent_runtime.tool_output.tool_overrides"
            value={value.tool_output.tool_overrides}
            onChange={(next) => update("tool_output.tool_overrides", next)}
          />
        </fieldset>
      </RuntimeGroup>

      <RuntimeGroup
        activeValue={activeGroup}
        value="vision-bridge"
        title={group("vision-bridge").title}
        description={group("vision-bridge").description}
      >
        <ModelField
          name="agent_runtime.vision_bridge.model_name"
          value={value.vision_bridge.model_name}
          activeModels={selectVisionInputModels(activeModels)}
          modelsStatus={modelsStatus}
          emptyOptionLabel={locale === "zh-CN" ? "关闭" : "Off"}
          onChange={(next) => update("vision_bridge.model_name", next)}
        />
        <NumberField
          name="agent_runtime.vision_bridge.timeout_seconds"
          value={value.vision_bridge.timeout_seconds}
          min={5}
          max={120}
          disabled={value.vision_bridge.model_name === null}
          onChange={(next) => update("vision_bridge.timeout_seconds", next)}
        />
        <FieldShell name="agent_runtime.vision_bridge.contract_version">
          <Input
            name="agent_runtime.vision_bridge.contract_version"
            value={value.vision_bridge.contract_version}
            readOnly
          />
        </FieldShell>
      </RuntimeGroup>

      <RuntimeGroup
        activeValue={activeGroup}
        value="safeguards"
        title={group("safeguards").title}
        description={group("safeguards").description}
      >
        <BooleanField
          name="agent_runtime.loop_detection.enabled"
          checked={value.loop_detection.enabled}
          onChange={(next) => update("loop_detection.enabled", next)}
        />
        <fieldset disabled={!value.loop_detection.enabled} className="contents">
          <legend className="sr-only">
            {locale === "zh-CN" ? "循环检测详细设置" : "Loop detection details"}
          </legend>
          {[
            ["loop_detection.warn_threshold", 1, 100_000],
            ["loop_detection.hard_limit", 1, 100_000],
            ["loop_detection.window_size", 1, 100_000],
            ["loop_detection.max_tracked_threads", 1, 100_000],
            ["loop_detection.tool_freq_warn", 1, 100_000],
            ["loop_detection.tool_freq_hard_limit", 1, 100_000],
          ].map(([path, min, max]) => (
            <NumberField
              key={String(path)}
              name={`agent_runtime.${path}`}
              value={number(String(path))}
              min={Number(min)}
              max={Number(max)}
              onChange={(next) => update(String(path), next)}
            />
          ))}
          <ToolFrequencyOverrides
            name="agent_runtime.loop_detection.tool_freq_overrides"
            value={value.loop_detection.tool_freq_overrides}
            onChange={(next) =>
              update("loop_detection.tool_freq_overrides", next)
            }
          />
        </fieldset>
        <BooleanField
          name="agent_runtime.read_before_write.enabled"
          checked={value.read_before_write.enabled}
          onChange={(next) => update("read_before_write.enabled", next)}
        />
        <BooleanField
          name="agent_runtime.safety_finish_reason.enabled"
          checked={value.safety_finish_reason.enabled}
          onChange={(next) => update("safety_finish_reason.enabled", next)}
        />
      </RuntimeGroup>
    </div>
  );
}

function AuthEditor({
  onChange,
  value,
}: {
  onChange: (value: AuthSettingsValue) => void;
  value: AuthSettingsValue;
}) {
  return (
    <BooleanField
      name="auth.allow_registration"
      checked={value.allow_registration}
      onChange={(allow_registration) => onChange({ allow_registration })}
    />
  );
}

function AutomationsEditor({
  onChange,
  value,
}: {
  onChange: (value: AutomationsSettingsValue) => void;
  value: AutomationsSettingsValue;
}) {
  return (
    <div className="grid gap-3">
      <BooleanField
        name="automations.enabled"
        checked={value.enabled}
        onChange={(enabled) => onChange({ ...value, enabled })}
      />
      <NumberField
        name="automations.poll_interval_seconds"
        value={value.poll_interval_seconds}
        min={1}
        max={300}
        onChange={(poll_interval_seconds) =>
          onChange({ ...value, poll_interval_seconds })
        }
      />
      <NumberField
        name="automations.max_concurrent_runs"
        value={value.max_concurrent_runs}
        min={1}
        max={32}
        onChange={(max_concurrent_runs) =>
          onChange({ ...value, max_concurrent_runs })
        }
      />
      <NumberField
        name="automations.min_once_delay_seconds"
        value={value.min_once_delay_seconds}
        min={0}
        max={86_400}
        onChange={(min_once_delay_seconds) =>
          onChange({ ...value, min_once_delay_seconds })
        }
      />
    </div>
  );
}

function QuotasEditor({
  onChange,
  value,
}: {
  onChange: (value: QuotaSettingsValue) => void;
  value: QuotaSettingsValue;
}) {
  return (
    <div className="grid gap-3">
      <NumberField
        name="quotas.default_member_limit"
        value={value.default_member_limit}
        min={1}
        max={Number.MAX_SAFE_INTEGER}
        onChange={(next) => onChange({ ...value, default_member_limit: next })}
      />
      <NumberField
        name="quotas.default_storage_bytes_limit"
        value={value.default_storage_bytes_limit}
        min={0}
        max={1_000_000}
        scale={GIB}
        onChange={(next) =>
          onChange({ ...value, default_storage_bytes_limit: next })
        }
      />
      <NumberField
        name="quotas.default_concurrent_run_limit"
        value={value.default_concurrent_run_limit}
        min={1}
        max={Number.MAX_SAFE_INTEGER}
        onChange={(next) =>
          onChange({ ...value, default_concurrent_run_limit: next })
        }
      />
      <NumberField
        name="quotas.default_mcp_calls_daily_limit"
        value={value.default_mcp_calls_daily_limit}
        min={0}
        max={Number.MAX_SAFE_INTEGER}
        onChange={(next) =>
          onChange({ ...value, default_mcp_calls_daily_limit: next })
        }
      />
      <NumberField
        name="quotas.warning_threshold"
        value={value.warning_threshold}
        min={1}
        max={99}
        scale={0.01}
        onChange={(next) => onChange({ ...value, warning_threshold: next })}
      />
    </div>
  );
}

function SectionFeedback({
  error,
  result,
}: {
  error?: string;
  result?: SystemSettingsMutationResponse;
}) {
  const labels = useI18n().t.adminSystemSettings;
  return (
    <div className="space-y-2">
      {error ? (
        <p
          role="alert"
          className="border-destructive/30 bg-destructive/5 text-destructive rounded-lg border px-3 py-2 text-sm"
        >
          {error}
        </p>
      ) : null}
      {result ? (
        <div
          role="status"
          className="border-success/30 bg-success/10 space-y-1 rounded-lg border px-3 py-2 text-sm"
        >
          <p className="font-medium">
            <CheckCircle2Icon
              aria-hidden
              className="text-success mr-1.5 inline size-4"
            />
            {labels.feedback.saved}
          </p>
          <div className="text-muted-foreground flex flex-wrap gap-x-4 gap-y-1 text-xs">
            <span>{labels.common.storedRevision(result.stored_revision)}</span>
            <span>
              {labels.common.effectiveRevision(result.effective_revision)}
            </span>
            <span>
              {result.pending_roles.length
                ? labels.common.pendingRoles(result.pending_roles.join(", "))
                : labels.common.noPendingRoles}
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function isSystemSettingsSaveDisabled({
  dirty,
  modelsStatus,
  pending,
  schemaValid,
  section,
}: {
  dirty: boolean;
  modelsStatus: ModelsStatus;
  pending: boolean;
  schemaValid: boolean;
  section: SystemSettingsSectionName;
}): boolean {
  return (
    !dirty ||
    pending ||
    (section === "memory_document" && !schemaValid) ||
    (section === "agent_runtime" && modelsStatus !== "ready")
  );
}

function EditableSection<Value>({
  activeModels,
  description,
  effectScope,
  effectiveRevision,
  error,
  lastResult,
  modelsStatus,
  onDraftChange,
  onSave,
  pending,
  revision,
  schema,
  section,
  title,
  updatedAt,
  validate,
  value,
  renderEditor,
}: {
  activeModels: Model[];
  description: string;
  effectScope: SystemSettingsEffectScope;
  effectiveRevision: number;
  error?: string;
  lastResult?: SystemSettingsMutationResponse;
  modelsStatus: ModelsStatus;
  onDraftChange: (
    section: SystemSettingsSectionName,
    base: SystemSettingsSectionValueMap[SystemSettingsSectionName],
    draft: SystemSettingsSectionValueMap[SystemSettingsSectionName],
  ) => void;
  onSave: SaveSection;
  pending: boolean;
  revision: number;
  schema: z.ZodType<Value>;
  section: SystemSettingsSectionName;
  title: string;
  updatedAt: string;
  validate?: (value: Value) => Value;
  value: Value;
  renderEditor: (
    draft: Value,
    setDraft: (value: Value) => void,
    activeModels: Model[],
    modelsStatus: ModelsStatus,
  ) => ReactNode;
}) {
  const { locale, t } = useI18n();
  const labels = t.adminSystemSettings;
  const [base, setBase] = useState(value);
  const [draft, setDraft] = useState(value);
  const [baseRevision, setBaseRevision] = useState(revision);
  const [localError, setLocalError] = useState<string | null>(null);
  const dirty = JSON.stringify(base) !== JSON.stringify(draft);
  const schemaValid =
    section !== "memory_document" || schema.safeParse(draft).success;

  function updateDraft(next: Value): void {
    setDraft(next);
    onDraftChange(
      section,
      base as SystemSettingsSectionValueMap[SystemSettingsSectionName],
      next as SystemSettingsSectionValueMap[SystemSettingsSectionName],
    );
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!dirty || pending) return;
    setLocalError(null);
    let parsed: Value;
    try {
      parsed = schema.parse(draft);
      parsed = validate ? validate(parsed) : parsed;
    } catch (validationError) {
      const message =
        validationError instanceof Error &&
        validationError.message.includes("current active model")
          ? labels.feedback.inactiveModel
          : labels.feedback.invalid;
      setLocalError(message);
      return;
    }
    if (
      section === "auth" &&
      typeof window !== "undefined" &&
      !window.confirm(labels.feedback.registrationConfirmation)
    ) {
      return;
    }
    const result = await onSave(
      section,
      parsed as SystemSettingsSectionValueMap[SystemSettingsSectionName],
      baseRevision,
    );
    if (result?.section !== section) return;
    const next = result.policy.value as Value;
    setBase(next);
    setDraft(next);
    setBaseRevision(result.stored_revision);
    onDraftChange(
      section,
      next as SystemSettingsSectionValueMap[SystemSettingsSectionName],
      next as SystemSettingsSectionValueMap[SystemSettingsSectionName],
    );
  }

  return (
    <AdminSection
      className="overflow-visible rounded-none border-0"
      aria-labelledby={`system-settings-${section}`}
      title={
        <span
          id={`system-settings-${section}`}
          className="flex items-center gap-2 text-base"
        >
          {title}
          <SystemSettingsEffectBadge scope={effectScope} />
        </span>
      }
      description={description}
      actions={
        <details className="group text-xs">
          <summary className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex min-h-9 cursor-pointer list-none items-center gap-1.5 rounded-md px-2 focus-visible:ring-2 focus-visible:outline-none">
            <InfoIcon aria-hidden className="size-3.5" />
            {locale === "zh-CN" ? "配置状态" : "Configuration status"}
            <ChevronDownIcon
              aria-hidden
              className="size-3.5 transition-transform group-open:rotate-180"
            />
          </summary>
          <div className="border-border/70 bg-background text-muted-foreground mt-2 grid gap-1 rounded-lg border p-3 tabular-nums">
            <span>{labels.common.revision(baseRevision)}</span>
            <span>{labels.common.effectiveRevision(effectiveRevision)}</span>
            <span>
              {labels.common.updatedAt(formatUpdatedAt(updatedAt, locale))}
            </span>
          </div>
        </details>
      }
      contentClassName="p-0"
    >
      <form onSubmit={(event) => void submit(event)}>
        <fieldset
          disabled={pending}
          aria-busy={pending}
          className="min-w-0 p-4 sm:p-5"
        >
          {renderEditor(draft, updateDraft, activeModels, modelsStatus)}
        </fieldset>
        <div
          data-settings-save-footer={section}
          className="border-border/70 bg-card/95 sticky bottom-0 z-10 space-y-3 border-t px-4 py-3 backdrop-blur sm:flex sm:items-center sm:justify-between sm:gap-4 sm:space-y-0 sm:px-5"
        >
          <div className="min-w-0 flex-1 space-y-2">
            <p className="text-sm font-medium" aria-live="polite">
              {dirty
                ? locale === "zh-CN"
                  ? "有未保存修改"
                  : "Unsaved changes"
                : locale === "zh-CN"
                  ? "未修改"
                  : "No changes"}
            </p>
            <SectionFeedback
              error={
                localError ??
                error ??
                (dirty && !schemaValid ? labels.feedback.invalid : undefined)
              }
              result={dirty ? undefined : lastResult}
            />
          </div>
          <div className="grid shrink-0 grid-cols-2 gap-2 sm:flex sm:justify-end">
            <Button
              type="button"
              variant="outline"
              className="min-h-11 sm:min-h-9"
              disabled={!dirty || pending}
              onClick={() => {
                updateDraft(base);
                setLocalError(null);
              }}
            >
              <Undo2Icon aria-hidden />
              {labels.common.reset}
            </Button>
            <Button
              type="submit"
              className="min-h-11 sm:min-h-9"
              disabled={isSystemSettingsSaveDisabled({
                dirty,
                modelsStatus,
                pending,
                schemaValid,
                section,
              })}
            >
              {pending ? (
                <RefreshCwIcon aria-hidden className="animate-spin" />
              ) : (
                <SaveIcon aria-hidden />
              )}
              {pending ? labels.common.saving : labels.common.save}
            </Button>
          </div>
        </div>
      </form>
    </AdminSection>
  );
}

export function AdminSystemSettingsStateView({
  activeModels,
  lastResults,
  modelsStatus,
  onNavigate,
  onRetry,
  onSave,
  pendingSection,
  retrying,
  sectionErrors,
  state,
}: {
  activeModels: Model[];
  lastResults: LastResults;
  modelsStatus: ModelsStatus;
  onNavigate?: (href: string) => void;
  onRetry: () => void;
  onSave: SaveSection;
  pendingSection: SystemSettingsSectionName | null;
  retrying: boolean;
  sectionErrors: SectionErrors;
  state: AdminSystemSettingsCatalogState;
}) {
  const { locale, t } = useI18n();
  const labels = t.adminSystemSettings;
  const [activeDestination, setActiveDestination] =
    useState<SettingsDestination>("auth");
  const [dirtyBySection, setDirtyBySection] =
    useState<DirtyDestinationsBySection>({});
  const [pendingLeave, setPendingLeave] =
    useState<PendingSystemSettingsLeave | null>(null);
  const allowLeaveRef = useRef(false);
  const dirtyDestinations = useMemo(
    () => collectDirtySystemSettingsDestinations(dirtyBySection),
    [dirtyBySection],
  );
  const hasUnsavedChanges = dirtyDestinations.size > 0;
  const unsavedLabel = locale === "zh-CN" ? "未保存" : "Unsaved";
  const platformDestinations = [
    {
      value: "auth",
      label: locale === "zh-CN" ? "注册与访问" : "Registration and access",
      icon: UserPlusIcon,
    },
    {
      value: "automations",
      label: locale === "zh-CN" ? "自动化调度" : "Automation scheduling",
      icon: CalendarClockIcon,
    },
    {
      value: "quotas",
      label: locale === "zh-CN" ? "项目默认配额" : "Project quota defaults",
      icon: UsersIcon,
    },
  ] as const;
  const agentDestinations = agentRuntimeGroups(locale);
  const activeSection: SystemSettingsSectionName = isAgentRuntimeGroup(
    activeDestination,
  )
    ? "agent_runtime"
    : activeDestination;
  const activeAgentGroup: AgentRuntimeGroupValue = isAgentRuntimeGroup(
    activeDestination,
  )
    ? activeDestination
    : "run-limits";

  function recordDraft(
    section: SystemSettingsSectionName,
    base: SystemSettingsSectionValueMap[SystemSettingsSectionName],
    draft: SystemSettingsSectionValueMap[SystemSettingsSectionName],
  ): void {
    const next = dirtySystemSettingsDestinations(section, base, draft);
    setDirtyBySection((current) => {
      if (sameDraftValue(current[section] ?? [], next)) return current;
      return { ...current, [section]: next };
    });
  }

  useEffect(() => {
    if (!hasUnsavedChanges) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      if (allowLeaveRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [hasUnsavedChanges]);

  useEffect(() => {
    if (!hasUnsavedChanges) return;

    const preventDirtyNavigation = (event: MouseEvent) => {
      if (
        allowLeaveRef.current ||
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest<HTMLAnchorElement>("a[href]");
      if (
        !anchor ||
        anchor.target === "_blank" ||
        anchor.hasAttribute("download")
      ) {
        return;
      }
      const destination = new URL(anchor.href, window.location.href);
      if (isSameDocumentNavigation(window.location.href, destination.href)) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      setPendingLeave({ href: destination.href, viaHistory: false });
    };

    const currentHref = window.location.href;
    const currentHistoryState: unknown = window.history.state;
    const preventHistoryNavigation = (event: PopStateEvent) => {
      if (allowLeaveRef.current) return;
      const destination = window.location.href;
      if (isSameDocumentNavigation(currentHref, destination)) return;
      event.stopImmediatePropagation();
      window.history.pushState(currentHistoryState, "", currentHref);
      setPendingLeave({ href: destination, viaHistory: true });
    };

    document.addEventListener("click", preventDirtyNavigation, true);
    window.addEventListener("popstate", preventHistoryNavigation, true);
    return () => {
      document.removeEventListener("click", preventDirtyNavigation, true);
      window.removeEventListener("popstate", preventHistoryNavigation, true);
    };
  }, [hasUnsavedChanges]);

  return (
    <AdminPage className="max-w-7xl space-y-5">
      <AdminPageHeader
        eyebrow={labels.header.eyebrow}
        title={labels.header.title}
        actions={
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={retrying}
            aria-busy={retrying}
            onClick={() => {
              if (!retrying) onRetry();
            }}
          >
            <RefreshCwIcon
              aria-hidden
              className={retrying ? "animate-spin" : undefined}
            />
            {retrying ? labels.header.refreshing : labels.header.refresh}
          </Button>
        }
      />

      {state.status === "loading" ? (
        <section aria-label={labels.states.loading} className="grid gap-4">
          <span className="sr-only">{labels.states.loading}</span>
          <Skeleton className="h-48 rounded-xl" />
          <Skeleton className="h-56 rounded-xl" />
          <Skeleton className="h-96 rounded-xl" />
        </section>
      ) : state.status === "error" ? (
        <Card className="border-dashed shadow-none">
          <CardHeader>
            <span className="bg-muted flex size-10 items-center justify-center rounded-lg">
              <DatabaseIcon
                aria-hidden
                className="text-muted-foreground size-4"
              />
            </span>
            <CardTitle>{labels.states.unavailableTitle}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-muted-foreground text-sm">
              {labels.states.unavailableDescription}
            </p>
            <Button
              type="button"
              variant="outline"
              disabled={retrying}
              onClick={() => {
                if (!retrying) onRetry();
              }}
            >
              <RefreshCwIcon
                aria-hidden
                className={retrying ? "animate-spin" : undefined}
              />
              {labels.states.retry}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div
          data-settings-layout="workbench"
          className="border-border/70 bg-card overflow-hidden rounded-xl border lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]"
        >
          <aside className="bg-muted/20 border-border/70 border-b lg:border-r lg:border-b-0">
            <div className="p-4 lg:hidden">
              <label
                htmlFor="system-settings-destination"
                className="mb-2 block text-sm font-medium"
              >
                {locale === "zh-CN"
                  ? "选择要管理的配置"
                  : "Choose what to configure"}
              </label>
              <select
                id="system-settings-destination"
                value={activeDestination}
                onChange={(event) =>
                  setActiveDestination(
                    event.currentTarget.value as SettingsDestination,
                  )
                }
                className="border-input bg-background h-11 w-full rounded-md border px-3 text-sm"
              >
                <optgroup
                  label={locale === "zh-CN" ? "平台策略" : "Platform policies"}
                >
                  {platformDestinations.map((destination) => (
                    <option key={destination.value} value={destination.value}>
                      {destination.label}
                      {dirtyDestinations.has(destination.value)
                        ? ` · ${unsavedLabel}`
                        : ""}
                    </option>
                  ))}
                </optgroup>
                <optgroup
                  label={locale === "zh-CN" ? "Agent 行为" : "Agent behavior"}
                >
                  {agentDestinations.map((destination) => (
                    <option key={destination.value} value={destination.value}>
                      {destination.title}
                      {dirtyDestinations.has(destination.value)
                        ? ` · ${unsavedLabel}`
                        : ""}
                    </option>
                  ))}
                </optgroup>
              </select>
            </div>

            <nav
              data-settings-navigation="primary"
              aria-label={
                locale === "zh-CN"
                  ? "系统配置导航"
                  : "System settings navigation"
              }
              className="hidden p-3 lg:block"
            >
              <p className="text-muted-foreground mb-2 px-2 text-xs font-medium">
                {locale === "zh-CN" ? "平台策略" : "Platform policies"}
              </p>
              <div className="space-y-1">
                {platformDestinations.map((destination) => {
                  const Icon = destination.icon;
                  const selected = activeDestination === destination.value;
                  return (
                    <button
                      key={destination.value}
                      type="button"
                      data-settings-task={destination.value}
                      data-settings-destination={destination.value}
                      data-settings-dirty={
                        dirtyDestinations.has(destination.value) || undefined
                      }
                      aria-current={selected ? "page" : undefined}
                      className={cn(
                        "text-muted-foreground flex min-h-10 w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm transition-colors hover:bg-blue-50 dark:hover:bg-blue-500/15",
                        selected &&
                          "bg-blue-50 text-blue-600 dark:bg-blue-500/15 dark:text-blue-300",
                      )}
                      onClick={() => setActiveDestination(destination.value)}
                    >
                      <Icon aria-hidden className="size-4 shrink-0" />
                      <span className="font-medium">{destination.label}</span>
                      {dirtyDestinations.has(destination.value) ? (
                        <Badge
                          variant="outline"
                          data-settings-dirty-marker={destination.value}
                          className="border-warning/40 bg-warning/10 text-warning-foreground ml-auto px-1.5 py-0 text-[10px]"
                        >
                          {unsavedLabel}
                        </Badge>
                      ) : null}
                    </button>
                  );
                })}
              </div>

              <div
                data-settings-task="agent_runtime"
                className="border-border/70 mt-4 border-t pt-4"
              >
                <p className="text-muted-foreground mb-2 flex items-center gap-2 px-2 text-xs font-medium">
                  <BotIcon aria-hidden className="size-3.5" />
                  {locale === "zh-CN" ? "Agent 行为" : "Agent behavior"}
                </p>
                <div className="space-y-1">
                  {agentDestinations.map((destination) => {
                    const Icon = destination.icon;
                    const selected = activeDestination === destination.value;
                    return (
                      <button
                        key={destination.value}
                        type="button"
                        data-settings-destination={destination.value}
                        data-settings-dirty={
                          dirtyDestinations.has(destination.value) || undefined
                        }
                        aria-current={selected ? "page" : undefined}
                        className={cn(
                          "text-muted-foreground flex min-h-10 w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm transition-colors hover:bg-blue-50 dark:hover:bg-blue-500/15",
                          selected &&
                            "bg-blue-50 text-blue-600 dark:bg-blue-500/15 dark:text-blue-300",
                        )}
                        onClick={() => setActiveDestination(destination.value)}
                      >
                        <Icon aria-hidden className="size-4 shrink-0" />
                        <span className="font-medium">{destination.title}</span>
                        {dirtyDestinations.has(destination.value) ? (
                          <Badge
                            variant="outline"
                            data-settings-dirty-marker={destination.value}
                            className="border-warning/40 bg-warning/10 text-warning-foreground ml-auto px-1.5 py-0 text-[10px]"
                          >
                            {unsavedLabel}
                          </Badge>
                        ) : null}
                      </button>
                    );
                  })}
                </div>
              </div>
            </nav>
          </aside>

          <div className="min-w-0">
            <div hidden={activeSection !== "auth"}>
              <EditableSection
                key={`auth-${state.data.sections.auth.revision}`}
                section="auth"
                title={labels.sections.auth.title}
                description={labels.sections.auth.description}
                value={state.data.sections.auth.value}
                revision={state.data.sections.auth.revision}
                effectiveRevision={state.data.sections.auth.effective_revision}
                effectScope={state.data.sections.auth.effect_scope}
                updatedAt={state.data.sections.auth.updated_at}
                schema={authSettingsValueSchema}
                pending={pendingSection === "auth"}
                error={sectionErrors.auth}
                lastResult={lastResults.auth}
                activeModels={activeModels}
                modelsStatus={modelsStatus}
                onDraftChange={recordDraft}
                onSave={onSave}
                renderEditor={(value, setValue) => (
                  <AuthEditor value={value} onChange={setValue} />
                )}
              />
            </div>
            <div hidden={activeSection !== "automations"}>
              <EditableSection
                key={`automations-${state.data.sections.automations.revision}`}
                section="automations"
                title={labels.sections.automations.title}
                description={labels.sections.automations.description}
                value={state.data.sections.automations.value}
                revision={state.data.sections.automations.revision}
                effectiveRevision={
                  state.data.sections.automations.effective_revision
                }
                effectScope={state.data.sections.automations.effect_scope}
                updatedAt={state.data.sections.automations.updated_at}
                schema={automationsSettingsValueSchema}
                pending={pendingSection === "automations"}
                error={sectionErrors.automations}
                lastResult={lastResults.automations}
                activeModels={activeModels}
                modelsStatus={modelsStatus}
                onDraftChange={recordDraft}
                onSave={onSave}
                renderEditor={(value, setValue) => (
                  <AutomationsEditor value={value} onChange={setValue} />
                )}
              />
            </div>
            <div hidden={activeSection !== "quotas"}>
              <EditableSection
                key={`quotas-${state.data.sections.quotas.revision}`}
                section="quotas"
                title={labels.sections.quotas.title}
                description={labels.sections.quotas.description}
                value={state.data.sections.quotas.value}
                revision={state.data.sections.quotas.revision}
                effectiveRevision={
                  state.data.sections.quotas.effective_revision
                }
                effectScope={state.data.sections.quotas.effect_scope}
                updatedAt={state.data.sections.quotas.updated_at}
                schema={quotaSettingsValueSchema}
                pending={pendingSection === "quotas"}
                error={sectionErrors.quotas}
                lastResult={lastResults.quotas}
                activeModels={activeModels}
                modelsStatus={modelsStatus}
                onDraftChange={recordDraft}
                onSave={onSave}
                renderEditor={(value, setValue) => (
                  <QuotasEditor value={value} onChange={setValue} />
                )}
              />
            </div>
            <div hidden={activeSection !== "agent_runtime"}>
              <EditableSection
                key={`agent-runtime-${state.data.sections.agent_runtime.revision}`}
                section="agent_runtime"
                title={labels.sections.agentRuntime.title}
                description={labels.sections.agentRuntime.description}
                value={state.data.sections.agent_runtime.value}
                revision={state.data.sections.agent_runtime.revision}
                effectiveRevision={
                  state.data.sections.agent_runtime.effective_revision
                }
                effectScope={state.data.sections.agent_runtime.effect_scope}
                updatedAt={state.data.sections.agent_runtime.updated_at}
                schema={agentRuntimeSettingsValueSchema}
                validate={(value) =>
                  validateAgentRuntimeModelReferences(
                    value,
                    activeModels.map((model) => model.name),
                    selectVisionInputModels(activeModels).map(
                      (model) => model.name,
                    ),
                  )
                }
                pending={pendingSection === "agent_runtime"}
                error={sectionErrors.agent_runtime}
                lastResult={lastResults.agent_runtime}
                activeModels={activeModels}
                modelsStatus={modelsStatus}
                onDraftChange={recordDraft}
                onSave={onSave}
                renderEditor={(value, setValue, models, modelStatus) => (
                  <AgentRuntimeEditor
                    activeGroup={activeAgentGroup}
                    value={value}
                    onChange={setValue}
                    activeModels={models}
                    modelsStatus={modelStatus}
                  />
                )}
              />
            </div>
            <div hidden={activeDestination !== "memory"}>
              <EditableSection
                key={`memory-document-${state.data.sections.memory_document.revision}`}
                section="memory_document"
                title={labels.sections.memoryDocument.title}
                description={labels.sections.memoryDocument.description}
                value={state.data.sections.memory_document.value}
                revision={state.data.sections.memory_document.revision}
                effectiveRevision={
                  state.data.sections.memory_document.effective_revision
                }
                effectScope={state.data.sections.memory_document.effect_scope}
                updatedAt={state.data.sections.memory_document.updated_at}
                schema={memoryDocumentSettingsValueSchema}
                pending={pendingSection === "memory_document"}
                error={sectionErrors.memory_document}
                lastResult={lastResults.memory_document}
                activeModels={activeModels}
                modelsStatus={modelsStatus}
                onDraftChange={recordDraft}
                onSave={onSave}
                renderEditor={(value, setValue) => (
                  <MemoryDocumentSectionsEditor
                    value={value}
                    onChange={setValue}
                  />
                )}
              />
            </div>
          </div>
        </div>
      )}
      <Dialog
        open={pendingLeave !== null}
        onOpenChange={(open) => {
          if (!open) setPendingLeave(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {locale === "zh-CN"
                ? "放弃未保存修改？"
                : "Discard unsaved changes?"}
            </DialogTitle>
            <DialogDescription>
              {locale === "zh-CN"
                ? "系统配置草稿只保存在当前页面。离开后将丢失这些修改。"
                : "System-setting drafts exist only on this page and will be lost if you leave."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setPendingLeave(null)}
            >
              {locale === "zh-CN" ? "继续编辑" : "Continue editing"}
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => {
                const leave = pendingLeave;
                setPendingLeave(null);
                if (!leave) return;
                allowLeaveRef.current = true;
                if (leave.viaHistory) {
                  window.history.back();
                  return;
                }
                const url = new URL(leave.href, window.location.href);
                if (url.origin === window.location.origin && onNavigate) {
                  onNavigate(`${url.pathname}${url.search}${url.hash}`);
                } else {
                  window.location.assign(url.href);
                }
              }}
            >
              {locale === "zh-CN"
                ? "放弃修改并离开"
                : "Discard changes and leave"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </AdminPage>
  );
}

function AuthorizedAdminSystemSettingsPage({
  accountId,
}: {
  accountId: string;
}) {
  const { t } = useI18n();
  const router = useRouter();
  const labels = t.adminSystemSettings;
  const catalog = useAdminSystemSettings(accountId);
  const models = useModels();
  const replaceSection = useReplaceAdminSystemSettingsSection(accountId);
  const [pendingSection, setPendingSection] =
    useState<SystemSettingsSectionName | null>(null);
  const [sectionErrors, setSectionErrors] = useState<SectionErrors>({});
  const [lastResults, setLastResults] = useState<LastResults>({});
  const state: AdminSystemSettingsCatalogState = catalog.isLoading
    ? { status: "loading" }
    : catalog.error || !catalog.data
      ? { status: "error" }
      : { status: "ready", data: catalog.data };
  const modelsStatus: ModelsStatus = models.isLoading
    ? "loading"
    : models.error
      ? "error"
      : "ready";

  async function saveSection(
    section: SystemSettingsSectionName,
    value: SystemSettingsSectionValueMap[SystemSettingsSectionName],
    expectedRevision: number,
  ): Promise<SystemSettingsMutationResponse | null> {
    setPendingSection(section);
    setSectionErrors((current) => ({ ...current, [section]: undefined }));
    setLastResults((current) => ({ ...current, [section]: undefined }));
    try {
      let result: SystemSettingsMutationResponse;
      switch (section) {
        case "agent_runtime": {
          const parsed = validateAgentRuntimeModelReferences(
            value,
            models.models.map((model) => model.name),
            selectVisionInputModels(models.models).map((model) => model.name),
          );
          result = await replaceSection.mutateAsync({
            section,
            input: { expected_revision: expectedRevision, value: parsed },
          });
          break;
        }
        case "auth":
          result = await replaceSection.mutateAsync({
            section,
            input: {
              expected_revision: expectedRevision,
              value: authSettingsValueSchema.parse(value),
            },
          });
          break;
        case "automations":
          result = await replaceSection.mutateAsync({
            section,
            input: {
              expected_revision: expectedRevision,
              value: automationsSettingsValueSchema.parse(value),
            },
          });
          break;
        case "memory_document":
          result = await replaceSection.mutateAsync({
            section,
            input: {
              expected_revision: expectedRevision,
              value: memoryDocumentSettingsValueSchema.parse(value),
            },
          });
          break;
        case "quotas":
          result = await replaceSection.mutateAsync({
            section,
            input: {
              expected_revision: expectedRevision,
              value: quotaSettingsValueSchema.parse(value),
            },
          });
          break;
      }
      setLastResults((current) => ({ ...current, [section]: result }));
      return result;
    } catch (error) {
      const kind = safeActionError(error, "generic");
      const message =
        kind === "auth"
          ? labels.feedback.authRequired
          : kind === "conflict"
            ? labels.feedback.conflict
            : kind === "invalid"
              ? labels.feedback.invalid
              : labels.feedback.generic;
      setSectionErrors((current) => ({ ...current, [section]: message }));
      return null;
    } finally {
      setPendingSection(null);
    }
  }

  return (
    <AdminSystemSettingsStateView
      state={state}
      activeModels={models.models}
      modelsStatus={modelsStatus}
      pendingSection={pendingSection}
      sectionErrors={sectionErrors}
      lastResults={lastResults}
      onNavigate={(href) => router.push(href)}
      onSave={saveSection}
      onRetry={() => void catalog.refetch()}
      retrying={catalog.isFetching}
    />
  );
}

/**
 * Client-side defence in depth. The server /admin layout and Gateway remain
 * authoritative; no system-settings query is constructed before this gate.
 */
export function AdminSystemSettingsPage() {
  const { user } = useAuth();
  if (user?.system_role !== "system_admin") return null;
  return <AuthorizedAdminSystemSettingsPage accountId={user.id} />;
}

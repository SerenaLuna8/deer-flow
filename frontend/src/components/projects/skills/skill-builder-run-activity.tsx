"use client";

import {
  CheckCircle2Icon,
  CircleIcon,
  Loader2Icon,
  TriangleAlertIcon,
  WrenchIcon,
} from "lucide-react";

import type {
  SkillBuilderRunAdmission,
  SkillBuilderRunPresentation,
  SkillBuilderRunPresentationStatus,
  SkillBuilderRunStreamProjection,
  SkillBuilderRunToolStepProjection,
} from "@/core/skill-builder";
import { cn } from "@/lib/utils";

const RUN_STATUS_LABELS: Record<SkillBuilderRunPresentationStatus, string> = {
  pending: "已排队，等待执行",
  running: "正在执行",
  success: "本轮已完成",
  error: "本轮执行失败",
  timeout: "本轮执行超时",
  interrupted: "本轮执行已中断",
  cancelled: "本轮执行已取消",
};

const TOOL_STATUS_LABELS: Record<
  SkillBuilderRunToolStepProjection["status"],
  string
> = {
  pending: "等待调用",
  running: "调用中",
  completed: "已完成",
  failed: "调用失败",
};

function ToolStatusIcon({
  status,
}: {
  status: SkillBuilderRunToolStepProjection["status"];
}) {
  if (status === "running") {
    return <Loader2Icon aria-hidden className="size-3.5 animate-spin" />;
  }
  if (status === "completed") {
    return <CheckCircle2Icon aria-hidden className="text-success size-3.5" />;
  }
  if (status === "failed") {
    return (
      <TriangleAlertIcon aria-hidden className="text-destructive size-3.5" />
    );
  }
  return <CircleIcon aria-hidden className="text-muted-foreground size-3.5" />;
}

export function skillBuilderRunIsActive(
  status: SkillBuilderRunPresentationStatus,
) {
  return status === "pending" || status === "running";
}

export function SkillBuilderRunActivity({
  activeRun,
  projection,
  presentation,
  failureCode,
}: {
  activeRun?: SkillBuilderRunAdmission | null;
  projection?: SkillBuilderRunStreamProjection | null;
  presentation?: SkillBuilderRunPresentation | null;
  failureCode?: string | null;
}) {
  const status =
    projection?.status ?? activeRun?.status ?? presentation?.status;
  if (!status) return null;

  const steps = projection?.toolSteps ?? [];
  const active = skillBuilderRunIsActive(status);

  return (
    <details
      className="border-border/70 bg-muted/20 rounded-xl border px-3 py-2"
      data-testid="skill-builder-run-activity"
      open={active || steps.length > 0}
    >
      <summary className="flex min-h-7 cursor-pointer list-none items-center gap-2 text-xs font-medium">
        {active ? (
          <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
        ) : status === "success" ? (
          <CheckCircle2Icon aria-hidden className="text-success size-3.5" />
        ) : (
          <TriangleAlertIcon
            aria-hidden
            className="text-destructive size-3.5"
          />
        )}
        <span>{RUN_STATUS_LABELS[status]}</span>
        <span className="text-muted-foreground font-normal">
          {steps.length > 0 ? `· ${steps.length} 个工具步骤` : "· 尚无工具步骤"}
        </span>
      </summary>

      {steps.length > 0 ? (
        <ol className="border-border/70 mt-2 space-y-1.5 border-t pt-2">
          {steps.map((step) => (
            <li
              key={step.id}
              className={cn(
                "flex items-center gap-2 text-xs",
                step.status === "failed" && "text-destructive",
              )}
            >
              <ToolStatusIcon status={step.status} />
              <WrenchIcon aria-hidden className="size-3.5" />
              <span className="min-w-0 flex-1 truncate font-mono">
                {step.toolName}
              </span>
              <span className="text-muted-foreground shrink-0">
                {TOOL_STATUS_LABELS[step.status]}
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-muted-foreground border-border/70 mt-2 border-t pt-2 text-xs leading-5">
          当前后端仅提供了可靠的 Run 状态，尚无可安全展示的工具步骤。
        </p>
      )}
      {status === "error" && failureCode === "MODEL_OUTPUT_LIMIT" ? (
        <p
          role="alert"
          className="border-border/70 mt-2 border-t pt-2 text-xs leading-5"
        >
          本轮达到模型输出上限。已成功写入的候选草稿仍然保留；请在下方继续发送“基于现有草稿继续完成”，Builder
          会重新读取草稿后续作，不会执行残缺的工具调用。
        </p>
      ) : null}
    </details>
  );
}

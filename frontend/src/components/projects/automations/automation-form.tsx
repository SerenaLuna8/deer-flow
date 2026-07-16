"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  AutomationScheduleInput,
  type AutomationScheduleValue,
} from "@/components/workspace/scheduled-task-schedule-input";
import type {
  Automation,
  CreateAutomationInput,
  UpdateAutomationInput,
} from "@/core/project-automations/types";
import { RECIPES, type RecipeTitleKey } from "@/core/scheduled-tasks/recipes";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

const RECIPE_TITLES: Record<RecipeTitleKey, string> = {
  trending: "GitHub Trending",
  news: "今日科技新闻",
  issues: "Issue 分诊",
  weekly: "每周项目报告",
};

const DEFAULT_SCHEDULE: AutomationScheduleValue = {
  schedule_type: "cron",
  schedule_spec: { cron: "0 9 * * *" },
  timezone: "",
};

export type AutomationAgentOption = {
  id: string;
  scope: "project" | "system";
  displayName: string;
};

export type AutomationFormDraft = {
  title: string;
  prompt: string;
  contextMode: "fresh_thread_per_run" | "reuse_thread";
  threadId: string;
  agentAssetId: string;
  agentScope: "project" | "system" | "";
  schedule: AutomationScheduleValue;
};

type FormSubmission =
  | {
      ok: true;
      input: CreateAutomationInput | UpdateAutomationInput;
    }
  | { ok: false; message: string };

type BuildSubmissionOptions =
  | { mode: "create"; draft: AutomationFormDraft }
  | {
      mode: "edit";
      draft: AutomationFormDraft;
      expectedVersion: number;
    };

function scheduleSpec(value: AutomationScheduleValue): Record<string, unknown> {
  if (value.schedule_type === "once") {
    return { run_at: value.schedule_spec.run_at };
  }
  return { cron: value.schedule_spec.cron };
}

export function buildAutomationFormSubmission(
  options: BuildSubmissionOptions,
  now = new Date(),
): FormSubmission {
  const { draft } = options;
  const title = draft.title.trim();
  const prompt = draft.prompt.trim();
  const timezone = draft.schedule.timezone.trim();

  if (!title) return { ok: false, message: "请输入 title。" };
  if (title.length > 255) {
    return { ok: false, message: "Title 不能超过 255 个字符。" };
  }
  if (!prompt) return { ok: false, message: "请输入 prompt。" };
  if (!UUID_PATTERN.test(draft.agentAssetId) || !draft.agentScope) {
    return { ok: false, message: "请选择可执行的 Agent。" };
  }
  if (
    draft.contextMode === "reuse_thread" &&
    !UUID_PATTERN.test(draft.threadId.trim())
  ) {
    return { ok: false, message: "请输入有效的 Thread UUID。" };
  }
  if (!timezone) return { ok: false, message: "请选择时区。" };
  if (timezone.length > 64) {
    return { ok: false, message: "时区名称不能超过 64 个字符。" };
  }

  if (draft.schedule.schedule_type === "cron") {
    const cron = draft.schedule.schedule_spec.cron?.trim() ?? "";
    if (cron.split(/\s+/u).filter(Boolean).length !== 5) {
      return { ok: false, message: "Cron 必须包含 5 个字段。" };
    }
  } else {
    const runAt = draft.schedule.schedule_spec.run_at?.trim() ?? "";
    const parsedRunAt = Date.parse(runAt);
    if (
      !runAt ||
      !Number.isFinite(parsedRunAt) ||
      parsedRunAt <= now.getTime()
    ) {
      return { ok: false, message: "单次执行时间必须在未来。" };
    }
  }

  if (options.mode === "edit") {
    if (
      !Number.isInteger(options.expectedVersion) ||
      options.expectedVersion < 1
    ) {
      return { ok: false, message: "Automation 版本无效，请刷新。" };
    }
    return {
      ok: true,
      input: {
        expected_version: options.expectedVersion,
        title,
        prompt,
        schedule_spec: scheduleSpec(draft.schedule),
        timezone,
      },
    };
  }

  return {
    ok: true,
    input: {
      title,
      prompt,
      context_mode: draft.contextMode,
      thread_id:
        draft.contextMode === "reuse_thread" ? draft.threadId.trim() : null,
      agent_asset_id: draft.agentAssetId,
      agent_scope: draft.agentScope,
      schedule_type: draft.schedule.schedule_type,
      schedule_spec: scheduleSpec(draft.schedule),
      timezone,
    },
  };
}

function initialDraft(
  initial: Automation | undefined,
  initialThreadId: string | undefined,
): AutomationFormDraft {
  if (initial) {
    return {
      title: initial.title,
      prompt: initial.prompt,
      contextMode: initial.context_mode,
      threadId: initial.thread_id ?? "",
      agentAssetId: initial.agent_asset_id,
      agentScope: initial.agent_scope,
      schedule: {
        schedule_type: initial.schedule_type,
        schedule_spec: {
          cron:
            typeof initial.schedule_spec.cron === "string"
              ? initial.schedule_spec.cron
              : undefined,
          run_at:
            typeof initial.schedule_spec.run_at === "string"
              ? initial.schedule_spec.run_at
              : undefined,
        },
        timezone: initial.timezone,
      },
    };
  }
  return {
    title: "",
    prompt: "",
    contextMode: initialThreadId ? "reuse_thread" : "fresh_thread_per_run",
    threadId: initialThreadId ?? "",
    agentAssetId: "",
    agentScope: "",
    schedule: DEFAULT_SCHEDULE,
  };
}

export function AutomationForm({
  mode,
  initial,
  initialThreadId,
  agents,
  canSubmit,
  onSubmit,
  onCancel,
}: {
  mode: "create" | "edit";
  initial?: Automation;
  initialThreadId?: string;
  agents: AutomationAgentOption[];
  canSubmit: boolean;
  onSubmit: (
    input: CreateAutomationInput | UpdateAutomationInput,
  ) => void | Promise<void>;
  onCancel?: () => void;
}) {
  const immutable = mode === "edit";
  const [draft, setDraft] = useState(() =>
    initialDraft(initial, initialThreadId),
  );
  const [formError, setFormError] = useState<string | null>(null);

  const selectAgent = (value: string) => {
    const agent = agents.find(({ id, scope }) => `${scope}:${id}` === value);
    setDraft((current) => ({
      ...current,
      agentAssetId: agent?.id ?? "",
      agentScope: agent?.scope ?? "",
    }));
  };

  return (
    <form
      className="space-y-5"
      data-testid="automation-form"
      onSubmit={(event) => {
        event.preventDefault();
        const result = buildAutomationFormSubmission(
          mode === "edit"
            ? {
                mode,
                draft,
                expectedVersion: initial?.version ?? 0,
              }
            : { mode, draft },
        );
        if (!result.ok) {
          setFormError(result.message);
          return;
        }
        setFormError(null);
        void onSubmit(result.input);
      }}
    >
      {mode === "create" ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-muted-foreground text-sm">模板</span>
          {RECIPES.map((recipe) => (
            <Button
              key={recipe.id}
              type="button"
              size="sm"
              variant="outline"
              onClick={() =>
                setDraft((current) => ({
                  ...current,
                  title: RECIPE_TITLES[recipe.titleKey],
                  prompt: recipe.prompt,
                  schedule: recipe.schedule,
                }))
              }
            >
              <span aria-hidden>{recipe.icon}</span>
              {RECIPE_TITLES[recipe.titleKey]}
            </Button>
          ))}
        </div>
      ) : null}

      <fieldset className="space-y-2" disabled={immutable}>
        <legend className="text-sm font-medium">对话上下文</legend>
        <div
          className="flex flex-wrap gap-2"
          data-testid="automation-context-mode"
        >
          <Button
            type="button"
            size="sm"
            variant={
              draft.contextMode === "fresh_thread_per_run"
                ? "default"
                : "outline"
            }
            aria-pressed={draft.contextMode === "fresh_thread_per_run"}
            onClick={() =>
              setDraft((current) => ({
                ...current,
                contextMode: "fresh_thread_per_run",
                threadId: "",
              }))
            }
          >
            每次新建 Thread
          </Button>
          <Button
            type="button"
            size="sm"
            variant={
              draft.contextMode === "reuse_thread" ? "default" : "outline"
            }
            aria-pressed={draft.contextMode === "reuse_thread"}
            onClick={() =>
              setDraft((current) => ({
                ...current,
                contextMode: "reuse_thread",
              }))
            }
          >
            复用 Thread
          </Button>
        </div>
        {draft.contextMode === "reuse_thread" ? (
          <Input
            aria-label="Thread UUID"
            value={draft.threadId}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                threadId: event.target.value,
              }))
            }
          />
        ) : null}
      </fieldset>

      <label className="block space-y-2 text-sm font-medium">
        <span>Agent</span>
        <select
          data-testid="automation-agent"
          className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm disabled:opacity-50"
          value={
            draft.agentAssetId
              ? `${draft.agentScope}:${draft.agentAssetId}`
              : ""
          }
          disabled={immutable}
          onChange={(event) => selectAgent(event.target.value)}
        >
          <option value="">请选择可执行 Agent</option>
          {agents.map((agent) => (
            <option
              key={`${agent.scope}:${agent.id}`}
              value={`${agent.scope}:${agent.id}`}
            >
              {agent.displayName} ·{" "}
              {agent.scope === "project" ? "项目" : "系统"}
            </option>
          ))}
        </select>
      </label>

      <label className="block space-y-2 text-sm font-medium">
        <span>Title</span>
        <Input
          value={draft.title}
          maxLength={255}
          onChange={(event) =>
            setDraft((current) => ({ ...current, title: event.target.value }))
          }
        />
      </label>

      <label className="block space-y-2 text-sm font-medium">
        <span>Prompt</span>
        <Textarea
          rows={6}
          value={draft.prompt}
          onChange={(event) =>
            setDraft((current) => ({ ...current, prompt: event.target.value }))
          }
        />
      </label>

      <div className="space-y-2">
        <p className="text-sm font-medium">Schedule</p>
        <AutomationScheduleInput
          initial={draft.schedule}
          scheduleTypeLocked={immutable}
          onChange={(schedule) =>
            setDraft((current) => ({ ...current, schedule }))
          }
        />
      </div>

      {formError ? (
        <p role="alert" className="text-destructive text-sm">
          {formError}
        </p>
      ) : null}

      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        {onCancel ? (
          <Button type="button" variant="outline" onClick={onCancel}>
            取消
          </Button>
        ) : null}
        <Button type="submit" disabled={!canSubmit}>
          {mode === "create" ? "创建 Automation" : "保存修改"}
        </Button>
      </div>
    </form>
  );
}

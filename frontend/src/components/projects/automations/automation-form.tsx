"use client";

import { useEffect, useRef, useState } from "react";

import {
  AutomationScheduleInput,
  detectBrowserTimezone,
  type AutomationScheduleValue,
} from "@/components/projects/automations/automation-schedule-input";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/core/i18n/hooks";
import {
  RECIPES,
  type Recipe,
  type RecipeTitleKey,
} from "@/core/project-automations/schedule/recipes";
import type {
  Automation,
  CreateAutomationInput,
  UpdateAutomationInput,
} from "@/core/project-automations/types";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

const RECIPE_TITLES: Record<RecipeTitleKey, string> = {
  trending: "GitHub Trending",
  news: "今日科技新闻",
  issues: "Issue 分诊",
  weekly: "每周项目报告",
};

function RequiredMark({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-0.5">
      {label}
      <span className="text-destructive" aria-hidden>
        *
      </span>
    </span>
  );
}

export type AutomationAgentOption = {
  id: string;
  scope: "project" | "system";
  displayName: string;
  isDefault?: boolean;
};

export type AutomationThreadOption = {
  id: string;
  title: string;
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

function validTimezone(value: string): boolean {
  if (!value) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value }).format();
    return true;
  } catch {
    return false;
  }
}

export function applyAutomationRecipeToDraft(
  draft: AutomationFormDraft,
  recipe: Recipe,
  fallbackTimezone = detectBrowserTimezone(),
): AutomationFormDraft {
  const currentTimezone = draft.schedule.timezone.trim();
  const detectedTimezone = fallbackTimezone.trim();
  return {
    ...draft,
    title: RECIPE_TITLES[recipe.titleKey],
    prompt: recipe.prompt,
    schedule: {
      schedule_type: recipe.schedule.schedule_type,
      schedule_spec: { ...recipe.schedule.schedule_spec },
      timezone: validTimezone(currentTimezone)
        ? currentTimezone
        : validTimezone(detectedTimezone)
          ? detectedTimezone
          : "UTC",
    },
  };
}

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
      initial: Automation;
    };

function scheduleSpec(value: AutomationScheduleValue): Record<string, unknown> {
  if (value.schedule_type === "once") {
    return { run_at: value.schedule_spec.run_at };
  }
  return { cron: value.schedule_spec.cron };
}

function sameSchedule(
  current: AutomationScheduleValue,
  initial: Automation,
): boolean {
  if (current.schedule_type !== initial.schedule_type) return false;
  if (current.schedule_type === "once") {
    const currentRunAt = current.schedule_spec.run_at;
    const initialRunAt = initial.schedule_spec.run_at;
    if (
      typeof currentRunAt !== "string" ||
      typeof initialRunAt !== "string" ||
      Object.keys(initial.schedule_spec).length !== 1
    ) {
      return false;
    }
    const currentTimestamp = Date.parse(currentRunAt);
    const initialTimestamp = Date.parse(initialRunAt);
    return (
      Number.isFinite(currentTimestamp) &&
      Number.isFinite(initialTimestamp) &&
      currentTimestamp === initialTimestamp
    );
  }
  const currentCron = current.schedule_spec.cron;
  const initialCron = initial.schedule_spec.cron;
  return (
    typeof currentCron === "string" &&
    typeof initialCron === "string" &&
    Object.keys(initial.schedule_spec).length === 1 &&
    currentCron.trim().replace(/\s+/gu, " ") ===
      initialCron.trim().replace(/\s+/gu, " ")
  );
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
    return { ok: false, message: "请选择要复用的会话。" };
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
    const { initial } = options;
    if (!Number.isInteger(initial.version) || initial.version < 1) {
      return { ok: false, message: "Automation 版本无效，请刷新。" };
    }
    const input: UpdateAutomationInput = {
      expected_version: initial.version,
    };
    if (title !== initial.title) input.title = title;
    if (prompt !== initial.prompt) input.prompt = prompt;
    const nextScheduleSpec = scheduleSpec(draft.schedule);
    if (!sameSchedule(draft.schedule, initial)) {
      input.schedule_spec = nextScheduleSpec;
    }
    if (timezone !== initial.timezone) input.timezone = timezone;
    return {
      ok: true,
      input,
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

function defaultAutomationAgent(
  agents: readonly AutomationAgentOption[],
): AutomationAgentOption | undefined {
  return agents.find((agent) => agent.isDefault);
}

function initialDraft(
  initial: Automation | undefined,
  initialThreadId: string | undefined,
  agents: readonly AutomationAgentOption[],
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
  const defaultAgent = defaultAutomationAgent(agents);
  return {
    title: "",
    prompt: "",
    contextMode: initialThreadId ? "reuse_thread" : "fresh_thread_per_run",
    threadId: initialThreadId ?? "",
    agentAssetId: defaultAgent?.id ?? "",
    agentScope: defaultAgent?.scope ?? "",
    schedule: {
      schedule_type: "cron",
      schedule_spec: { cron: "0 9 * * *" },
      timezone: detectBrowserTimezone(),
    },
  };
}

export function AutomationForm({
  mode,
  initial,
  initialThreadId,
  agents,
  threads = [],
  threadsLoading = false,
  threadsError = null,
  canSubmit,
  onSubmit,
  onCancel,
}: {
  mode: "create" | "edit";
  initial?: Automation;
  initialThreadId?: string;
  agents: AutomationAgentOption[];
  threads?: AutomationThreadOption[];
  threadsLoading?: boolean;
  threadsError?: Error | null;
  canSubmit: boolean;
  onSubmit: (
    input: CreateAutomationInput | UpdateAutomationInput,
  ) => void | Promise<void>;
  onCancel?: () => void;
}) {
  const { t, locale } = useI18n();
  const immutable = mode === "edit";
  const createMode = mode === "create";
  const [draft, setDraft] = useState(() =>
    initialDraft(initial, initialThreadId, agents),
  );
  const initialDefaultAgent = defaultAutomationAgent(agents);
  const defaultAgentApplied = useRef(
    mode === "edit" || Boolean(initialDefaultAgent),
  );
  const [scheduleRevision, setScheduleRevision] = useState(0);
  const [validationVisible, setValidationVisible] = useState(false);
  const defaultAgent = defaultAutomationAgent(agents);
  const submission: FormSubmission =
    mode === "edit"
      ? initial
        ? buildAutomationFormSubmission({ mode, draft, initial })
        : { ok: false, message: "Automation 版本无效，请刷新。" }
      : buildAutomationFormSubmission({ mode, draft });
  const formError =
    validationVisible && !submission.ok ? submission.message : null;
  const selectedThreadIsMissing =
    Boolean(draft.threadId) &&
    !threads.some((thread) => thread.id === draft.threadId);
  const threadSelectDisabled =
    immutable ||
    threadsLoading ||
    (threads.length === 0 && !selectedThreadIsMissing);

  useEffect(() => {
    if (mode !== "create" || defaultAgentApplied.current || !defaultAgent) {
      return;
    }
    defaultAgentApplied.current = true;
    setDraft((current) =>
      current.agentAssetId
        ? current
        : {
            ...current,
            agentAssetId: defaultAgent.id,
            agentScope: defaultAgent.scope,
          },
    );
  }, [defaultAgent, mode]);

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
      className={createMode ? "space-y-7" : "space-y-5"}
      data-layout="prompt-first"
      data-testid="automation-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!submission.ok) {
          setValidationVisible(true);
          return;
        }
        setValidationVisible(false);
        void onSubmit(submission.input);
      }}
    >
      {createMode ? (
        <section
          className="grid gap-3 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-center"
          data-testid="automation-template-strip"
        >
          <h3 className="text-muted-foreground text-sm font-medium">模板</h3>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {RECIPES.map((recipe) => (
              <Button
                key={recipe.id}
                className="h-11 w-full justify-start px-3 lg:justify-center"
                type="button"
                variant="outline"
                onClick={() => {
                  setDraft((current) =>
                    applyAutomationRecipeToDraft(current, recipe),
                  );
                  setScheduleRevision((current) => current + 1);
                }}
              >
                <span aria-hidden className="text-base leading-none">
                  {recipe.icon}
                </span>
                {RECIPE_TITLES[recipe.titleKey]}
              </Button>
            ))}
          </div>
        </section>
      ) : null}

      <section className="space-y-5" data-testid="automation-task-composer">
        <label className="block space-y-2 text-sm font-medium">
          <RequiredMark label={t.automation.fields.title} />
          <Input
            className={createMode ? "h-12" : undefined}
            value={draft.title}
            maxLength={255}
            aria-required="true"
            placeholder={
              locale.startsWith("zh") ? "输入标题…" : "Enter a title…"
            }
            onChange={(event) =>
              setDraft((current) => ({ ...current, title: event.target.value }))
            }
          />
        </label>

        <label className="block space-y-2 text-sm font-medium">
          <RequiredMark label={t.automation.fields.prompt} />
          <Textarea
            className={createMode ? "min-h-40 resize-y" : "resize-y"}
            rows={5}
            value={draft.prompt}
            aria-required="true"
            placeholder={
              locale.startsWith("zh")
                ? "输入要让 Agent 执行的任务或指令…"
                : "Describe the task or instruction for the Agent…"
            }
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                prompt: event.target.value,
              }))
            }
          />
        </label>
      </section>

      <section
        className="grid gap-5 border-t pt-5 sm:grid-cols-2 sm:items-start"
        data-testid="automation-run-context"
      >
        <fieldset className="space-y-2" disabled={immutable}>
          <legend className="text-sm font-medium">对话上下文</legend>
          <div
            className="flex flex-wrap gap-2"
            data-testid="automation-context-mode"
          >
            <Button
              type="button"
              size={createMode ? "default" : "sm"}
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
              size={createMode ? "default" : "sm"}
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
        </fieldset>

        <label className="block space-y-2 text-sm font-medium">
          <RequiredMark label="Agent" />
          <select
            data-testid="automation-agent"
            className={`border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 w-full rounded-md border px-3 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:ring-[3px] disabled:opacity-50 ${
              createMode ? "h-12" : "h-9"
            }`}
            value={
              draft.agentAssetId
                ? `${draft.agentScope}:${draft.agentAssetId}`
                : ""
            }
            aria-required="true"
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

        {draft.contextMode === "reuse_thread" ? (
          <div
            className="space-y-2 sm:col-span-2"
            data-testid="automation-reuse-thread"
          >
            <label
              className="block text-sm font-medium"
              htmlFor="automation-thread-select"
            >
              <RequiredMark label="选择已有会话" />
            </label>
            <select
              id="automation-thread-select"
              aria-label="选择已有会话"
              aria-describedby="automation-thread-help"
              data-testid="automation-thread-select"
              className={`border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 w-full rounded-md border px-3 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50 ${
                createMode ? "h-12" : "h-9"
              }`}
              value={draft.threadId}
              aria-required="true"
              disabled={threadSelectDisabled}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  threadId: event.target.value,
                }))
              }
            >
              <option value="" disabled>
                {threadsLoading
                  ? "正在加载会话…"
                  : threadsError
                    ? "会话列表不可用"
                    : threads.length === 0
                      ? "暂无可复用会话"
                      : "选择一个会话"}
              </option>
              {selectedThreadIsMissing ? (
                <option value={draft.threadId}>当前绑定会话</option>
              ) : null}
              {threads.map((thread) => (
                <option key={thread.id} value={thread.id}>
                  {thread.title}
                </option>
              ))}
            </select>
            <p
              id="automation-thread-help"
              className={
                threadsError
                  ? "text-destructive text-xs"
                  : "text-muted-foreground text-xs"
              }
              role={threadsError ? "alert" : undefined}
            >
              {threadsError
                ? "无法加载会话列表，请关闭后重试。"
                : threads.length === 0 && !selectedThreadIsMissing
                  ? "暂无可复用会话，请先在“会话”中创建一个会话。"
                  : "Automation 会在所选会话中延续上下文。"}
            </p>
          </div>
        ) : null}
      </section>

      <section
        className="space-y-3 border-t pt-5"
        data-testid="automation-schedule-section"
      >
        <AutomationScheduleInput
          key={scheduleRevision}
          initial={draft.schedule}
          layout={createMode ? "compact" : "stacked"}
          scheduleTypeLocked={immutable}
          label={<RequiredMark label={t.automation.fields.schedule} />}
          onChange={(schedule) =>
            setDraft((current) => ({ ...current, schedule }))
          }
        />
      </section>

      {formError ? (
        <p role="alert" className="text-destructive text-sm">
          {formError}
        </p>
      ) : null}

      <div className="flex flex-col-reverse gap-2 border-t pt-5 sm:flex-row sm:justify-end">
        {onCancel ? (
          <Button
            type="button"
            size={createMode ? "lg" : "default"}
            variant="outline"
            onClick={onCancel}
          >
            取消
          </Button>
        ) : null}
        <Button
          type="submit"
          size={createMode ? "lg" : "default"}
          disabled={!canSubmit}
        >
          {createMode ? t.automation.create : "保存修改"}
        </Button>
      </div>
    </form>
  );
}

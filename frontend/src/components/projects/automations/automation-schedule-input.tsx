"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useI18n } from "@/core/i18n/hooks";
import {
  describeSchedule,
  pad2,
  parseCron,
  serializeCron,
  utcToZonedLocalInput,
  WEEKDAYS,
  zonedLocalToUtcIso,
  type CronParts,
  type CronPreset,
  type ScheduleLocale,
  type Weekday,
} from "@/core/project-automations/schedule/cron";
import type { ProjectAutomationSchedule } from "@/core/project-automations/schedule/types";
import { cn } from "@/lib/utils";

const COMPACT_CONTROL_CLASS =
  "h-11 min-h-11 py-0 text-sm leading-none data-[size=default]:h-11";

export type AutomationScheduleValue = ProjectAutomationSchedule;

const PRESETS: CronPreset[] = [
  "hourly",
  "daily",
  "weekly",
  "monthly",
  "custom",
];

const FALLBACK_TIMEZONES = [
  "UTC",
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Asia/Singapore",
  "Europe/London",
  "Europe/Berlin",
  "America/New_York",
  "America/Chicago",
  "America/Los_Angeles",
];

export function detectBrowserTimezone(): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (typeof tz === "string" && tz.length > 0) {
      return tz;
    }
  } catch {
    // resolvedOptions unavailable
  }
  return "UTC";
}

function timezoneOptions(): string[] {
  const supported = (
    Intl as unknown as {
      supportedValuesOf?: (key: string) => string[] | undefined;
    }
  ).supportedValuesOf?.("timeZone");
  if (Array.isArray(supported) && supported.length > 0) {
    return supported;
  }
  return FALLBACK_TIMEZONES;
}

const TIMEZONE_OPTIONS = timezoneOptions();

export function AutomationScheduleInput({
  initial,
  onChange,
  scheduleTypeLocked = false,
  layout = "stacked",
  label,
}: {
  initial: ProjectAutomationSchedule;
  onChange: (value: ProjectAutomationSchedule) => void;
  scheduleTypeLocked?: boolean;
  layout?: "stacked" | "compact";
  label?: ReactNode;
}) {
  const { t, locale } = useI18n();
  const schedLocale: ScheduleLocale = locale.startsWith("zh") ? "zh" : "en";
  const labels = t.scheduledTasks;

  const [scheduleType, setScheduleType] = useState<"once" | "cron">(
    initial.schedule_type,
  );
  const [preset, setPreset] = useState<CronPreset>(
    () => parseCron(initial.schedule_spec.cron ?? "0 9 * * *").preset,
  );
  const [parts, setParts] = useState<CronParts>(
    () => parseCron(initial.schedule_spec.cron ?? "0 9 * * *").parts,
  );
  const [runAtLocal, setRunAtLocal] = useState<string>(
    initial.schedule_type === "once" && initial.schedule_spec.run_at
      ? utcToZonedLocalInput(
          initial.schedule_spec.run_at,
          initial.timezone || "UTC",
        )
      : "",
  );
  const [timezone, setTimezone] = useState<string>(
    initial.timezone || detectBrowserTimezone(),
  );

  // Hold the latest onChange in a ref so the effect below does not depend on
  // it. This avoids a re-render loop: if the parent passes an inline
  // onChange (new reference each render), depending on it directly would
  // re-fire the effect every render and call onChange again, looping.
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  // Emit on every change including mount. On mount this syncs the parent with
  // the browser-detected timezone and the canonicalized cron, so the submitted
  // value always matches what the user sees in the preview.
  useEffect(() => {
    if (scheduleType === "once") {
      const runAt = runAtLocal ? zonedLocalToUtcIso(runAtLocal, timezone) : "";
      onChangeRef.current({
        schedule_type: "once",
        schedule_spec: runAt ? { run_at: runAt } : {},
        timezone,
      });
      return;
    }
    const cron =
      preset === "custom" ? (parts.raw ?? "") : serializeCron(preset, parts);
    onChangeRef.current({
      schedule_type: "cron",
      schedule_spec: cron ? { cron } : {},
      timezone,
    });
  }, [scheduleType, preset, parts, runAtLocal, timezone]);

  function updateParts(patch: Partial<CronParts>) {
    setParts((prev) => ({ ...prev, ...patch }));
  }

  function changePreset(next: CronPreset) {
    setParts((prev) => {
      const merged = { ...prev };
      if (next === "weekly" && (merged.weekdays ?? []).length === 0) {
        merged.weekdays = ["mon"];
      }
      if (next === "monthly" && merged.dayOfMonth == null) {
        merged.dayOfMonth = 1;
      }
      if (next === "custom" && !merged.raw) {
        merged.raw = serializeCron("daily", prev);
      }
      return merged;
    });
    setPreset(next);
  }

  function toggleWeekday(w: Weekday) {
    setParts((prev) => {
      const set = new Set(prev.weekdays ?? []);
      if (set.has(w)) {
        if (set.size <= 1) {
          return prev;
        }
        set.delete(w);
      } else {
        set.add(w);
      }
      return { ...prev, weekdays: WEEKDAYS.filter((d) => set.has(d)) };
    });
  }

  const preview = describeSchedule(
    { scheduleType, preset, parts, runAtLocal, timezone },
    schedLocale,
  );
  const compact = layout === "compact";

  const typeToggle = !scheduleTypeLocked ? (
    <div className="flex flex-wrap gap-2">
      <Button
        type="button"
        variant={scheduleType === "cron" ? "default" : "outline"}
        size={compact ? "default" : "sm"}
        onClick={() => setScheduleType("cron")}
      >
        {labels.scheduleType.cron}
      </Button>
      <Button
        type="button"
        variant={scheduleType === "once" ? "default" : "outline"}
        size={compact ? "default" : "sm"}
        onClick={() => setScheduleType("once")}
      >
        {labels.scheduleType.once}
      </Button>
    </div>
  ) : null;

  return (
    <div
      className={cn("flex flex-col gap-2", compact && "gap-3")}
      data-layout={layout}
      data-testid="schedule-input"
    >
      {label || typeToggle ? (
        <div
          className={cn(
            "flex flex-wrap items-center gap-3",
            !compact && "flex-col items-start gap-2",
          )}
        >
          {label ? <div className="text-sm font-medium">{label}</div> : null}
          {typeToggle}
        </div>
      ) : null}

      {scheduleType === "cron" && preset === "custom" ? (
        <div className="space-y-3" data-testid="schedule-fields">
          <div
            className={cn(
              "flex flex-col gap-2",
              compact &&
                "grid items-center gap-3 sm:grid-cols-[minmax(10rem,13rem)_minmax(11rem,15rem)_minmax(0,1fr)]",
            )}
          >
            <Select
              value={preset}
              onValueChange={(v) => changePreset(v as CronPreset)}
            >
              <SelectTrigger
                className={cn("w-full", compact && COMPACT_CONTROL_CLASS)}
                data-testid="schedule-preset"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PRESETS.map((p) => (
                  <SelectItem key={p} value={p}>
                    {labels.preset[p]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select value={timezone} onValueChange={setTimezone}>
              <SelectTrigger
                className={cn("w-full", compact && COMPACT_CONTROL_CLASS)}
                data-testid="schedule-timezone"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TIMEZONE_OPTIONS.map((tzOption) => (
                  <SelectItem key={tzOption} value={tzOption}>
                    {tzOption}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <div
              className={cn(
                "text-muted-foreground text-sm",
                compact &&
                  "bg-muted/45 flex h-11 min-h-11 items-center truncate rounded-md px-3",
              )}
              data-testid="schedule-preview"
            >
              {preview}
            </div>
          </div>

          <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-3">
            <Input
              className={cn(
                "font-mono",
                compact ? COMPACT_CONTROL_CLASS : undefined,
                "sm:min-w-0 sm:flex-1",
              )}
              value={parts.raw ?? ""}
              onChange={(e) => updateParts({ raw: e.target.value })}
              placeholder={labels.fields.cronPlaceholder}
              aria-label={labels.fields.cron}
            />
            <a
              href="https://crontab.guru/"
              target="_blank"
              rel="noreferrer"
              className="text-muted-foreground shrink-0 text-xs whitespace-nowrap hover:underline"
            >
              {labels.cronHelp} ↗
            </a>
          </div>
        </div>
      ) : (
        <div
          className={cn(
            "flex flex-col gap-2",
            compact &&
              "grid items-center gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,0.8fr)_minmax(0,1.15fr)_minmax(12rem,1.45fr)]",
          )}
          data-testid="schedule-fields"
        >
          {scheduleType === "cron" ? (
            <>
              <Select
                value={preset}
                onValueChange={(v) => changePreset(v as CronPreset)}
              >
                <SelectTrigger
                  className={cn("w-full", compact && COMPACT_CONTROL_CLASS)}
                  data-testid="schedule-preset"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PRESETS.map((p) => (
                    <SelectItem key={p} value={p}>
                      {labels.preset[p]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {preset === "hourly" && (
                <Input
                  className={cn(compact && COMPACT_CONTROL_CLASS)}
                  type="number"
                  min={0}
                  max={59}
                  value={parts.minute ?? 0}
                  onChange={(e) =>
                    updateParts({ minute: Number(e.target.value) })
                  }
                  aria-label={labels.fields.minute}
                />
              )}

              {(preset === "daily" ||
                preset === "weekly" ||
                preset === "monthly") && (
                <Input
                  className={cn(compact && COMPACT_CONTROL_CLASS)}
                  type="time"
                  value={`${pad2(parts.hour ?? 9)}:${pad2(parts.minute ?? 0)}`}
                  onChange={(e) => {
                    const [h, m] = e.target.value.split(":").map(Number);
                    updateParts({ hour: h, minute: m });
                  }}
                  aria-label={labels.fields.time}
                />
              )}

              {preset === "weekly" && (
                <div
                  className={cn(
                    "flex flex-wrap gap-1",
                    compact && "sm:col-span-2 lg:col-span-4",
                  )}
                >
                  <span className="text-muted-foreground w-full text-sm">
                    {labels.fields.weekday}
                  </span>
                  {WEEKDAYS.map((w) => {
                    const active = (parts.weekdays ?? []).includes(w);
                    return (
                      <Button
                        key={w}
                        type="button"
                        variant={active ? "default" : "outline"}
                        size="sm"
                        onClick={() => toggleWeekday(w)}
                        aria-pressed={active}
                      >
                        {labels.weekdays[w]}
                      </Button>
                    );
                  })}
                </div>
              )}

              {preset === "monthly" && (
                <Input
                  className={cn(compact && COMPACT_CONTROL_CLASS)}
                  type="number"
                  min={1}
                  max={31}
                  value={parts.dayOfMonth ?? 1}
                  onChange={(e) =>
                    updateParts({ dayOfMonth: Number(e.target.value) })
                  }
                  aria-label={labels.fields.dayOfMonth}
                />
              )}
            </>
          ) : (
            <Input
              className={cn(
                compact && COMPACT_CONTROL_CLASS,
                compact && "lg:col-span-2",
              )}
              type="datetime-local"
              value={runAtLocal}
              onChange={(e) => setRunAtLocal(e.target.value)}
              aria-label={labels.fields.runAt}
            />
          )}

          <Select value={timezone} onValueChange={setTimezone}>
            <SelectTrigger
              className={cn("w-full", compact && COMPACT_CONTROL_CLASS)}
              data-testid="schedule-timezone"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {TIMEZONE_OPTIONS.map((tzOption) => (
                <SelectItem key={tzOption} value={tzOption}>
                  {tzOption}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <div
            className={cn(
              "text-muted-foreground text-sm",
              compact &&
                "bg-muted/45 flex h-11 min-h-11 items-center rounded-md px-3 sm:col-span-2 lg:col-span-1",
            )}
            data-testid="schedule-preview"
          >
            {preview}
          </div>
        </div>
      )}
    </div>
  );
}

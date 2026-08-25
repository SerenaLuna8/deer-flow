"use client";

import { Clock3Icon } from "lucide-react";
import { useEffect, useState } from "react";

import { Shimmer } from "@/components/ai-elements/shimmer";
import { useI18n } from "@/core/i18n/hooks";
import { formatRunDuration } from "@/core/messages/run-duration";
import {
  runExecutionStateSchema,
  type RunExecutionState,
} from "@/core/threads/run-execution-state";

type RunExecutionActivityProps = {
  state: RunExecutionState | "unavailable";
};

type RunExecutionClock = Readonly<{
  observedAt: string;
  now: number;
}>;

function useServerAnchoredNow(observedAt: string): number {
  const observedAtMs = Date.parse(observedAt);
  const [clock, setClock] = useState<RunExecutionClock>(() => ({
    observedAt,
    now: observedAtMs,
  }));
  const now = clock.observedAt === observedAt ? clock.now : observedAtMs;

  useEffect(() => {
    const updateNow = () => {
      setClock({
        observedAt,
        now: Math.max(observedAtMs, Date.now()),
      });
    };
    updateNow();
    const interval = window.setInterval(updateNow, 1000);
    return () => window.clearInterval(interval);
  }, [observedAt, observedAtMs]);

  return now;
}

function elapsedFromServerTimestamp(
  timestamp: string | null,
  now: number,
): number | null {
  if (timestamp === null) return null;
  const startedAt = Date.parse(timestamp);
  if (!Number.isFinite(startedAt) || startedAt > now) return null;
  return Math.floor((now - startedAt) / 1000);
}

function UnavailableRunExecutionActivity() {
  const { t } = useI18n();
  return (
    <div
      className="text-muted-foreground flex items-center gap-2 text-sm"
      data-state="unavailable"
      data-testid="run-execution-activity"
    >
      <Clock3Icon className="size-4" />
      <span>{t.runExecutionState.unavailable}</span>
    </div>
  );
}

function ActiveRunExecutionActivity({ state }: { state: RunExecutionState }) {
  const { t } = useI18n();
  const now = useServerAnchoredNow(state.observed_at);
  const totalElapsed = elapsedFromServerTimestamp(
    state.execution_started_at,
    now,
  );
  const phaseElapsed = elapsedFromServerTimestamp(state.phase_started_at, now);
  const totalDuration =
    totalElapsed === null
      ? null
      : formatRunDuration(totalElapsed, t.runDuration);
  const phaseDuration =
    phaseElapsed === null
      ? null
      : formatRunDuration(phaseElapsed, t.runDuration);

  if (state.phase === "terminal") return null;

  return (
    <div
      aria-live="polite"
      className="text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-sm"
      data-phase={state.phase}
      data-testid="run-execution-activity"
    >
      <Clock3Icon className="size-4" />
      <span data-testid="run-execution-phase-shimmer">
        <Shimmer as="span" duration={1}>
          {t.runExecutionState.phases[state.phase]}
        </Shimmer>
      </span>
      {totalDuration && (
        <span data-testid="run-execution-total-duration">
          {t.runExecutionState.totalDuration(totalDuration)}
        </span>
      )}
      {phaseDuration && (
        <span data-testid="run-execution-phase-duration">
          {t.runExecutionState.phaseDuration(phaseDuration)}
        </span>
      )}
    </div>
  );
}

export function RunExecutionActivity({ state }: RunExecutionActivityProps) {
  if (state === "unavailable") {
    return <UnavailableRunExecutionActivity />;
  }
  const parsed = runExecutionStateSchema.safeParse(state);
  if (!parsed.success) {
    return <UnavailableRunExecutionActivity />;
  }
  if (parsed.data.phase === "terminal") return null;
  return <ActiveRunExecutionActivity state={parsed.data} />;
}

export function RunActivity({ startTime }: { startTime: number | null }) {
  const { t } = useI18n();
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (startTime === null) {
      setElapsed(0);
      return;
    }

    const updateElapsed = () => {
      setElapsed(Math.max(0, Math.floor((Date.now() - startTime) / 1000)));
    };
    updateElapsed();
    const interval = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(interval);
  }, [startTime]);

  const formatted = formatRunDuration(elapsed, t.runDuration);

  return (
    <div
      className="text-muted-foreground flex items-center gap-2 text-sm"
      data-testid="run-activity"
    >
      <Clock3Icon className="size-4" />
      <Shimmer duration={1}>{t.runDuration.working}</Shimmer>
      {formatted && <span aria-hidden="true">({formatted})</span>}
    </div>
  );
}

export function RunDuration({ durationSeconds }: { durationSeconds: number }) {
  const { t } = useI18n();
  const formatted = formatRunDuration(durationSeconds, t.runDuration);
  if (!formatted) {
    return null;
  }

  return (
    <div
      className="inline-flex items-center gap-1"
      data-testid="run-duration"
      title={t.runDuration.description}
    >
      <Clock3Icon className="size-3" />
      <span>{t.runDuration.completedIn(formatted)}</span>
    </div>
  );
}

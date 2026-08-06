export type ProjectAutomationSchedule = {
  schedule_type: "once" | "cron";
  schedule_spec: { cron?: string; run_at?: string };
  timezone: string;
};

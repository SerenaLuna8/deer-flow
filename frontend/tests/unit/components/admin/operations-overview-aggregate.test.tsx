import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { OperationsOverviewStateView } from "@/components/admin/operations/operations-overview";
import { operationsOverviewSchema } from "@/core/admin-operations/types";
import { I18nProvider } from "@/core/i18n/context";

const overview = operationsOverviewSchema.parse({
  readiness: {
    status: "ready",
    database: "ready",
    schema: "ready",
    schema_state: "ready",
    worker_fleet: "ready",
    scheduler: "disabled",
    stream: "ready",
    quota: "ready",
    audit: "ready",
    role: "gateway",
    worker_count: 4,
    worker_capacity: 12,
    worker_oldest_heartbeat_age_seconds: 8,
    private_run_worker_fleet: "ready",
    private_run_worker_count: 2,
    private_run_worker_capacity: 7,
    scheduler_ownership: "disabled",
    run_skill_writer_mode: "legacy_v3",
    run_skill_writer_artifact_version: "run-skill-snapshot-writer-v2",
    run_skill_legacy_policy_digest:
      "e01a816a3f20a4ecf088e2f0d37b92ba16634e5969860b900a14924312edb6e8",
    run_skill_writer_ready: true,
  },
  data_status: "available",
  counts: {
    projects: 3,
    suspended_projects: 1,
    queued_jobs: 5,
    running_jobs: 2,
    dead_jobs: 1,
    ready_jobs: 4,
    oldest_ready_job_age_seconds: 17,
    stale_leases: 2,
    waiting_for_worker_runs: 3,
    waiting_for_terminalization_runs: 1,
  },
  usage: [
    { dimension: "members", used: 1, reserved: 0 },
    { dimension: "storage_bytes", used: 2, reserved: 0 },
    { dimension: "concurrent_runs", used: 3, reserved: 0 },
    { dimension: "mcp_calls_daily", used: 4, reserved: 0 },
  ],
  channel_providers: [],
});

describe("OperationsOverviewStateView aggregate projection", () => {
  test("renders private-run capacity and queue convergence signals", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <OperationsOverviewStateView
          state={{ status: "ready", data: overview }}
        />
      </I18nProvider>,
    );

    for (const text of [
      "Private-run Worker fleet",
      "Private-run Worker processes",
      "Private-run Worker capacity",
      "Run Skill writer",
      "Run Skill writer mode",
      "Legacy v3 rollback",
      "Run Skill writer artifact",
      "run-skill-snapshot-writer-v2",
      "Legacy policy digest",
      "e01a816a3f20a4ecf088e2f0d37b92ba16634e5969860b900a14924312edb6e8",
      "Ready jobs",
      "Oldest ready job age (seconds)",
      "Stale leases",
      "Runs waiting for a Worker",
      "Runs waiting for terminalization",
    ]) {
      expect(html).toContain(text);
    }
  });
});

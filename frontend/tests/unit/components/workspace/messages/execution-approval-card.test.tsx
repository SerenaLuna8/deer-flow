import { describe, expect, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ExecutionApprovalCard } from "@/components/workspace/messages/execution-approval-card";
import type { ExecutionApprovalProjection } from "@/core/execution-approvals/schemas";
import { I18nContext } from "@/core/i18n/context";

const common = {
  approval_id: "11111111-1111-4111-8111-111111111111",
  source_run_id: "run-1",
  source_tool_call_id: "call-1",
  version: "1",
  execution_domain: {
    label: "Jiangfeng Mac",
    effective_user_label: "jiangfeng",
  },
  command_preview: "python <untrusted.py> --value **literal**\necho done",
  cwd_preview: "/mnt/user-data/workspace",
  timeout_seconds: 60,
  source_agent: {
    kind: "subagent" as const,
    label: "Bash subagent",
    path: ["Project Assistant", "Bash subagent"],
  },
  risk_level: "host_execution" as const,
  warning_code: "LOCAL_PROCESS_RUNS_ON_HOST" as const,
  continuation_run: null,
};

function approvalFor(
  status: ExecutionApprovalProjection["status"],
): ExecutionApprovalProjection {
  switch (status) {
    case "pending":
      return {
        ...common,
        status,
        can_decide: true,
        decision_expires_at: "2026-08-14T16:20:00Z",
        remaining_ttl_seconds: 300,
      };
    case "approved":
      return {
        ...common,
        status,
        can_decide: false,
        decision_at: "2026-08-14T16:16:00Z",
        claim_expires_at: "2026-08-14T16:17:00Z",
        continuation_run: { run_id: "run-2", status: "pending" },
      };
    case "claimed":
      return {
        ...common,
        status,
        can_decide: false,
        claimed_at: "2026-08-14T16:16:05Z",
        continuation_run: { run_id: "run-2", status: "running" },
      };
    case "finished":
      return {
        ...common,
        status,
        can_decide: false,
        finished_at: "2026-08-14T16:16:10Z",
        exit_code: 0,
        result_summary_code: "PROCESS_EXITED",
      };
    case "launch_failed":
      return {
        ...common,
        status,
        can_decide: false,
        finished_at: "2026-08-14T16:16:10Z",
        reason_code: "PROCESS_NOT_CREATED",
      };
    case "unknown":
      return {
        ...common,
        status,
        can_decide: false,
        finished_at: "2026-08-14T16:16:10Z",
        warning_code: "HOST_EXECUTION_STATE_UNKNOWN",
      };
    case "denied":
      return {
        ...common,
        status,
        can_decide: false,
        decision_at: "2026-08-14T16:16:00Z",
        denial_delivery_status: "delivered",
      };
    case "expired":
    case "cancelled":
      return {
        ...common,
        status,
        can_decide: false,
        finished_at: "2026-08-14T16:20:00Z",
        reason_code:
          status === "expired" ? "DECISION_TTL_EXPIRED" : "THREAD_CANCELLED",
      };
  }
}

function renderCard(
  approval: ExecutionApprovalProjection,
  extra: Partial<Parameters<typeof ExecutionApprovalCard>[0]> = {},
  locale: "en-US" | "zh-CN" = "en-US",
) {
  return renderToStaticMarkup(
    createElement(
      I18nContext.Provider,
      { value: { locale, setLocale: () => undefined } },
      createElement(ExecutionApprovalCard, {
        approval,
        onDecision: () => undefined,
        ...extra,
      }),
    ),
  );
}

describe("ExecutionApprovalCard", () => {
  test("renders the complete command as escaped plain text with host risk", () => {
    const html = renderCard(approvalFor("pending"));

    expect(html).toContain('data-execution-approval-state="pending"');
    expect(html).toContain(
      "Request to run a command in the ActWeave Worker host environment",
    );
    expect(html).toContain("Jiangfeng Mac");
    expect(html).toContain("Bash subagent");
    expect(html).toContain("/mnt/user-data/workspace");
    expect(html).toContain("python &lt;untrusted.py&gt; --value **literal**");
    expect(html).toContain("echo done");
    expect(html).not.toContain("<untrusted.py>");
    expect(html).toContain("This is not an isolated sandbox");
    expect(html).toContain("Waiting for approval");
    expect(html).toContain("Allow once");
    expect(html).toContain("Deny");
  });

  test("matches the compact Chinese approval decision copy", () => {
    const html = renderCard(approvalFor("pending"), {}, "zh-CN");

    expect(html).toContain("等待审批");
    expect(html).toContain("允许一次");
    expect(html).toMatch(/>拒绝<\/button>/u);
    expect(html.match(/min-h-11/gu)?.length).toBe(2);
    expect(html).not.toContain("仅允许本次命令");
  });

  test.each([
    "approved",
    "claimed",
    "finished",
    "launch_failed",
    "unknown",
    "denied",
    "expired",
    "cancelled",
  ] as const)("renders %s as a read-only state", (status) => {
    const html = renderCard(approvalFor(status));

    expect(html).toContain(`data-execution-approval-state="${status}"`);
    expect(html).not.toContain("Allow once");
    expect(html).not.toMatch(/>Deny<\/button>/u);
  });

  test("shows deterministic terminal details and the unknown warning", () => {
    expect(renderCard(approvalFor("finished"))).toContain("Exit code: 0");
    expect(renderCard(approvalFor("launch_failed"))).toContain(
      "PROCESS_NOT_CREATED",
    );
    expect(renderCard(approvalFor("unknown"))).toContain(
      "child processes may still be running",
    );
  });

  test("disables both decisions while one imperative request is pending", () => {
    const html = renderCard(approvalFor("pending"), {
      pendingDecision: "allow_once",
    });

    expect(html.match(/disabled=""/gu)?.length).toBe(2);
    expect(html).toContain("Allowing…");
  });

  test("keeps a pending approval read-only after decision capability is revoked", () => {
    const html = renderCard({
      ...approvalFor("pending"),
      can_decide: false,
    });

    expect(html).toContain('data-execution-approval-state="pending"');
    expect(html).not.toContain("Allow once");
    expect(html).not.toMatch(/>Deny<\/button>/u);
  });
});

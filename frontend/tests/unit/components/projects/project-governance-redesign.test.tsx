import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectAuditStateView } from "@/components/projects/governance/project-audit-page";
import {
  describeAuditItem,
  type AuditItemPresentation,
} from "@/components/projects/governance/project-audit-view-model";
import { ProjectUsageStateView } from "@/components/projects/governance/project-usage-page";
import {
  describeUsageDimension,
  formatUsageValue,
} from "@/components/projects/governance/project-usage-view-model";
import { I18nProvider } from "@/core/i18n/context";
import type { ProjectAuditPage } from "@/core/project-governance/audit";
import type { ProjectUsage } from "@/core/project-governance/usage";

const usage: ProjectUsage = {
  policy: {
    version: 4,
    configured: {
      member_limit: 12,
      storage_bytes_limit: null,
      concurrent_run_limit: 3,
      mcp_calls_daily_limit: null,
    },
    effective: {
      member_limit: 12,
      storage_bytes_limit: 5_368_709_120,
      concurrent_run_limit: 3,
      mcp_calls_daily_limit: 10_000,
    },
  },
  dimensions: [
    {
      dimension: "members",
      bucket: "lifetime",
      used: 9,
      reserved: 1,
      limit: 12,
      warning_threshold_reached: true,
    },
    {
      dimension: "storage_bytes",
      bucket: "lifetime",
      used: 1_073_741_824,
      reserved: 536_870_912,
      limit: 5_368_709_120,
      warning_threshold_reached: false,
    },
    {
      dimension: "concurrent_runs",
      bucket: "lifetime",
      used: 0,
      reserved: 2,
      limit: 3,
      warning_threshold_reached: false,
    },
    {
      dimension: "mcp_calls_daily",
      bucket: "2026-07-21",
      used: 1240,
      reserved: 0,
      limit: 10_000,
      warning_threshold_reached: false,
    },
  ],
};

const auditPage: ProjectAuditPage = {
  items: [
    {
      id: "33333333-3333-4333-8333-333333333333",
      occurred_at: "2026-07-21T10:00:00Z",
      actor: "user",
      action: "quota.policy_updated",
      target_kind: "quota",
      outcome: "success",
      public_error_code: null,
      metadata: {
        member_limit: 10,
        storage_bytes_limit: null,
        concurrent_run_limit: 2,
        mcp_calls_daily_limit: null,
        version: 5,
      },
    },
    {
      id: "44444444-4444-4444-8444-444444444444",
      occurred_at: "2026-07-21T09:00:00Z",
      actor: "worker",
      action: "run.terminal",
      target_kind: "run",
      outcome: "failed",
      public_error_code: "MODEL_UNAVAILABLE",
      metadata: {
        job_type: "private_run",
        status: "failed",
        public_error_code: "MODEL_UNAVAILABLE",
      },
    },
  ],
  next_cursor: null,
};

function renderWithLocale(
  children: React.ReactNode,
  locale: "en-US" | "zh-CN",
) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>{children}</I18nProvider>,
  );
}

describe("project governance redesign", () => {
  test("formats storage and quota counters without changing the four-dimension contract", () => {
    expect(formatUsageValue("storage_bytes", 1024, "zh-CN")).toBe("1 KiB");
    expect(formatUsageValue("storage_bytes", 5_368_709_120, "zh-CN")).toBe(
      "5 GiB",
    );
    expect(formatUsageValue("mcp_calls_daily", 10_000, "zh-CN")).toBe("10,000");

    expect(describeUsageDimension(usage, "concurrent_runs", "zh-CN")).toEqual(
      expect.objectContaining({
        used: "0",
        reserved: "2",
        consumed: "2",
        limit: "3",
        effectiveLimit: "3",
        configuredLimit: "3",
        inheritsPlatformLimit: false,
        progressValue: expect.closeTo(66.7, 1),
        progressText: "66.7%",
      }),
    );
    expect(describeUsageDimension(usage, "storage_bytes", "zh-CN")).toEqual(
      expect.objectContaining({
        used: "1 GiB",
        reserved: "512 MiB",
        consumed: "1.5 GiB",
        limit: "5 GiB",
        effectiveLimit: "5 GiB",
        configuredLimit: null,
        inheritsPlatformLimit: true,
        progressText: "30%",
      }),
    );
  });

  test("renders effective limits, inherited policy and accessible progress", () => {
    const html = renderWithLocale(
      <ProjectUsageStateView state={{ status: "ready", data: usage }} />,
      "zh-CN",
    );

    expect(html).toContain("当前占用");
    expect(html).toContain("继承平台上限");
    expect(html).toContain("项目自定义上限");
    expect(html).toContain("1.5 GiB");
    expect(html).toContain('role="progressbar"');
    expect(html).toContain('aria-valuenow="30"');
    expect(html).not.toMatch(/账单|模型|费用|Token/u);
  });

  test("turns strict audit fields into localized, human-readable descriptions", () => {
    const description: AuditItemPresentation = describeAuditItem(
      auditPage.items[0]!,
      "zh-CN",
    );
    expect(description).toEqual(
      expect.objectContaining({
        action: "已更新项目配额",
        actor: "用户",
        target: "配额",
        outcome: "成功",
      }),
    );
    expect(description.metadata).toEqual(
      expect.arrayContaining([
        { label: "成员上限", value: "10" },
        { label: "存储上限", value: "继承平台上限" },
        { label: "策略版本", value: "5" },
      ]),
    );
    expect(description.metadata.map((item) => item.label)).not.toContain(
      "member_limit",
    );

    const deleted = describeAuditItem(
      {
        id: "55555555-5555-4555-8555-555555555555",
        occurred_at: "2026-07-25T15:00:00Z",
        actor: "user",
        action: "asset.deleted",
        target_kind: "asset",
        outcome: "success",
        public_error_code: null,
        metadata: { asset_kind: "skill" },
      },
      "zh-CN",
    );
    expect(deleted).toEqual(
      expect.objectContaining({
        action: "已删除资产",
        target: "资产",
        metadata: [{ label: "资产类型", value: "Skill" }],
      }),
    );

    const credentialDeleted = describeAuditItem(
      {
        id: "66666666-6666-4666-8666-666666666666",
        occurred_at: "2026-07-27T10:00:00Z",
        actor: "user",
        action: "asset.credential_deleted",
        target_kind: "asset",
        outcome: "success",
        public_error_code: null,
        metadata: { asset_kind: "mcp" },
      },
      "zh-CN",
    );
    expect(credentialDeleted).toEqual(
      expect.objectContaining({
        action: "已删除资产凭据",
        target: "资产",
        metadata: [{ label: "资产类型", value: "MCP" }],
      }),
    );
  });

  test("renders localized audit events without inventing actor or target names", () => {
    const html = renderWithLocale(
      <ProjectAuditStateView state={{ status: "ready", data: auditPage }} />,
      "zh-CN",
    );

    expect(html).toContain("已更新项目配额");
    expect(html).toContain("用户");
    expect(html).toContain("执行失败");
    expect(html).toContain("MODEL_UNAVAILABLE");
    expect(html).not.toContain("member_limit");
    expect(html).not.toMatch(/张三|管理员姓名|目标名称/u);
  });
});

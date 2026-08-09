import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectUsageStateView } from "@/components/projects/governance/project-usage-page";
import { I18nProvider } from "@/core/i18n/context";
import type { ProjectUsage } from "@/core/project-governance/usage";

const usage: ProjectUsage = {
  policy: {
    version: 3,
    configured: {
      member_limit: null,
      storage_bytes_limit: null,
      concurrent_run_limit: null,
      mcp_calls_daily_limit: null,
    },
    effective: {
      member_limit: 20,
      storage_bytes_limit: 5_368_709_120,
      concurrent_run_limit: 3,
      mcp_calls_daily_limit: 10_000,
    },
  },
  dimensions: [
    {
      dimension: "members",
      bucket: "lifetime",
      used: 0,
      reserved: 1,
      limit: 20,
      warning_threshold_reached: false,
    },
    {
      dimension: "storage_bytes",
      bucket: "lifetime",
      used: 0,
      reserved: 0,
      limit: 5_368_709_120,
      warning_threshold_reached: false,
    },
    {
      dimension: "concurrent_runs",
      bucket: "lifetime",
      used: 0,
      reserved: 0,
      limit: 3,
      warning_threshold_reached: false,
    },
    {
      dimension: "mcp_calls_daily",
      bucket: "2026-08-07",
      used: 0,
      reserved: 0,
      limit: 10_000,
      warning_threshold_reached: false,
    },
  ],
};

describe("ProjectUsageStateView", () => {
  test("renders the four occupancy dimension cards", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <ProjectUsageStateView state={{ status: "ready", data: usage }} />
      </I18nProvider>,
    );

    expect(html).toContain("成员");
    expect(html).toContain("存储字节数");
    expect(html).toContain("并发运行数");
    expect(html).toContain("每日 MCP 调用数");
    expect(html).toContain("当前有效上限");
  });
});

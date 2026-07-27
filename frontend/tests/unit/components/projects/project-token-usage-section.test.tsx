import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectTokenUsageStateView } from "@/components/projects/project-token-usage-section";
import { buildTokenUsageChartModel } from "@/components/projects/project-token-usage-view-model";
import { I18nProvider } from "@/core/i18n/context";
import type { ProjectTokenUsageSeries } from "@/core/project-governance/usage";

const START_TIME = Date.parse("2026-07-26T13:00:00.000Z");

function makeTokenUsageSeries(zero = false): ProjectTokenUsageSeries {
  const points = Array.from({ length: 24 }, (_, index) => ({
    bucket_start: new Date(START_TIME + index * 60 * 60 * 1000).toISOString(),
    input_tokens: zero ? 0 : index === 22 ? 120 : index === 23 ? 80 : 0,
    output_tokens: zero ? 0 : index === 22 ? 30 : index === 23 ? 20 : 0,
    total_tokens: zero ? 0 : index === 22 ? 170 : index === 23 ? 130 : 0,
  }));
  return {
    window_start: points[0]!.bucket_start,
    window_end: new Date(START_TIME + (23 * 60 + 30) * 60 * 1000).toISOString(),
    bucket_minutes: 60,
    totals: {
      input_tokens: zero ? 0 : 200,
      output_tokens: zero ? 0 : 50,
      total_tokens: zero ? 0 : 300,
    },
    points,
  };
}

function collectPathStrings(value: unknown): string[] {
  if (typeof value !== "object" || value === null) return [];
  if (Array.isArray(value)) return value.flatMap(collectPathStrings);
  return Object.entries(value).flatMap(([key, child]) => {
    if (
      typeof child === "string" &&
      key.toLocaleLowerCase("en-US").includes("path")
    ) {
      return [child];
    }
    return collectPathStrings(child);
  });
}

function render(children: React.ReactNode): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">{children}</I18nProvider>,
  );
}

describe("project overview token usage", () => {
  test("builds finite SVG geometry for both used and zero-token windows", () => {
    for (const series of [makeTokenUsageSeries(), makeTokenUsageSeries(true)]) {
      const model = buildTokenUsageChartModel(series, "zh-CN");
      const serialized = JSON.stringify(model);
      const paths = collectPathStrings(model);

      expect(model.points).toHaveLength(24);
      expect(model.xTicks).toHaveLength(5);
      expect(model.yTicks.length).toBeGreaterThan(1);
      expect(paths.length).toBeGreaterThan(0);
      expect(paths.every((path) => /^[ML\d.,\s-]+$/u.test(path))).toBe(true);
      expect(serialized).not.toMatch(/NaN|Infinity/u);
    }
  });

  test("renders loading, error, zero, and nonzero states accessibly", () => {
    const loading = render(
      <ProjectTokenUsageStateView state={{ status: "loading" }} />,
    );
    expect(loading).toContain('aria-busy="true"');
    expect(loading).toContain("Token");

    const error = render(
      <ProjectTokenUsageStateView
        state={{ status: "error" }}
        onRetry={() => undefined}
      />,
    );
    expect(error).toContain('role="alert"');
    expect(error).toContain("重试");

    const zero = render(
      <ProjectTokenUsageStateView
        state={{ status: "ready", data: makeTokenUsageSeries(true) }}
      />,
    );
    expect(zero).toMatch(/最近 24 个?小时/u);
    expect(zero).toMatch(/暂无|没有/u);
    expect(zero).not.toMatch(/NaN|Infinity/u);

    const ready = render(
      <ProjectTokenUsageStateView
        state={{ status: "ready", data: makeTokenUsageSeries() }}
      />,
    );
    expect(ready).toContain("Token 使用量");
    expect(ready).toMatch(/最近 24 个?小时/u);
    expect(ready).toContain("300");
    expect(ready).toContain("输入");
    expect(ready).toContain("输出");
    expect(ready).toContain("<svg");
    expect(ready).toContain('role="img"');
    expect(ready).toContain("<title");
    expect(ready).toContain("<desc");
    expect(ready).toContain('<div class="sr-only"><table>');
    expect(ready).not.toContain('<table class="sr-only">');
    expect(ready).not.toMatch(/NaN|Infinity|owner_user_id|run_id/u);

    const dataPoints =
      ready.match(
        /<[a-z][\w:-]*\b[^>]*data-token-usage-index="[^"]+"[^>]*>/gu,
      ) ?? [];
    expect(dataPoints).toHaveLength(24);
    expect(
      dataPoints.every(
        (point) =>
          (point.startsWith("<button") || point.includes('tabindex="0"')) &&
          /aria-label="[^"]+"/u.test(point),
      ),
    ).toBe(true);
    expect(
      dataPoints.map((point) =>
        Number(
          /data-token-usage-index="(\d+)"/u.exec(point)?.[1] ?? Number.NaN,
        ),
      ),
    ).toEqual(Array.from({ length: 24 }, (_, index) => index));
  });
});

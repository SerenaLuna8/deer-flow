import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { I18nProvider } from "@/core/i18n/context";

function render(status: Parameters<typeof AssetStatusBadge>[0]["status"]) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <AssetStatusBadge status={status} />
    </I18nProvider>,
  );
}

describe("AssetStatusBadge", () => {
  test("uses semantic catalog tones instead of a black primary pill", () => {
    expect(render("active")).toContain("border-success/25");
    expect(render("published")).toContain("bg-success/10");
    expect(render("suspended")).toContain("border-chart-4/30");
    expect(render("rejected")).toContain("border-destructive/25");
    expect(render("archived")).toContain("bg-muted");
  });
});

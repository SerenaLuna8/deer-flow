import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  formatPrivacyDeadline,
  PrivacyCaseCard,
  privacyRemainingDays,
} from "@/components/projects/privacy-center-page";
import type { PrivacyCase } from "@/core/privacy-center/types";

const privacyCase: PrivacyCase = {
  project_id: "22222222-2222-4222-8222-222222222222",
  project_slug: "former-project",
  project_display_name: "Former project",
  project_icon: "folder",
  membership_status: "left",
  retention_kind: "former_owner",
  deletion_deadline: "2026-08-21T08:00:00Z",
  early_delete_requested: false,
};

describe("privacy center page", () => {
  test("localizes deletion deadline and derives a bounded remaining-day label", () => {
    const localized = formatPrivacyDeadline(
      privacyCase.deletion_deadline,
      "zh-CN",
    );

    expect(localized).not.toBe(privacyCase.deletion_deadline);
    expect(localized).toContain("2026");
    expect(
      privacyRemainingDays(
        privacyCase.deletion_deadline,
        new Date("2026-07-22T08:00:00Z"),
      ),
    ).toBe(30);
  });

  test("presents export before irreversible early delete", () => {
    const html = renderToStaticMarkup(
      <PrivacyCaseCard
        privacyCase={privacyCase}
        exporting={false}
        now={new Date("2026-07-22T08:00:00Z")}
        onExport={() => undefined}
        onEarlyDelete={() => undefined}
      />,
    );

    expect(html).toContain("Former project");
    expect(html).toContain("导出我的数据");
    expect(html).toContain("提前删除");
    expect(html).toContain("30 天");
  });

  test("disables duplicate early-delete requests once a durable job exists", () => {
    const html = renderToStaticMarkup(
      <PrivacyCaseCard
        privacyCase={{ ...privacyCase, early_delete_requested: true }}
        exporting={false}
        now={new Date("2026-07-22T08:00:00Z")}
        onExport={() => undefined}
        onEarlyDelete={() => undefined}
      />,
    );

    expect(html).toContain("删除请求已提交");
    expect(html).toContain("disabled");
  });
});

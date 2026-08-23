import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { WorkspaceChangeBadge } from "@/components/workspace/changes/workspace-change-badge";
import { I18nProvider } from "@/core/i18n/context";

rs.mock("@/core/workspace-changes/hooks", () => ({
  useWorkspaceChanges: () => ({
    isLoading: false,
    data: {
      available: true,
      version: 2,
      summary: {
        created: 1,
        modified: 0,
        deleted: 0,
        additions: null,
        deletions: null,
        truncated: false,
      },
      files: [
        {
          path: "/mnt/user-data/outputs/report.bin",
          root: "outputs",
          status: "created",
          binary: true,
          sensitive: false,
          size_before: null,
          size_after: 10,
          sha256_before: null,
          sha256_after: "a".repeat(64),
          diff: "",
          diff_truncated: false,
          diff_unavailable_reason: "binary",
          additions: null,
          deletions: null,
        },
      ],
      limits: {},
    },
  }),
}));

describe("WorkspaceChangeBadge", () => {
  test("shows the file name without its workspace path", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <WorkspaceChangeBadge threadId="thread-1" runId="run-1" />
      </I18nProvider>,
    );

    expect(html).toContain("report.bin");
    expect(html).not.toContain("outputs/");
    expect(html).not.toContain("/mnt/user-data/");
  });

  test("shows unknown line counts instead of fake +0 -0", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <WorkspaceChangeBadge threadId="thread-1" runId="run-1" />
      </I18nProvider>,
    );

    expect(html).not.toContain(">+0<");
    expect(html).not.toContain(">-0<");
    expect(html).toContain("—");
  });
});

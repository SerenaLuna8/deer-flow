import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

describe("platform administration Chinese copy", () => {
  test("does not mix ordinary English implementation terms into Chinese UI copy", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/core/i18n/locales/zh-CN.ts"),
      "utf8",
    );
    const adminCopy = source.slice(
      source.indexOf("adminOperations:"),
      source.indexOf("automation:"),
    );

    for (const implementationTerm of [
      "packaged catalog",
      "Private runtime",
      "Project MCP",
      "Worker 访问",
      "Credential 元数据",
      "Credential 槽位",
      "Credential 授权",
      "active Grant",
    ]) {
      expect(adminCopy).not.toContain(implementationTerm);
    }
  });

  test("routes admin overlay close labels through i18n", () => {
    const dialogSource = readFileSync(
      resolve(process.cwd(), "src/components/ui/dialog.tsx"),
      "utf8",
    );
    const sheetSource = readFileSync(
      resolve(process.cwd(), "src/components/ui/sheet.tsx"),
      "utf8",
    );
    const assetSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-asset-page.tsx",
      ),
      "utf8",
    );

    expect(dialogSource).toContain('<span className="sr-only">{closeLabel}');
    expect(sheetSource).toContain('<span className="sr-only">{closeLabel}');
    expect(assetSource).toContain("closeLabel={t.adminOperations.ui.close}");
  });

  test("routes desktop navigation controls through formal locale keys", () => {
    const shellSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/operations/admin-operations-shell.tsx",
      ),
      "utf8",
    );

    expect(shellSource).toContain("localLabels.expandNavigation");
    expect(shellSource).toContain("localLabels.collapseNavigation");
    expect(shellSource).not.toContain('locale === "zh-CN"');
  });
});

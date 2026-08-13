import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { CredentialWriteNotice } from "@/components/admin/assets/admin-asset-page";
import { credentialPendingMigrationMessage } from "@/components/admin/assets/admin-asset-view-model";
import { zhCN } from "@/core/i18n/locales/zh-CN";

const zh = zhCN.adminAssets.common;

describe("credential pending migration notice", () => {
  test("stays silent when nothing is pending", () => {
    expect(credentialPendingMigrationMessage(null, zh)).toBeNull();
    expect(
      credentialPendingMigrationMessage(
        { total: 0, system_model_count: 0 },
        zh,
      ),
    ).toBeNull();
  });

  test("renders the server-reported counts as an actionable status", () => {
    const markup = renderToStaticMarkup(
      <CredentialWriteNotice
        message={credentialPendingMigrationMessage(
          { total: 3, system_model_count: 1 },
          zh,
        )}
        action={{ label: zh.migrateReferences, onClick: () => undefined }}
      />,
    );

    expect(markup).toContain('role="status"');
    expect(markup).toContain(zh.migrateReferences);
    expect(markup).toContain("3");
    expect(markup).toContain("1 个系统模型");
  });
});

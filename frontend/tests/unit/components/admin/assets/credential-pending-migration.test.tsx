import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { CredentialWriteNotice } from "@/components/admin/assets/admin-asset-page";
import { credentialPendingMigrationMessage } from "@/components/admin/assets/admin-asset-view-model";
import { enUS } from "@/core/i18n/locales/en-US";
import { zhCN } from "@/core/i18n/locales/zh-CN";

const zh = zhCN.adminAssets.common;
const en = enUS.adminAssets.common;

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

  test("names the total and keeps the system model share identifiable", () => {
    const message = credentialPendingMigrationMessage(
      { total: 3, system_model_count: 1 },
      zh,
    );

    expect(message).toContain("3");
    expect(message).toContain("1 个系统模型");
    expect(
      credentialPendingMigrationMessage(
        { total: 3, system_model_count: 1 },
        en,
      ),
    ).toContain("1 system model");
  });

  test("omits the model clause when only grants and bindings are behind", () => {
    const message = credentialPendingMigrationMessage(
      { total: 2, system_model_count: 0 },
      zh,
    );

    expect(message).toContain("2");
    expect(message).not.toContain("系统模型");
  });

  test("renders an actionable status region carrying the migrate action", () => {
    const markup = renderToStaticMarkup(
      <CredentialWriteNotice
        message={credentialPendingMigrationMessage(
          { total: 2, system_model_count: 2 },
          zh,
        )}
        action={{ label: zh.migrateReferences, onClick: () => undefined }}
      />,
    );

    expect(markup).toContain('role="status"');
    expect(markup).toContain(zh.migrateReferences);
    expect(markup).toContain("2 个系统模型");
  });

  test("renders nothing at all without a message", () => {
    expect(
      renderToStaticMarkup(
        <CredentialWriteNotice
          message={null}
          action={{ label: zh.migrateReferences, onClick: () => undefined }}
        />,
      ),
    ).toBe("");
  });
});

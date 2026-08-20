import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { CredentialMigrationReferenceList } from "@/components/admin/assets/admin-asset-dialogs";
import { CredentialWriteNotice } from "@/components/admin/assets/admin-asset-page";
import {
  credentialMigrationActionVisible,
  credentialMigrationCompleteMessage,
  credentialPendingMigrationMessage,
} from "@/components/admin/assets/admin-asset-view-model";
import { I18nProvider } from "@/core/i18n/context";
import { zhCN } from "@/core/i18n/locales/zh-CN";

const zh = zhCN.adminAssets.common;

describe("credential pending migration notice", () => {
  test("stays silent when nothing is pending", () => {
    expect(credentialPendingMigrationMessage(null, zh)).toBeNull();
    expect(
      credentialPendingMigrationMessage(
        {
          total: 0,
          mcp_grant_count: 0,
          skill_binding_count: 0,
          system_model_count: 0,
          references: [],
          current_reference_count: 0,
          current_references: [],
        },
        zh,
      ),
    ).toBeNull();
    expect(
      credentialMigrationCompleteMessage(
        {
          total: 0,
          mcp_grant_count: 0,
          skill_binding_count: 0,
          system_model_count: 0,
          references: [],
          current_reference_count: 0,
          current_references: [],
        },
        zh,
      ),
    ).toBe(zh.credentialMigrationComplete);
    expect(
      credentialMigrationActionVisible({
        total: 0,
        mcp_grant_count: 0,
        skill_binding_count: 0,
        system_model_count: 0,
        references: [],
        current_reference_count: 0,
        current_references: [],
      }),
    ).toBe(false);
  });

  test("renders the server-reported counts as an actionable status", () => {
    const markup = renderToStaticMarkup(
      <CredentialWriteNotice
        message={credentialPendingMigrationMessage(
          {
            total: 3,
            mcp_grant_count: 1,
            skill_binding_count: 1,
            system_model_count: 1,
            references: [],
            current_reference_count: 0,
            current_references: [],
          },
          zh,
        )}
        action={{ label: zh.migrateReferences, onClick: () => undefined }}
      />,
    );

    expect(markup).toContain('role="status"');
    expect(markup).toContain(zh.migrateReferences);
    expect(markup).toContain("3");
    expect(markup).toContain("1 个 MCP 授权");
    expect(markup).toContain("1 个 Skill 环境变量绑定");
    expect(markup).toContain("1 个系统模型");
    expect(
      credentialMigrationActionVisible({
        total: 3,
        mcp_grant_count: 1,
        skill_binding_count: 1,
        system_model_count: 1,
        references: [],
        current_reference_count: 0,
        current_references: [],
      }),
    ).toBe(true);
  });

  test("shows the concrete consumers without exposing Credential values", () => {
    const markup = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <CredentialMigrationReferenceList
          pendingMigration={{
            total: 3,
            mcp_grant_count: 1,
            skill_binding_count: 1,
            system_model_count: 1,
            references: [
              {
                kind: "skill_binding",
                display_name: "文本路由 Skill",
                version_number: 2,
                reference_name: "TEXT_ROUTE_DB_HOST",
                source_name: "DB_HOST",
              },
              {
                kind: "mcp_grant",
                display_name: "数据库 MCP",
                version_number: 4,
                reference_name: "database-auth",
                source_name: null,
              },
              {
                kind: "system_model",
                display_name: "默认模型",
                version_number: 3,
                reference_name: "MODEL_API_KEY",
                source_name: null,
              },
            ],
            current_reference_count: 1,
            current_references: [
              {
                kind: "skill_binding",
                display_name: "已迁移 Skill",
                version_number: 5,
                reference_name: "CURRENT_DB_HOST",
                source_name: "DB_HOST",
              },
            ],
          }}
        />
      </I18nProvider>,
    );

    expect(markup).toContain("文本路由 Skill");
    expect(markup).toContain("TEXT_ROUTE_DB_HOST");
    expect(markup).toContain("DB_HOST");
    expect(markup).toContain("数据库 MCP");
    expect(markup).toContain("默认模型");
    expect(markup).toContain("当前版本使用方");
    expect(markup).toContain("已迁移 Skill");
    expect(markup).not.toContain("secret-value");
  });
});

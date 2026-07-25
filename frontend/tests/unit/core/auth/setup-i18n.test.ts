import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

import { translations } from "@/core/i18n/translations";

type SetupTranslations = {
  initAdminTitle: string;
  email: string;
  password: string;
  confirmPassword: string;
  createAdminAccount: string;
  completeAdminTitle: string;
  currentPassword: string;
  newPassword: string;
  confirmNewPassword: string;
  completeSetup: string;
};

describe("setup page i18n", () => {
  test("provides setup labels for both supported locales", () => {
    const zh = (
      translations["zh-CN"] as unknown as { setup?: SetupTranslations }
    ).setup;
    const en = (
      translations["en-US"] as unknown as { setup?: SetupTranslations }
    ).setup;

    expect(zh).toMatchObject({
      initAdminTitle: "创建管理员账号",
      email: "邮箱",
      password: "密码",
      confirmPassword: "确认密码",
      createAdminAccount: "创建管理员账号",
      completeAdminTitle: "完成管理员账号设置",
      currentPassword: "当前密码",
      newPassword: "新密码",
      confirmNewPassword: "确认新密码",
      completeSetup: "完成设置",
    });
    expect(en).toMatchObject({
      initAdminTitle: "Create admin account",
      email: "Email",
      password: "Password",
      confirmPassword: "Confirm Password",
      createAdminAccount: "Create Admin Account",
      completeAdminTitle: "Complete admin account setup",
      currentPassword: "Current password",
      newPassword: "New password",
      confirmNewPassword: "Confirm new password",
      completeSetup: "Complete Setup",
    });
  });

  test("renders setup copy through the shared i18n hook", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/app/(auth)/setup/page.tsx"),
      "utf8",
    );

    expect(source).toContain('import { useI18n } from "@/core/i18n/hooks"');
    expect(source).toContain("const { t } = useI18n()");
    expect(source).toContain("t.setup.initAdminTitle");
    expect(source).toContain("t.setup.completeAdminTitle");
    expect(source).not.toContain(">Create admin account<");
    expect(source).not.toContain(">Confirm Password<");
  });
});

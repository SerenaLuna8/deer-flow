import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next-themes", () => ({
  useTheme: () => ({
    theme: "light",
    systemTheme: "light",
    setTheme: () => undefined,
  }),
}));

rs.mock("@/core/settings", () => ({
  CHAT_CONTENT_WIDTH_OPTIONS: ["narrow", "standard", "wide", "full"],
  useLocalSettings: () => [
    { appearance: { chatContentWidth: "standard" } },
    () => undefined,
  ],
}));

import { AutomationScheduleInput } from "@/components/projects/automations/automation-schedule-input";
import { AppearanceSettingsPage } from "@/components/workspace/settings/appearance-settings-page";
import { I18nProvider } from "@/core/i18n/context";

describe("selection control accessibility", () => {
  test("announces the selected theme and names the language selector", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <AppearanceSettingsPage />
      </I18nProvider>,
    );

    expect(html).toMatch(/<button[^>]*aria-pressed="true"[^>]*>[\s\S]*?Light/u);
    expect(html).toContain('aria-label="Language"');
  });

  test("announces the selected Automation schedule type", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <AutomationScheduleInput
          initial={{
            schedule_type: "cron",
            schedule_spec: { cron: "0 9 * * *" },
            timezone: "UTC",
          }}
          onChange={() => undefined}
        />
      </I18nProvider>,
    );

    expect(html).toContain('role="group"');
    expect(html).toContain('aria-label="Schedule"');
    expect(html).toMatch(
      /<button[^>]*aria-pressed="true"[^>]*>[\s\S]*?Recurring/u,
    );
    expect(html).toMatch(
      /<button[^>]*aria-pressed="false"[^>]*>[\s\S]*?One-time/u,
    );
  });
});

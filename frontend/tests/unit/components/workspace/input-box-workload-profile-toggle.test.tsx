import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { InputBoxWorkloadProfileToggle } from "@/components/workspace/input-box-workload-profile-toggle";
import { I18nProvider } from "@/core/i18n/context";

function render(profile: "interactive" | "research") {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <InputBoxWorkloadProfileToggle
        disabled={false}
        profile={profile}
        onSelect={() => undefined}
      />
    </I18nProvider>,
  );
}

describe("InputBoxWorkloadProfileToggle", () => {
  test("shows the one-Run Research action beside the chat controls", () => {
    const interactive = render("interactive");
    const research = render("research");

    expect(interactive).toContain("深度研究");
    expect(interactive).toContain('aria-pressed="false"');
    expect(research).toContain('aria-pressed="true"');
    expect(research).toContain("仅用于下一次发送");
  });
});

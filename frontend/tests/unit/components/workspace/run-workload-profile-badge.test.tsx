import { expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { RunWorkloadProfileBadge } from "@/components/workspace/run-workload-profile-badge";
import { I18nProvider } from "@/core/i18n/context";

function render(
  profile: "interactive" | "research",
  locale: "en-US" | "zh-CN",
) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>
      <RunWorkloadProfileBadge profile={profile} />
    </I18nProvider>,
  );
}

test("shows only the server-confirmed effective Run workload profile", () => {
  const research = render("research", "zh-CN");
  const interactive = render("interactive", "en-US");

  expect(research).toContain('data-testid="effective-run-workload-profile"');
  expect(research).toContain('data-workload-profile="research"');
  expect(research).toContain("服务端确认：深度研究");
  expect(interactive).toContain('data-workload-profile="interactive"');
  expect(interactive).toContain("Server-confirmed: Interactive");
});

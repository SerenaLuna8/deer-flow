import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AssistantProcessDisclosure } from "@/components/workspace/messages/assistant-process-disclosure";
import { I18nProvider } from "@/core/i18n/context";

function renderDisclosure(autoCollapseOnMount = false) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <AssistantProcessDisclosure
        autoCollapseOnMount={autoCollapseOnMount}
        stepCount={1}
      >
        <div data-testid="process-step">过程内容</div>
      </AssistantProcessDisclosure>
    </I18nProvider>,
  );
}

describe("AssistantProcessDisclosure", () => {
  test("keeps historical execution processes collapsed", () => {
    const html = renderDisclosure();

    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain('data-testid="process-step"');
  });

  test("mounts a newly completed process open before auto-collapsing it", () => {
    const html = renderDisclosure(true);

    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain('data-testid="process-step"');
    expect(html).toContain("animate-assistant-process-collapse");
    expect(html).toContain('data-initial-open="true"');
  });
});

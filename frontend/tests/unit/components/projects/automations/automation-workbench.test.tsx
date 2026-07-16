import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AutomationWorkbench,
  settleAutomationAction,
} from "@/components/projects/automations/automation-workbench";
import { I18nProvider } from "@/core/i18n/context";

import {
  AUTOMATION,
  AUTOMATION_RUN,
} from "../../../core/project-automations/fixtures";

function renderWorkbench(
  props: Partial<React.ComponentProps<typeof AutomationWorkbench>> = {},
) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <AutomationWorkbench
        projectSlug="alpha team"
        automations={[AUTOMATION]}
        selected={AUTOMATION}
        runs={[{ ...AUTOMATION_RUN, thread_id: "thread / one" }]}
        permissions={{ canRead: true, canManage: false, canExecute: false }}
        schedulerEnabled
        agents={[]}
        onSelect={() => undefined}
        {...props}
      />
    </I18nProvider>,
  );
}

describe("AutomationWorkbench", () => {
  test("renders Viewer history and private Thread links without mutation controls", () => {
    const html = renderWorkbench();

    expect(html).toContain(AUTOMATION.title);
    expect(html).toContain('data-testid="automation-run-list"');
    expect(html).toContain("/projects/alpha%20team/chats/thread%20%2F%20one");
    for (const action of ["立即运行", "删除", "编辑", "暂停", "恢复"]) {
      expect(html).not.toContain(`>${action}<`);
    }
  });

  test("keeps manual execution available when scheduler polling is disabled", () => {
    const html = renderWorkbench({
      schedulerEnabled: false,
      permissions: { canRead: true, canManage: true, canExecute: true },
      onTrigger: async () => undefined,
    });

    expect(html).toContain('data-testid="automation-scheduler-disabled"');
    expect(html).toMatch(
      /<button(?![^>]*\sdisabled(?:=|>))[^>]*>立即运行<\/button>/u,
    );
  });

  test.each([
    ["conflict", "状态已更新，请刷新后重试。", true],
    ["rate_limit", "当前并发已达上限，请稍后重试。", false],
    ["unavailable", "Automation 暂时不可用，请稍后重试。", true],
  ] as const)("renders safe %s feedback", (kind, message, canRefresh) => {
    const html = renderWorkbench({
      actionFeedback: { kind, message },
      onRefresh: canRefresh ? async () => undefined : undefined,
    });
    expect(html).toContain(message);
    expect(html.includes(">刷新<")).toBe(canRefresh);
    expect(html).not.toContain("SELECT ");
  });

  test("clears the in-memory create form after success without persisting prompt data", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/automations/automation-workbench.tsx",
      ),
      "utf8",
    );
    const formSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/automations/automation-form.tsx",
      ),
      "utf8",
    );

    expect(source).toContain("setCreateGeneration((value) => value + 1)");
    expect(source).toContain("setCreateOpen(false)");
    expect(`${source}\n${formSource}`).not.toMatch(
      /localStorage|sessionStorage|URLSearchParams/u,
    );
  });

  test("settles rejected button actions after the page records safe feedback", async () => {
    const error = new Error("safe public failure");
    await expect(
      settleAutomationAction(async () => {
        throw error;
      }),
    ).resolves.toBeUndefined();
  });
});

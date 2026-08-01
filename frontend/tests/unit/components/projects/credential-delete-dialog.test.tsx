import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  CredentialDeleteConfirmation,
  credentialDeleteSecondsRemaining,
  createCredentialDeleteSnapshot,
} from "@/components/projects/assets/credential-delete-dialog";
import { Dialog } from "@/components/ui/dialog";
import { I18nProvider } from "@/core/i18n/context";

describe("Credential delete confirmation", () => {
  test("freezes identity and optimistic revision for the five-second delay", () => {
    const snapshot = createCredentialDeleteSnapshot(
      {
        id: "11111111-1111-4111-8111-111111111111",
        display_name: "Project API",
        version: 4,
      },
      1_000,
    );

    expect(snapshot).toEqual({
      credentialId: "11111111-1111-4111-8111-111111111111",
      credentialName: "Project API",
      expectedCredentialVersion: 4,
      startedAt: 1_000,
    });
    expect(Object.isFrozen(snapshot)).toBe(true);
    expect(credentialDeleteSecondsRemaining(1_000, 1_000)).toBe(5);
    expect(credentialDeleteSecondsRemaining(1_000, 5_001)).toBe(1);
    expect(credentialDeleteSecondsRemaining(1_000, 6_000)).toBe(0);
  });

  test("explains logical deletion and keeps the destructive action delayed", () => {
    const waiting = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <Dialog open>
          <CredentialDeleteConfirmation
            credentialName="Project API"
            remainingSeconds={5}
            pending={false}
            errorMessage={null}
            onCancel={() => undefined}
            onConfirm={() => undefined}
          />
        </Dialog>
      </I18nProvider>,
    );

    expect(waiting).toContain("删除凭据");
    expect(waiting).toContain("普通列表与运行时");
    expect(waiting).toContain("仅审计记录保留");
    expect(waiting).toContain("确认删除（5 秒）");
    expect(waiting).toContain('disabled=""');

    const ready = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <Dialog open>
          <CredentialDeleteConfirmation
            credentialName="Project API"
            remainingSeconds={0}
            pending={false}
            errorMessage="Credential 状态已变化，请刷新后重试。"
            onCancel={() => undefined}
            onConfirm={() => undefined}
          />
        </Dialog>
      </I18nProvider>,
    );
    expect(ready).toContain(">确认删除<");
    expect(ready).toContain('role="alert"');
    expect(ready).not.toContain('disabled=""');
  });
});

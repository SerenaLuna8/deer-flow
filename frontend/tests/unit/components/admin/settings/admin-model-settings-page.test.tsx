import { describe, expect, rs, test } from "@rstest/core";
import type { ComponentProps, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogContent: ({
    children,
    closeLabel: _closeLabel,
    ...props
  }: ComponentProps<"div"> & { closeLabel?: string }) => (
    <div {...props}>{children}</div>
  ),
  DialogDescription: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  DialogFooter: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  DialogHeader: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  DialogTitle: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

import {
  ModelEditorDialog,
  type AdminModelCredentialOption,
} from "@/components/admin/settings/admin-model-settings-page";
import { I18nProvider } from "@/core/i18n/context";

const credentials: AdminModelCredentialOption[] = [
  {
    id: "11111111-1111-4111-8111-111111111111",
    display_name: "OpenAI primary",
    credential_type: "model_api_key",
    current_version_id: "22222222-2222-4222-8222-222222222222",
    version: 1,
    status: "active",
  },
];

function renderEditor(): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <ModelEditorDialog
        open
        model={null}
        credentials={credentials}
        credentialStatus="ready"
        credentialsRefreshing={false}
        pending={false}
        mutationError={null}
        onOpenChange={() => undefined}
        onRetryCredentials={() => undefined}
        onSubmit={async () => true}
        onTestConnection={async () => ({
          status: "succeeded",
          request_id: "test-request",
        })}
      />
    </I18nProvider>,
  );
}

describe("ModelEditorDialog", () => {
  test("keeps the Base URL in basic configuration before credential and runtime sections", () => {
    const markup = renderEditor();
    const basicStart = markup.indexOf(
      'data-testid="admin-model-editor-basic-information"',
    );
    const credentialStart = markup.indexOf(
      'data-testid="admin-model-editor-credential-binding"',
    );
    const capabilitiesStart = markup.indexOf(
      'data-testid="admin-model-editor-capabilities-and-runtime"',
    );
    const baseUrl = markup.indexOf('id="base_url"');

    expect(basicStart).toBeGreaterThan(-1);
    expect(baseUrl).toBeGreaterThan(basicStart);
    expect(baseUrl).toBeLessThan(credentialStart);
    expect(credentialStart).toBeLessThan(capabilitiesStart);
  });

  test("keeps advanced JSON collapsed until the user chooses to open it", () => {
    const markup = renderEditor();
    const advancedStart = markup.indexOf(
      'data-testid="admin-model-editor-advanced-settings"',
    );
    const advancedTagStart = markup.lastIndexOf("<details", advancedStart);
    const advancedTag = markup.slice(
      advancedTagStart,
      markup.indexOf(">", advancedStart) + 1,
    );

    expect(advancedTag).toContain("<details");
    expect(advancedTag).not.toContain("open");
  });

  test("offers a connection test after the Credential binding", () => {
    const markup = renderEditor();
    const credentialStart = markup.indexOf(
      'data-testid="admin-model-editor-credential-binding"',
    );
    const testAction = markup.indexOf(
      'data-testid="admin-model-test-connection"',
    );
    const capabilityStart = markup.indexOf(
      'data-testid="admin-model-editor-capabilities-and-runtime"',
    );

    expect(testAction).toBeGreaterThan(credentialStart);
    expect(testAction).toBeLessThan(capabilityStart);
    expect(markup).toContain("测试连通性");
  });
});

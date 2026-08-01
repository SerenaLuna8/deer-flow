import { describe, expect, rs, test } from "@rstest/core";
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  PropsWithChildren,
} from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/components/ui/button", () => ({
  Button: ({
    size: _size,
    variant: _variant,
    ...props
  }: ButtonHTMLAttributes<HTMLButtonElement> & {
    size?: string;
    variant?: string;
  }) => <button {...props} />,
}));

rs.mock("@/components/ui/dialog", () => {
  function Container({ children }: PropsWithChildren) {
    return <div>{children}</div>;
  }

  return {
    Dialog: ({ children, open }: PropsWithChildren<{ open?: boolean }>) =>
      open ? <div>{children}</div> : null,
    DialogContent: Container,
    DialogDescription: ({ children }: PropsWithChildren) => <p>{children}</p>,
    DialogFooter: Container,
    DialogHeader: Container,
    DialogTitle: ({ children }: PropsWithChildren) => <h2>{children}</h2>,
  };
});

rs.mock("@/components/ui/input", () => ({
  Input: (props: InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

import {
  buildCredentialPayload,
  CredentialFieldInputError,
  CredentialSecretDialog,
  credentialValueInputName,
  submitCredentialSecretForm,
  type CredentialSecretFieldRow,
} from "@/components/admin/assets/admin-asset-dialogs";
import { I18nProvider } from "@/core/i18n/context";

const SECRET_SENTINEL = "credential-secret-sentinel-7f4a";

function secretForm(rows: readonly CredentialSecretFieldRow[]): FormData {
  const form = new FormData();
  for (const [index, row] of rows.entries()) {
    form.set(credentialValueInputName(row.id), `${SECRET_SENTINEL}-${index}`);
  }
  return form;
}

describe("CredentialSecretDialog multi-field writes", () => {
  test("builds all supported groups without placing secret values in row state", () => {
    const rows: CredentialSecretFieldRow[] = [
      { id: "row-env-token", group: "env", field: "GITHUB_TOKEN" },
      { id: "row-env-org", group: "env", field: "GITHUB_ORG" },
      {
        id: "row-header-auth",
        group: "headers",
        field: "Authorization",
      },
      { id: "row-oauth", group: "oauth", field: "refresh_token" },
    ];

    expect(buildCredentialPayload(rows, secretForm(rows))).toEqual({
      env: {
        GITHUB_TOKEN: `${SECRET_SENTINEL}-0`,
        GITHUB_ORG: `${SECRET_SENTINEL}-1`,
      },
      headers: { Authorization: `${SECRET_SENTINEL}-2` },
      oauth: { refresh_token: `${SECRET_SENTINEL}-3` },
    });
    expect(JSON.stringify(rows)).not.toContain(SECRET_SENTINEL);
  });

  test.each([
    {
      label: "an empty field name",
      rows: [{ id: "row-empty", group: "env", field: "" }],
      expectedCode: "empty_field",
    },
    {
      label: "a duplicate field in one group",
      rows: [
        { id: "row-a", group: "env", field: "TOKEN" },
        { id: "row-b", group: "env", field: "TOKEN" },
      ],
      expectedCode: "duplicate_field",
    },
    {
      label: "an unsupported payload group",
      rows: [{ id: "row-cookie", group: "cookies", field: "session" }],
      expectedCode: "unsupported_group",
    },
  ])("rejects $label with a secret-safe error", ({ rows, expectedCode }) => {
    const form = secretForm(rows as CredentialSecretFieldRow[]);

    try {
      buildCredentialPayload(rows as CredentialSecretFieldRow[], form);
      throw new Error("expected Credential field validation to fail");
    } catch (error) {
      expect(error).toBeInstanceOf(CredentialFieldInputError);
      expect(error).toMatchObject({ code: expectedCode });
      expect(String(error)).not.toContain(SECRET_SENTINEL);
    }
  });

  test("clears the form before dispatching create and replace callbacks", () => {
    const rows: CredentialSecretFieldRow[] = [
      { id: "row-token", group: "env", field: "TOKEN" },
    ];
    const createForm = secretForm(rows);
    createForm.set("name", "github");
    createForm.set("display_name", "GitHub");
    createForm.set("credential_type", "token");
    const createOrder: string[] = [];
    const onCreate = rs.fn(() => createOrder.push("create"));

    submitCredentialSecretForm({
      mode: "create",
      rows,
      form: createForm,
      expectedVersion: undefined,
      clear: () => createOrder.push("clear"),
      onCreate,
    });

    expect(createOrder).toEqual(["clear", "create"]);
    expect(onCreate).toHaveBeenCalledWith({
      name: "github",
      display_name: "GitHub",
      credential_type: "token",
      payload: { env: { TOKEN: `${SECRET_SENTINEL}-0` } },
    });

    const replaceOrder: string[] = [];
    const onReplace = rs.fn(() => replaceOrder.push("replace"));
    submitCredentialSecretForm({
      mode: "replace",
      rows,
      form: secretForm(rows),
      expectedVersion: 7,
      clear: () => replaceOrder.push("clear"),
      onReplace,
    });

    expect(replaceOrder).toEqual(["clear", "replace"]);
    expect(onReplace).toHaveBeenCalledWith({
      payload: { env: { TOKEN: `${SECRET_SENTINEL}-0` } },
      expected_credential_version: 7,
    });
  });

  test("prefills replacement field names only and never renders secret values", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <CredentialSecretDialog
          mode="replace"
          open
          expectedVersion={3}
          pending={false}
          errorMessage={null}
          initialFields={[
            { group: "env", field: "GITHUB_TOKEN" },
            { group: "headers", field: "Authorization" },
            { group: "oauth", field: "refresh_token" },
          ]}
          onOpenChange={rs.fn()}
          onReplace={rs.fn()}
        />
      </I18nProvider>,
    );

    expect(html).toContain('value="GITHUB_TOKEN"');
    expect(html).toContain('value="Authorization"');
    expect(html).toContain('value="refresh_token"');
    expect(html.match(/type="password"/gu)).toHaveLength(3);
    expect(html).not.toContain(SECRET_SENTINEL);
  });

  test("keeps history unavailability separate from write progress", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <CredentialSecretDialog
          mode="replace"
          open
          expectedVersion={3}
          pending={false}
          disabled
          errorMessage="版本结构暂时无法加载。"
          onRetry={rs.fn()}
          onOpenChange={rs.fn()}
          onReplace={rs.fn()}
        />
      </I18nProvider>,
    );

    expect(html).toContain("版本结构暂时无法加载。");
    expect(html).toContain("重新加载");
    expect(html).toContain("替换凭据");
    expect(html).not.toContain("写入中…");
    expect(html).toMatch(/<button[^>]+type="submit"[^>]+disabled/gu);
  });
});

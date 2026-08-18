import { describe, expect, test, rs } from "@rstest/core";

import {
  CredentialFieldInputError,
  submitCredentialSecretForm,
  type CredentialSecretFieldRow,
} from "@/components/admin/assets/admin-asset-dialogs";
import { createCredentialInputSchema } from "@/core/shared-assets";

const row: CredentialSecretFieldRow = {
  id: "row-1",
  group: "env",
  field: "API_TOKEN",
};

function createForm(name: string): FormData {
  const form = new FormData();
  form.set("display_name", "Example Credential");
  form.set("name", name);
  form.set("credential_type", "model_api_key");
  form.set("credential_value:row-1", "test-value");
  return form;
}

describe("credential secret form submission", () => {
  test("rejects an invalid Credential slug before clearing or dispatching", () => {
    const clearSecrets = rs.fn();
    const onCreate = rs.fn();

    let thrown: unknown;
    try {
      submitCredentialSecretForm({
        mode: "create",
        rows: [row],
        form: createForm("Bad Slug!"),
        expectedVersion: undefined,
        clearSecrets,
        onCreate,
      });
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBeInstanceOf(CredentialFieldInputError);
    expect(thrown).toMatchObject({
      code: "invalid_name",
      target: "credential_name",
    });
    expect(clearSecrets).not.toHaveBeenCalled();
    expect(onCreate).not.toHaveBeenCalled();
    expect(
      createCredentialInputSchema.safeParse({
        name: "Bad Slug!",
        display_name: "Example Credential",
        credential_type: "model_api_key",
        payload: { env: { API_TOKEN: "test-value" } },
      }).success,
    ).toBe(false);
  });

  test("clears only the secret controls after dispatching a valid create", () => {
    const clearSecrets = rs.fn();
    const onCreate = rs.fn();

    submitCredentialSecretForm({
      mode: "create",
      rows: [row],
      form: createForm("example-credential"),
      expectedVersion: undefined,
      clearSecrets,
      onCreate,
    });

    expect(onCreate).toHaveBeenCalledTimes(1);
    expect(clearSecrets).toHaveBeenCalledTimes(1);
  });
});

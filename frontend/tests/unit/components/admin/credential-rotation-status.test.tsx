import { describe, expect, test, rs } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { CredentialRotationStatusCard } from "@/components/admin/assets/credential-rotation-status";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  credentialRotationStatusSchema,
  getAdminCredentialRotationStatus,
} from "@/core/shared-assets";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("credential rotation status", () => {
  test("strict contract rejects envelope and key metadata", () => {
    const safe = {
      eligible_total: 7,
      current: 5,
      pending: 2,
      status: "pending",
    };
    expect(credentialRotationStatusSchema.parse(safe)).toEqual(safe);
    expect(
      credentialRotationStatusSchema.safeParse({
        ...safe,
        key_id: "secret-key",
      }).success,
    ).toBe(false);
    expect(
      credentialRotationStatusSchema.safeParse({ ...safe, nonce: "secret" })
        .success,
    ).toBe(false);
  });

  test("authenticated API reads only the static admin aggregate route", async () => {
    rs.mocked(fetchWithAuth).mockResolvedValueOnce(
      jsonResponse({
        eligible_total: 7,
        current: 5,
        pending: 2,
        status: "pending",
      }),
    );

    await expect(getAdminCredentialRotationStatus()).resolves.toMatchObject({
      pending: 2,
    });
    expect(fetchWithAuth).toHaveBeenCalledWith(
      "/backend/api/admin/assets/credentials/rotation-status",
      { signal: undefined },
    );
  });

  test("UI distinguishes current and pending rotation without key details", () => {
    const pending = renderToStaticMarkup(
      createElement(CredentialRotationStatusCard, {
        status: {
          eligible_total: 7,
          current: 5,
          pending: 2,
          status: "pending",
        },
      }),
    );
    const current = renderToStaticMarkup(
      createElement(CredentialRotationStatusCard, {
        status: {
          eligible_total: 7,
          current: 7,
          pending: 0,
          status: "current",
        },
      }),
    );

    expect(pending).toContain("待轮换 2 项");
    expect(current).toContain("轮换正常");
    for (const forbidden of [
      "key_id",
      "nonce",
      "ciphertext",
      "storage_locator",
    ]) {
      expect(pending).not.toContain(forbidden);
      expect(current).not.toContain(forbidden);
    }
  });
});

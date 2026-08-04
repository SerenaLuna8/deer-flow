import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

import { credentialPayloadFieldsFromVersions } from "@/components/projects/assets/project-assets-page";
import type { AssetVersion } from "@/core/shared-assets";

const CURRENT_ID = "11111111-1111-4111-8111-111111111111";
const CREDENTIAL_ID = "22222222-2222-4222-8222-222222222222";

function credentialVersion(
  overrides: Partial<Extract<AssetVersion, { credential_id: string }>> = {},
): Extract<AssetVersion, { credential_id: string }> {
  return {
    id: CURRENT_ID,
    credential_id: CREDENTIAL_ID,
    version_number: 2,
    status: "active",
    payload_schema_version: 1,
    payload_schema: {
      env: ["REGION", "TOKEN"],
      headers: ["Authorization"],
      query: ["key"],
      oauth: ["client_id"],
    },
    supersedes_version_id: null,
    created_by_user_id: "owner",
    created_at: "2026-07-21T08:00:00Z",
    ...overrides,
  };
}

describe("project Credential replacement", () => {
  test("prepares every current payload-schema field without any value", () => {
    expect(
      credentialPayloadFieldsFromVersions([credentialVersion()], CURRENT_ID),
    ).toEqual([
      { group: "env", field: "REGION" },
      { group: "env", field: "TOKEN" },
      { group: "headers", field: "Authorization" },
      { group: "query", field: "key" },
      { group: "oauth", field: "client_id" },
    ]);
  });

  test("fails closed when current schema is missing, retired, or unknown", () => {
    expect(credentialPayloadFieldsFromVersions([], CURRENT_ID)).toBeNull();
    expect(
      credentialPayloadFieldsFromVersions(
        [credentialVersion({ status: "retired" })],
        CURRENT_ID,
      ),
    ).toBeNull();
    expect(
      credentialPayloadFieldsFromVersions(
        [
          {
            ...credentialVersion(),
            payload_schema: { unsupported: ["secret"] },
          } as unknown as AssetVersion,
        ],
        CURRENT_ID,
      ),
    ).toBeNull();
  });

  test("uses a dedicated project Credential route", () => {
    const route = readFileSync(
      resolve(
        process.cwd(),
        "src/app/projects/[project_slug]/credentials/page.tsx",
      ),
      "utf8",
    );
    expect(route).toContain("ProjectCredentialPage");
    expect(route).not.toContain('ProjectAssetsPage kind="credentials"');
  });

  test("does not render redundant explanatory copy above the Credential list", () => {
    const page = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/credentials/project-credential-page.tsx",
      ),
      "utf8",
    );
    expect(page).toContain('title="项目凭证"');
    expect(page).toContain(
      'description="管理项目运行所需的环境变量、请求头、查询参数和 OAuth 字段。"',
    );
    expect(page).not.toContain("管理 MCP 连接所需");
    expect(page).not.toContain("敏感值只写入一次");
    expect(page).not.toContain("凭据值提交后不会回显");
  });
});

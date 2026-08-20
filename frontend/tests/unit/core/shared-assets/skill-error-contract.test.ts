import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  changeProjectAssetStatus,
  deleteProjectSkill,
  importProjectSkillArchive,
} from "@/core/shared-assets";
import type { SharedAssetApiError } from "@/core/shared-assets";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const ASSET_ID = "22222222-2222-4222-8222-222222222222";

afterEach(() => {
  rs.unstubAllGlobals();
});

function errorResponse(code: string, message: string, status = 409) {
  return Response.json(
    {
      detail: {
        code,
        message,
        request_id: "request-1",
      },
    },
    { status },
  );
}

describe("Project Skill error contract", () => {
  test("accepts the stable in-use delete error", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=asset-token" });
    rs.stubGlobal("fetch", async () =>
      errorResponse("ASSET_IN_USE", "Asset is still referenced"),
    );

    await expect(
      deleteProjectSkill(PROJECT_ID, ASSET_ID, {
        expected_revision: 3,
      }),
    ).rejects.toMatchObject({
      status: 409,
      code: "ASSET_IN_USE",
    } satisfies Partial<SharedAssetApiError>);
  });

  test("accepts runtime-name conflicts from project activation", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=asset-token" });
    rs.stubGlobal("fetch", async () =>
      errorResponse(
        "SKILL_RUNTIME_NAME_CONFLICT",
        "Skill runtime name conflict",
      ),
    );

    await expect(
      changeProjectAssetStatus(PROJECT_ID, "skills", ASSET_ID, "enable", {
        expected_revision: 3,
      }),
    ).rejects.toMatchObject({
      status: 409,
      code: "SKILL_RUNTIME_NAME_CONFLICT",
    } satisfies Partial<SharedAssetApiError>);
  });

  test("normalizes the middleware 413 into the upload-limit client code", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=asset-token" });
    rs.stubGlobal("fetch", async () =>
      errorResponse(
        "skill_version_request_body_too_large",
        "Skill version request body is too large.",
        413,
      ),
    );

    await expect(
      importProjectSkillArchive(
        PROJECT_ID,
        new File(["archive"], "catalog-auditor.zip"),
      ),
    ).rejects.toMatchObject({
      status: 413,
      code: "ASSET_UPLOAD_TOO_LARGE",
    } satisfies Partial<SharedAssetApiError>);
  });

  test("accepts the backend archive-limit code on parsed error routes", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=asset-token" });
    rs.stubGlobal("fetch", async () =>
      errorResponse(
        "SKILL_ARCHIVE_LIMIT_EXCEEDED",
        "Skill archive exceeds the allowed size or member limit",
        413,
      ),
    );

    await expect(
      deleteProjectSkill(PROJECT_ID, ASSET_ID, {
        expected_revision: 3,
      }),
    ).rejects.toMatchObject({
      status: 413,
      code: "ASSET_UPLOAD_TOO_LARGE",
    } satisfies Partial<SharedAssetApiError>);
  });
});

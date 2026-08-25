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
  test("returns the number of Agent Definitions unbound by logical deletion", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=asset-token" });
    const response = {
      skill_id: ASSET_ID,
      affected_agent_count: 2,
      request_id: "skill-delete",
    };
    rs.stubGlobal("fetch", async () => Response.json(response));

    await expect(
      deleteProjectSkill(PROJECT_ID, ASSET_ID, {
        expected_revision: 3,
      }),
    ).resolves.toEqual(response);
  });

  test("rejects malformed Skill deletion results", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=asset-token" });
    rs.stubGlobal("fetch", async () =>
      Response.json({
        skill_id: ASSET_ID,
        affected_agent_count: 2,
        request_id: "skill-delete",
        agent_ids: ["55555555-5555-4555-8555-555555555555"],
      }),
    );

    await expect(
      deleteProjectSkill(PROJECT_ID, ASSET_ID, {
        expected_revision: 3,
      }),
    ).rejects.toMatchObject({
      code: "ASSET_RESPONSE_INVALID",
    } satisfies Partial<SharedAssetApiError>);
  });

  test("rejects a Skill deletion result for another asset", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=asset-token" });
    rs.stubGlobal("fetch", async () =>
      Response.json({
        skill_id: "33333333-3333-4333-8333-333333333333",
        affected_agent_count: 2,
        request_id: "skill-delete",
      }),
    );

    await expect(
      deleteProjectSkill(PROJECT_ID, ASSET_ID, {
        expected_revision: 3,
      }),
    ).rejects.toMatchObject({
      code: "ASSET_RESPONSE_INVALID",
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

  test("submits the archive as the only multipart field", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=asset-token" });
    let submittedBody: FormData | undefined;
    rs.stubGlobal(
      "fetch",
      async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.body instanceof FormData) submittedBody = init.body;
        return errorResponse(
          "asset_validation_failed",
          "Asset validation failed",
          422,
        );
      },
    );

    await expect(
      importProjectSkillArchive(
        PROJECT_ID,
        new File(["archive"], "ordinary-upload.zip"),
      ),
    ).rejects.toMatchObject({
      status: 422,
      code: "ASSET_VALIDATION_FAILED",
    } satisfies Partial<SharedAssetApiError>);

    expect(submittedBody).toBeInstanceOf(FormData);
    expect([...submittedBody!.keys()]).toEqual(["archive"]);
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

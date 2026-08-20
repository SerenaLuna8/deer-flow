import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  exportAdminSkillVersion,
  exportProjectSkillVersion,
} from "@/core/shared-assets";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const SKILL_ID = "22222222-2222-4222-8222-222222222222";
const VERSION_ID = "33333333-3333-4333-8333-333333333333";

afterEach(() => {
  rs.unstubAllGlobals();
});

function zipResponse(filename = "meeting-brief-v7.zip") {
  return new Response(new Uint8Array([80, 75, 3, 4]), {
    headers: {
      "Content-Type": "application/zip",
      "Content-Disposition": `attachment; filename="${filename}"`,
    },
  });
}

describe("Skill distribution export API", () => {
  test("downloads the exact selected Project Skill version", async () => {
    const fetchMock = rs.fn(async () => zipResponse());
    rs.stubGlobal("fetch", fetchMock);

    const download = await exportProjectSkillVersion(
      PROJECT_ID,
      SKILL_ID,
      VERSION_ID,
    );

    expect(download.filename).toBe("meeting-brief-v7.zip");
    expect(
      Array.from(new Uint8Array(await download.content.arrayBuffer())),
    ).toEqual([80, 75, 3, 4]);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(
        `/api/projects/${PROJECT_ID}/skills/${SKILL_ID}/versions/${VERSION_ID}/export`,
      ),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  test("uses the global System Skill endpoint for admin details", async () => {
    const fetchMock = rs.fn(async () => zipResponse("system-skill-v2.zip"));
    rs.stubGlobal("fetch", fetchMock);

    await expect(
      exportAdminSkillVersion(SKILL_ID, VERSION_ID),
    ).resolves.toEqual(
      expect.objectContaining({ filename: "system-skill-v2.zip" }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(
        `/api/admin/assets/skills/${SKILL_ID}/versions/${VERSION_ID}/export`,
      ),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  test("rejects a response that is not a safe ZIP download", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(
        async () =>
          new Response("not a zip", {
            headers: {
              "Content-Type": "text/plain",
              "Content-Disposition": 'attachment; filename="unsafe.txt"',
            },
          }),
      ),
    );

    await expect(
      exportProjectSkillVersion(PROJECT_ID, SKILL_ID, VERSION_ID),
    ).rejects.toMatchObject({
      code: "ASSET_RESPONSE_INVALID",
    });
  });
});

import { beforeEach, describe, expect, rs, test } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "" }));

import { fetch as fetcher } from "@/core/api/fetcher";
import { loadSkillContent, SkillRequestError } from "@/core/skills/api";

const mockedFetch = rs.mocked(fetcher);

describe("loadSkillContent", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
  });

  test("loads encoded skill content", async () => {
    const content = {
      content: "---\nname: paper review\n---\n# Workflow",
    };
    mockedFetch.mockResolvedValueOnce(
      new Response(JSON.stringify(content), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    // The value is deliberately outside the validated skill-name grammar so
    // this pure URL-boundary test proves the client encodes path input.
    await expect(loadSkillContent("paper review")).resolves.toEqual(content);
    expect(mockedFetch).toHaveBeenCalledWith(
      "/api/skills/content/paper%20review",
    );
  });

  test("maps detail failures to SkillRequestError", async () => {
    mockedFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Skill content unavailable" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(loadSkillContent("missing")).rejects.toMatchObject({
      name: "SkillRequestError",
      status: 404,
      message: "Skill content unavailable",
    });
    await expect(
      Promise.reject(new SkillRequestError(500, "x")),
    ).rejects.toBeInstanceOf(SkillRequestError);
  });
});

import { beforeEach, describe, expect, test, rs } from "@rstest/core";

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { polishInputDraft } from "@/core/input-polish/api";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));

const mockedFetch = rs.mocked(fetchWithAuth);
const projectId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const access = {
  scope: {
    accountId: "11111111-1111-4111-8111-111111111111",
    projectId,
  },
  apiBaseURL: `/api/projects/${projectId}/private-work`,
};

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("project input polish adapter", () => {
  test("uses only the active project private-work URL", async () => {
    mockedFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ rewritten_text: "Clear request", changed: true }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await polishInputDraft(access, {
      text: "request",
      thread_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    });

    expect(mockedFetch).toHaveBeenCalledWith(
      `/api/projects/${projectId}/private-work/input-polish`,
      expect.objectContaining({ method: "POST" }),
    );
  });
});

import { afterEach, describe, expect, rs, test } from "@rstest/core";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";

describe("protected auth fetch redirects", () => {
  afterEach(() => {
    rs.unstubAllGlobals();
  });

  test("401 redirects with the complete private browser destination", async () => {
    const location = {
      pathname: "/projects/example/chats",
      search: "?thread=abc",
      hash: "#latest",
      href: "",
    };
    rs.stubGlobal("window", { location });
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => new Response(null, { status: 401 })),
    );

    await expect(fetchWithAuth("/api/projects")).rejects.toBeInstanceOf(
      AuthRequiredError,
    );
    expect(location.href).toBe(
      "/login?next=%2Fprojects%2Fexample%2Fchats%3Fthread%3Dabc%23latest",
    );
  });
});

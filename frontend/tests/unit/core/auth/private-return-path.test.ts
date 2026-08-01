import { describe, expect, test } from "@rstest/core";

import {
  PRIVATE_RETURN_PATH_HEADER,
  currentBrowserReturnPath,
  isPrivateRoutePath,
  privateReturnPathFromHeaders,
} from "@/core/auth/private-return-path";
import { buildLoginUrl } from "@/core/auth/types";

describe("private auth return paths", () => {
  test("preserves browser pathname, search, and hash in the login next value", () => {
    const returnPath = currentBrowserReturnPath({
      pathname: "/projects/example/chats",
      search: "?thread=abc",
      hash: "#latest",
    } as Location);

    expect(returnPath).toBe("/projects/example/chats?thread=abc#latest");
    expect(buildLoginUrl(returnPath)).toBe(
      "/login?next=%2Fprojects%2Fexample%2Fchats%3Fthread%3Dabc%23latest",
    );
  });

  test.each([
    ["/workspace", true],
    ["/workspace/privacy", true],
    ["/projects/example", true],
    ["/admin/jobs", true],
    ["/login", false],
    ["/workspace-escape", false],
  ])("classifies %s as private=%s", (pathname, expected) => {
    expect(isPrivateRoutePath(pathname)).toBe(expected);
  });

  test("server layouts retain only paths under their own private root", () => {
    const headers = new Headers({
      [PRIVATE_RETURN_PATH_HEADER]: "/projects/example?tab=members",
    });
    expect(
      privateReturnPathFromHeaders(headers, ["/projects"], "/workspace"),
    ).toBe("/projects/example?tab=members");
    expect(
      privateReturnPathFromHeaders(headers, ["/admin"], "/admin/operations"),
    ).toBe("/admin/operations");
  });
});

import { describe, expect, test } from "@rstest/core";
import { NextRequest } from "next/server";

import {
  ADMIN_RETURN_PATH_HEADER,
  adminReturnPathFromHeaders,
} from "@/core/auth/admin-return-path";
import { config, proxy } from "@/proxy";

describe("admin return path", () => {
  test("proxy records the exact admin path and query for the server layout", () => {
    const response = proxy(
      new NextRequest(
        "https://deerflow.example/admin/assets/agents?scope=system",
        {
          headers: {
            [ADMIN_RETURN_PATH_HEADER]: "/admin/operations",
          },
        },
      ),
    );

    expect(config).toEqual({
      matcher: ["/workspace/:path*", "/projects/:path*", "/admin/:path*"],
    });
    expect(
      response.headers.get(`x-middleware-request-${ADMIN_RETURN_PATH_HEADER}`),
    ).toBe("/admin/assets/agents?scope=system");
  });

  test.each([
    [null, "/admin/operations"],
    ["/workspace", "/admin/operations"],
    ["//evil.example", "/admin/operations"],
    ["/admin/jobs?status=dead", "/admin/jobs?status=dead"],
  ])("maps %j to %s", (value, expected) => {
    expect(
      adminReturnPathFromHeaders(
        new Headers(
          value === null ? undefined : { [ADMIN_RETURN_PATH_HEADER]: value },
        ),
      ),
    ).toBe(expected);
  });
});

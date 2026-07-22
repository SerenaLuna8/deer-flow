import { type NextRequest, NextResponse } from "next/server";

import { ADMIN_RETURN_PATH_HEADER } from "@/core/auth/admin-return-path";

export function proxy(request: NextRequest) {
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set(
    ADMIN_RETURN_PATH_HEADER,
    `${request.nextUrl.pathname}${request.nextUrl.search}`,
  );
  return NextResponse.next({ request: { headers: requestHeaders } });
}

export const config = { matcher: "/admin/:path*" };

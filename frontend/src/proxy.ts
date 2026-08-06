import { type NextRequest, NextResponse } from "next/server";

import { PRIVATE_RETURN_PATH_HEADER } from "@/core/auth/private-return-path";

export function proxy(request: NextRequest) {
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set(
    PRIVATE_RETURN_PATH_HEADER,
    `${request.nextUrl.pathname}${request.nextUrl.search}`,
  );
  return NextResponse.next({ request: { headers: requestHeaders } });
}

export const config = {
  matcher: ["/workspace/:path*", "/projects/:path*", "/admin/:path*"],
};

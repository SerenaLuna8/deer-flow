import { safeInternalNextPath } from "./next-path";

export const PRIVATE_RETURN_PATH_HEADER = "x-deerflow-private-return-path";
export const PRIVATE_ROUTE_ROOTS = [
  "/workspace",
  "/projects",
  "/admin",
] as const;

export function isPrivateRoutePath(pathname: string): boolean {
  return PRIVATE_ROUTE_ROOTS.some(
    (root) => pathname === root || pathname.startsWith(`${root}/`),
  );
}

export function privateReturnPathFromHeaders(
  requestHeaders: Pick<Headers, "get">,
  allowedRoots: readonly string[],
  fallback: string,
): string {
  const candidate = safeInternalNextPath(
    requestHeaders.get(PRIVATE_RETURN_PATH_HEADER),
    fallback,
  );
  const [pathname] = candidate.split(/[?#]/u, 1);
  if (
    pathname &&
    allowedRoots.some(
      (root) => pathname === root || pathname.startsWith(`${root}/`),
    )
  ) {
    return candidate;
  }
  return fallback;
}

export function currentBrowserReturnPath(
  location: Pick<Location, "hash" | "pathname" | "search">,
): string {
  return safeInternalNextPath(
    `${location.pathname}${location.search}${location.hash}`,
  );
}

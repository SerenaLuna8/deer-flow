import { safeInternalNextPath } from "./next-path";

export const ADMIN_RETURN_PATH_HEADER = "x-deerflow-admin-return-path";
export const DEFAULT_ADMIN_RETURN_PATH = "/admin/operations";

export function adminReturnPathFromHeaders(
  requestHeaders: Pick<Headers, "get">,
): string {
  const candidate = safeInternalNextPath(
    requestHeaders.get(ADMIN_RETURN_PATH_HEADER),
    DEFAULT_ADMIN_RETURN_PATH,
  );
  const [pathname] = candidate.split(/[?#]/u, 1);
  return pathname === "/admin" || pathname?.startsWith("/admin/")
    ? candidate
    : DEFAULT_ADMIN_RETURN_PATH;
}

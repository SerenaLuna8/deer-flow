import {
  PRIVATE_RETURN_PATH_HEADER,
  privateReturnPathFromHeaders,
} from "./private-return-path";

export const ADMIN_RETURN_PATH_HEADER = PRIVATE_RETURN_PATH_HEADER;
export const DEFAULT_ADMIN_RETURN_PATH = "/admin/operations";

export function adminReturnPathFromHeaders(
  requestHeaders: Pick<Headers, "get">,
): string {
  return privateReturnPathFromHeaders(
    requestHeaders,
    ["/admin"],
    DEFAULT_ADMIN_RETURN_PATH,
  );
}

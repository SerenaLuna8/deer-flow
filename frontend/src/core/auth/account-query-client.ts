import { QueryClient } from "@tanstack/react-query";

import { abortAdminOperationsAccount } from "@/core/admin-operations/api";

export function createAccountQueryClient(): QueryClient {
  return new QueryClient();
}

export async function transitionAccountQueries(
  queryClient: QueryClient,
  previousUserId: string | null,
  nextUserId: string | null,
  options: { force?: boolean } = {},
): Promise<boolean> {
  if (!options.force && previousUserId === nextUserId) return false;
  if (previousUserId) abortAdminOperationsAccount(previousUserId);
  const cancellation = queryClient.cancelQueries();
  await cancellation;
  queryClient.clear();
  return true;
}

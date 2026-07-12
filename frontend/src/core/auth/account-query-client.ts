import { QueryClient } from "@tanstack/react-query";

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
  const cancellation = queryClient.cancelQueries();
  queryClient.clear();
  await cancellation;
  return true;
}

import type { QueryClient } from "@tanstack/react-query";

import { modelsQueryKey } from "@/core/models/hooks";

import { adminModelRegistryRoot } from "./model-registry/query-keys";
import { adminModelSettingsRoot } from "./models/query-keys";

/**
 * One provider/model configuration write moves three catalogs at once: the
 * admin text-model catalog (provider names and aggregates), the admin model
 * registry (provider cards and retrieval models), and the public model list.
 * Connection tests write no configuration and must not call this.
 */
export async function invalidateModelCatalogs(
  queryClient: QueryClient,
  accountId: string,
): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({
      queryKey: adminModelSettingsRoot(accountId),
    }),
    queryClient.invalidateQueries({
      queryKey: adminModelRegistryRoot(accountId),
    }),
    queryClient.invalidateQueries({ queryKey: modelsQueryKey }),
  ]);
}

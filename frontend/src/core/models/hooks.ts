import { useQuery } from "@tanstack/react-query";

import { loadModels } from "./api";

export const modelsQueryKey = ["models"] as const;

export function useModels({ enabled = true }: { enabled?: boolean } = {}) {
  const { data, isLoading, error } = useQuery({
    queryKey: modelsQueryKey,
    queryFn: ({ signal }) => loadModels(signal),
    enabled,
    refetchOnWindowFocus: false,
  });
  return {
    models: data?.models ?? [],
    tokenUsageEnabled: data?.token_usage.enabled ?? false,
    isLoading,
    error,
  };
}

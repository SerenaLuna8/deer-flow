import { privateWorkQueryKey } from "@/core/private-work/query-keys";
import type { ProjectClientScope } from "@/core/private-work/types";

export function projectChannelGroupBindingsQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "channel-group-bindings");
}

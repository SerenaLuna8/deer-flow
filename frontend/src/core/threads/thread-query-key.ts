import { privateWorkQueryKey } from "../private-work/query-keys";
import type { ProjectClientScope } from "../private-work/types";

export function scopedThreadQueryKey(
  scope: ProjectClientScope,
  ...segments: readonly unknown[]
) {
  return privateWorkQueryKey(scope, ...segments);
}

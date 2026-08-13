import type { Capability } from "@/core/projects/types";

import type { ProjectMemoryPermissions } from "./types";

export function projectMemoryPermissions(
  capabilities: readonly Capability[],
): ProjectMemoryPermissions {
  const canRead = capabilities.includes("private_work.read_own");
  const canCreate = capabilities.includes("private_work.create");
  return {
    canRead,
    canDream: canCreate,
    canRestore: canCreate,
  };
}

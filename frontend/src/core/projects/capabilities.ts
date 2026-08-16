import type { Capability } from "./types";

export function canOpenProjectCapabilitiesWorkspace(
  capabilities: readonly Capability[],
): boolean {
  return (
    capabilities.includes("shared_assets.edit") ||
    capabilities.includes("shared_assets.manage_bindings")
  );
}

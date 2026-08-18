import type { Capability } from "./types";

export function canReadProjectAgents(
  capabilities: readonly Capability[],
): boolean {
  return capabilities.includes("shared_assets.read");
}

export function canOpenProjectCapabilitiesWorkspace(
  capabilities: readonly Capability[],
): boolean {
  return (
    capabilities.includes("shared_assets.edit") ||
    capabilities.includes("shared_assets.manage_bindings")
  );
}

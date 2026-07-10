export const WORKSPACE_CAPABILITY_LINKS = [
  { id: "memory", href: "/workspace/memory" },
  { id: "tools", href: "/workspace/tools" },
  { id: "skills", href: "/workspace/skills" },
] as const;

export function isWorkspaceCapabilityPath(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}

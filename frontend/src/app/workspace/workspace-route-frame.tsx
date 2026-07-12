"use client";

import { usePathname } from "next/navigation";
import { type PropsWithChildren, type ReactNode } from "react";

type WorkspaceRouteFrameProps = PropsWithChildren<{
  legacyShell: ReactNode;
}>;

export function WorkspaceRouteFrame({
  children,
  legacyShell,
}: WorkspaceRouteFrameProps) {
  const pathname = usePathname();
  if (pathname === "/workspace" || pathname === "/workspace/projects") {
    return children;
  }
  return legacyShell;
}

"use client";

import { GatewayOfflineBanner } from "@/components/workspace/gateway-offline-banner";
import { useAuth } from "@/core/auth/AuthProvider";

import { ProjectWorkbench } from "./project-workbench";

export function ProjectWorkbenchPage() {
  const { user, logout } = useAuth();
  if (!user) return <GatewayOfflineBanner gatewayUnavailable />;
  return (
    <ProjectWorkbench
      key={user.id}
      userId={user.id}
      accountUsername={user.username}
      systemRole={user.system_role}
      onLogout={logout}
    />
  );
}

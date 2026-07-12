"use client";

import { useAuth } from "@/core/auth/AuthProvider";

import { ProjectWorkbench } from "./project-workbench";

export function ProjectWorkbenchPage() {
  const { user } = useAuth();
  if (!user) return null;
  return <ProjectWorkbench key={user.id} userId={user.id} />;
}

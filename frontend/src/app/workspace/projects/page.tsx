"use client";

import { ProjectWorkbench } from "@/components/projects/project-workbench";
import { useAuth } from "@/core/auth/AuthProvider";

export default function ProjectsWorkbenchPage() {
  const { user } = useAuth();
  if (!user) return null;
  return <ProjectWorkbench userId={user.id} />;
}

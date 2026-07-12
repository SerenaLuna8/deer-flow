"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { useEnterProject, useProjectBySlug } from "@/core/projects/hooks";

import { ProjectHome } from "./project-home";
import { projectErrorMessage } from "./project-view-model";

export function ProjectHomeLoader({ slug }: { slug: string }) {
  const { user } = useAuth();
  const projectQuery = useProjectBySlug(user?.id, slug);
  const projectId = projectQuery.data?.id;
  const enter = useEnterProject(user?.id, projectId);
  const enteredRef = useRef<string | null>(null);

  useEffect(() => {
    if (!projectId || enteredRef.current === projectId) return;
    enteredRef.current = projectId;
    enter.mutate();
  }, [projectId, enter]);

  const error = projectQuery.error ?? enter.error;
  if (error) {
    return (
      <main className="mx-auto flex min-h-screen max-w-xl flex-col items-center justify-center px-6 text-center">
        <h1 className="text-2xl font-semibold">无法进入项目</h1>
        <p role="alert" className="text-muted-foreground mt-3">
          {projectErrorMessage(error)}
        </p>
        <Button asChild className="mt-6">
          <Link href="/workspace/projects">返回项目工作台</Link>
        </Button>
      </main>
    );
  }

  const project = enter.data ?? projectQuery.data;
  if (!project || enter.isPending) {
    return (
      <div
        data-testid="project-home-loading"
        className="mx-auto max-w-6xl space-y-5 p-8"
      >
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-32 w-full rounded-2xl" />
        <div className="grid gap-4 sm:grid-cols-3">
          <Skeleton className="h-36 rounded-xl" />
          <Skeleton className="h-36 rounded-xl" />
          <Skeleton className="h-36 rounded-xl" />
        </div>
      </div>
    );
  }
  return <ProjectHome project={project} />;
}

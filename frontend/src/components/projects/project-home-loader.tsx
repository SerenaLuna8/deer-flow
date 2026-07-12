"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { useEnterProject, useProjectBySlug } from "@/core/projects/hooks";
import type { Project } from "@/core/projects/types";

import { ProjectHome } from "./project-home";
import {
  projectHomeIdentityKey,
  projectResultForIdentity,
  type ProjectHomeResult,
} from "./project-home-state";
import { projectErrorMessage } from "./project-view-model";

export function ProjectHomeLoader({ slug }: { slug: string }) {
  const { user } = useAuth();
  const projectQuery = useProjectBySlug(user?.id, slug);
  const projectId = projectQuery.data?.id;
  const enter = useEnterProject(user?.id, projectId);
  const { mutate: enterProject, reset: resetEnter } = enter;
  const identity = projectHomeIdentityKey(user?.id, slug, projectId);
  const identityRef = useRef(identity);
  const enteredRef = useRef<string | null>(null);
  const [attemptedIdentity, setAttemptedIdentity] = useState<string | null>(
    null,
  );
  const [enteredResult, setEnteredResult] = useState<ProjectHomeResult | null>(
    null,
  );
  identityRef.current = identity;

  const startEnter = useCallback(
    (nextIdentity: string, project: Project) => {
      enteredRef.current = nextIdentity;
      setAttemptedIdentity(nextIdentity);
      enterProject(undefined, {
        onSuccess: () => {
          if (identityRef.current === nextIdentity) {
            setEnteredResult({ identity: nextIdentity, project });
          }
        },
      });
    },
    [enterProject],
  );

  useEffect(() => {
    if (identity && enteredRef.current === identity) return;
    resetEnter();
    enteredRef.current = null;
    setAttemptedIdentity(null);
    setEnteredResult(null);
    if (!identity || !projectQuery.data) return;
    startEnter(identity, projectQuery.data);
  }, [identity, projectQuery.data, resetEnter, startEnter]);

  const enterError = attemptedIdentity === identity ? enter.error : null;
  const error = projectQuery.error ?? enterError;
  if (error) {
    return (
      <main className="mx-auto flex min-h-screen max-w-xl flex-col items-center justify-center px-6 text-center">
        <h1 className="text-2xl font-semibold">无法进入项目</h1>
        <p role="alert" className="text-muted-foreground mt-3">
          {projectErrorMessage(error)}
        </p>
        <div className="mt-6 flex gap-3">
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              if (projectQuery.error) {
                void projectQuery.refetch();
                return;
              }
              resetEnter();
              setEnteredResult(null);
              enteredRef.current = null;
              if (identity && projectQuery.data) {
                startEnter(identity, projectQuery.data);
              }
            }}
          >
            重试
          </Button>
          <Button asChild>
            <Link href="/workspace/projects">返回项目工作台</Link>
          </Button>
        </div>
      </main>
    );
  }

  const project = projectResultForIdentity(identity, enteredResult);
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

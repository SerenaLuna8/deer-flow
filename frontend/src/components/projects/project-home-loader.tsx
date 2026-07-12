"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { useEnterProject, useProjectBySlug } from "@/core/projects/hooks";

import { ProjectHome } from "./project-home";
import {
  commitProjectHomeAttempt,
  createProjectHomeAttemptCoordinator,
  projectHomeIdentityKey,
  projectResultForIdentity,
  type ProjectHomeAttemptToken,
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
  const attemptsRef = useRef(createProjectHomeAttemptCoordinator());
  const activeAttemptRef = useRef<ProjectHomeAttemptToken | null>(null);
  const [enterFailure, setEnterFailure] = useState<{
    identity: string;
    error: Error;
  } | null>(null);
  const [enteredResult, setEnteredResult] = useState<ProjectHomeResult | null>(
    null,
  );
  identityRef.current = identity;

  const startEnter = useCallback(
    (nextIdentity: string): ProjectHomeAttemptToken | null => {
      const token = attemptsRef.current.start(nextIdentity);
      if (!token) return null;
      activeAttemptRef.current = token;
      setEnterFailure(null);
      enterProject(undefined, {
        onSuccess: ({ project }) => {
          const result = commitProjectHomeAttempt(
            attemptsRef.current,
            token,
            identityRef.current,
            nextIdentity,
            project,
          );
          if (activeAttemptRef.current === token) {
            activeAttemptRef.current = null;
          }
          if (result) {
            setEnteredResult(result);
          }
        },
        onError: (error) => {
          if (
            identityRef.current === nextIdentity &&
            attemptsRef.current.fail(token)
          ) {
            activeAttemptRef.current = null;
            setEnterFailure({ identity: nextIdentity, error });
          }
        },
      });
      return token;
    },
    [enterProject],
  );

  useEffect(() => {
    const attempts = attemptsRef.current;
    attempts.activate(identity);
    resetEnter();
    if (!identity || !projectQuery.data) return;
    startEnter(identity);
    return () => {
      const active = activeAttemptRef.current;
      if (active?.identity !== identity) return;
      attempts.dispose(active);
      activeAttemptRef.current = null;
    };
  }, [identity, projectQuery.data, resetEnter, startEnter]);

  const enterError =
    enterFailure?.identity === identity ? enterFailure.error : null;
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
              setEnterFailure(null);
              if (identity && projectQuery.data) {
                startEnter(identity);
              }
            }}
          >
            重试
          </Button>
          <Button asChild>
            <Link href="/workspace">返回工作空间</Link>
          </Button>
        </div>
      </main>
    );
  }

  const project = projectResultForIdentity(identity, enteredResult);
  if (!project) {
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

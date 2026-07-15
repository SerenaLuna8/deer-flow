"use client";

import Link from "next/link";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { ProjectPrivateWorkProvider } from "@/core/private-work/provider";
import { useEnterProject, useProjectBySlug } from "@/core/projects/hooks";
import type { Project } from "@/core/projects/types";

import {
  commitProjectHomeAttempt,
  createProjectHomeAttemptCoordinator,
  projectHomeIdentityKey,
  projectResultForIdentity,
  type ProjectHomeAttemptToken,
  type ProjectHomeResult,
} from "./project-home-state";
import { ProjectShell } from "./project-shell";
import { projectErrorMessage } from "./project-view-model";

export type ProjectEntryState =
  | { status: "loading" }
  | { status: "ready"; project: Project }
  | { status: "error"; error: Error; retry: () => void };

const CurrentProjectContext = createContext<Project | undefined>(undefined);

export function useEnteredProjectBySlug(
  userId: string | null | undefined,
  slug: string,
): ProjectEntryState {
  const projectQuery = useProjectBySlug(userId, slug);
  const projectId = projectQuery.data?.id;
  const enter = useEnterProject(userId, projectId);
  const { mutate: enterProject, reset: resetEnter } = enter;
  const identity = projectHomeIdentityKey(
    userId,
    slug,
    projectId,
    projectQuery.data?.membership_version,
  );
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
          if (result) setEnteredResult(result);
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
    if (!identity) return;
    startEnter(identity);
    return () => {
      const active = activeAttemptRef.current;
      if (active?.identity !== identity) return;
      attempts.dispose(active);
      activeAttemptRef.current = null;
    };
  }, [identity, resetEnter, startEnter]);

  const enterError =
    enterFailure?.identity === identity ? enterFailure.error : null;
  const error = projectQuery.error ?? enterError;
  if (error) {
    return {
      status: "error",
      error,
      retry: () => {
        if (projectQuery.error) {
          void projectQuery.refetch();
          return;
        }
        resetEnter();
        setEnteredResult(null);
        setEnterFailure(null);
        if (identity && projectQuery.data) startEnter(identity);
      },
    };
  }

  const project = projectResultForIdentity(identity, enteredResult);
  return project ? { status: "ready", project } : { status: "loading" };
}

export function ProjectContextProvider({
  slug,
  children,
}: {
  slug: string;
  children: React.ReactNode;
}) {
  const { user, logout } = useAuth();
  const entry = useEnteredProjectBySlug(user?.id, slug);

  if (entry.status === "error") {
    return (
      <main className="mx-auto flex min-h-screen max-w-xl flex-col items-center justify-center px-6 text-center">
        <h1 className="text-2xl font-semibold">无法进入项目</h1>
        <p role="alert" className="text-muted-foreground mt-3">
          {projectErrorMessage(entry.error)}
        </p>
        <div className="mt-6 flex gap-3">
          <Button type="button" variant="outline" onClick={entry.retry}>
            重试
          </Button>
          <Button asChild>
            <Link href="/workspace">返回工作空间</Link>
          </Button>
        </div>
      </main>
    );
  }

  if (entry.status === "loading" || !user) {
    return (
      <div
        data-testid="project-shell-loading"
        aria-busy="true"
        aria-label="正在进入项目"
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

  return (
    <CurrentProjectContext.Provider value={entry.project}>
      <ProjectPrivateWorkProvider
        accountId={user.id}
        projectId={entry.project.id}
      >
        <ProjectShell
          project={entry.project}
          accountEmail={user.email}
          onLogout={logout}
        >
          {children}
        </ProjectShell>
      </ProjectPrivateWorkProvider>
    </CurrentProjectContext.Provider>
  );
}

export function useCurrentProject(): Project {
  const project = useContext(CurrentProjectContext);
  if (project === undefined) {
    throw new Error(
      "useCurrentProject must be used within a ProjectContextProvider",
    );
  }
  return project;
}

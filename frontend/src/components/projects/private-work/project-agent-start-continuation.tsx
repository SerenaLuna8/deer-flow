"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { useProjectPrivateWorkReadiness } from "@/core/private-work/readiness";
import type { ProjectClientScope } from "@/core/private-work/types";
import type { Project } from "@/core/projects/types";
import {
  useProjectDefaultAgent,
  type ProjectDefaultAgent,
  type ProjectAssetList,
} from "@/core/shared-assets";

import { useAgentMcpDependencyRuntime } from "../assets/use-mcp-dependency-runtime";

import {
  createProjectChatWithDefaultAgent,
  resolveProjectDefaultAgent,
  type ExecutableProjectAgent,
} from "./agent-selector-dialog";
import { projectNewChatErrorMessage } from "./project-new-chat-error";

type StartChatCandidateState = {
  requested: boolean;
  canCreate: boolean;
  readinessStatus: "ready" | "unavailable" | undefined;
};

const projectStartChatIntentRuns = new Map<string, Promise<string>>();

export function projectStartChatCandidate(
  catalog: ProjectAssetList | undefined,
  defaultAgent: ProjectDefaultAgent | undefined,
  state: StartChatCandidateState,
): ExecutableProjectAgent | null {
  if (
    !state.requested ||
    !state.canCreate ||
    state.readinessStatus !== "ready"
  ) {
    return null;
  }
  const resolution = resolveProjectDefaultAgent(catalog, defaultAgent);
  return resolution.status === "ready" ? resolution.agent : null;
}

export async function consumeProjectStartChatIntent({
  scope,
  projectSlug,
  intentId,
  replace,
  createChat = createProjectChatWithDefaultAgent,
}: {
  scope: ProjectClientScope;
  projectSlug: string;
  intentId: string;
  replace: (path: string) => void;
  createChat?: typeof createProjectChatWithDefaultAgent;
}): Promise<string> {
  const intentKey = `${scope.accountId}:${scope.projectId}:${intentId}`;
  let run = projectStartChatIntentRuns.get(intentKey);
  if (!run) {
    run = Promise.resolve().then(async () => {
      return createChat({
        scope,
        projectSlug,
        navigate: () => undefined,
      });
    });
    projectStartChatIntentRuns.set(intentKey, run);
    void run.catch(() => {
      if (projectStartChatIntentRuns.get(intentKey) === run) {
        projectStartChatIntentRuns.delete(intentKey);
      }
    });
  }
  const threadId = await run;
  replace(
    `/projects/${encodeURIComponent(projectSlug)}/chats/${encodeURIComponent(threadId)}`,
  );
  return threadId;
}

export type ProjectAgentStartContinuationStatus =
  | "waiting-for-service"
  | "waiting-for-agent"
  | "creating-chat"
  | "read-only"
  | "error";

export function ProjectAgentStartContinuationView({
  status,
  onRetry,
  errorMessage,
}: {
  status: ProjectAgentStartContinuationStatus;
  onRetry?: () => void;
  errorMessage?: string | null;
}) {
  const copy = {
    "waiting-for-service": {
      title: "正在确认私有工作服务",
      detail: "服务就绪后会继续你刚才的开始对话操作。",
    },
    "waiting-for-agent": {
      title: "开始对话意图已保留",
      detail: "完成 Agent 配置后将自动创建对话。",
    },
    "creating-chat": {
      title: "Agent 已可用",
      detail: "正在创建你的首个对话…",
    },
    "read-only": {
      title: "当前为只读访问",
      detail: "你可以查看 Agent，但不能创建新的私有对话。",
    },
    error: {
      title: "自动创建对话失败",
      detail: "Agent 已完成配置，请重试进入对话。",
    },
  } satisfies Record<
    ProjectAgentStartContinuationStatus,
    { title: string; detail: string }
  >;
  const content =
    status === "error" && errorMessage
      ? { ...copy.error, detail: errorMessage }
      : copy[status];
  return (
    <section
      role={status === "error" ? "alert" : "status"}
      className="border-primary/25 bg-primary/5 mb-6 rounded-xl border p-4"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="font-medium">{content.title}</h2>
          <p className="text-muted-foreground mt-1 text-sm">{content.detail}</p>
        </div>
        {status === "error" && onRetry && (
          <Button type="button" size="sm" onClick={onRetry}>
            重试进入对话
          </Button>
        )}
      </div>
    </section>
  );
}

export function ProjectAgentStartContinuation({
  project,
  catalog,
  requested,
  intentId,
}: {
  project: Project;
  catalog: ProjectAssetList;
  requested: boolean;
  intentId: string | null;
}) {
  const router = useRouter();
  const privateWork = usePrivateWorkAccess();
  const canCreate =
    project.capabilities.includes("private_work.create") &&
    project.capabilities.includes("shared_assets.execute");
  const readiness = useProjectPrivateWorkReadiness(requested && canCreate);
  const defaultAgent = useProjectDefaultAgent(
    privateWork.scope.accountId,
    project.id,
    requested && canCreate,
  );
  const refetchDefaultAgent = defaultAgent.refetch;
  const defaultResolution = useMemo(
    () => resolveProjectDefaultAgent(catalog, defaultAgent.data),
    [catalog, defaultAgent.data],
  );
  const candidate = useMemo(
    () =>
      projectStartChatCandidate(catalog, defaultAgent.data, {
        requested,
        canCreate,
        readinessStatus: readiness.data?.status,
      }),
    [canCreate, catalog, defaultAgent.data, readiness.data?.status, requested],
  );
  const customAgent =
    defaultResolution.status === "ready" &&
    defaultResolution.source === "project"
      ? defaultResolution.agent
      : null;
  const customAgentDependencies = useAgentMcpDependencyRuntime({
    accountId: privateWork.scope.accountId,
    projectId: project.id,
    agents: customAgent ? [customAgent] : [],
    enabled: Boolean(candidate && customAgent),
  });
  const customAgentAssessment = customAgentDependencies.assessments[0];
  const candidateReady =
    Boolean(candidate) &&
    (!customAgent || customAgentAssessment?.status === "ready");
  const [retry, setRetry] = useState(0);
  const [failure, setFailure] = useState<string | null>(null);
  const startedKeyRef = useRef<string | null>(null);
  const fallbackIntentIdRef = useRef<string | null>(null);
  fallbackIntentIdRef.current ??= `${privateWork.scope.accountId}:${privateWork.scope.projectId}:legacy`;
  const stableIntentId = intentId ?? fallbackIntentIdRef.current;
  const candidateKey =
    candidate && candidateReady
      ? `${stableIntentId}:${candidate.scope}:${candidate.id}:${retry}`
      : null;

  useEffect(() => {
    if (!candidate || !candidateKey || startedKeyRef.current === candidateKey) {
      return;
    }
    startedKeyRef.current = candidateKey;
    setFailure(null);
    void consumeProjectStartChatIntent({
      scope: privateWork.scope,
      projectSlug: project.slug,
      intentId: stableIntentId,
      replace: (path) => router.replace(path),
    }).catch(async (error) =>
      setFailure(
        await projectNewChatErrorMessage(
          error,
          () => refetchDefaultAgent(),
          "Agent 已完成配置，请重试进入对话。",
        ),
      ),
    );
  }, [
    candidate,
    candidateKey,
    defaultResolution,
    privateWork.scope,
    project.slug,
    refetchDefaultAgent,
    router,
    stableIntentId,
  ]);

  if (!requested) return null;
  if (!canCreate) {
    return <ProjectAgentStartContinuationView status="read-only" />;
  }
  if (readiness.isLoading || readiness.data?.status !== "ready") {
    return <ProjectAgentStartContinuationView status="waiting-for-service" />;
  }
  if (defaultAgent.error) {
    return (
      <ProjectAgentStartContinuationView
        status="error"
        errorMessage="无法加载项目默认 Agent，请稍后重试。"
        onRetry={() => void defaultAgent.refetch()}
      />
    );
  }
  if (defaultAgent.isLoading) {
    return <ProjectAgentStartContinuationView status="waiting-for-agent" />;
  }
  if (defaultResolution.status === "unavailable") {
    return (
      <ProjectAgentStartContinuationView
        status="error"
        errorMessage={defaultResolution.reason}
        onRetry={() => void defaultAgent.refetch()}
      />
    );
  }
  if (
    customAgentDependencies.error ||
    customAgentAssessment?.status === "blocked"
  ) {
    return (
      <ProjectAgentStartContinuationView
        status="error"
        errorMessage={
          customAgentAssessment?.reason ??
          "无法验证项目默认 Agent 的运行依赖，请稍后重试。"
        }
        onRetry={() => setRetry((value) => value + 1)}
      />
    );
  }
  if (customAgentDependencies.isLoading) {
    return <ProjectAgentStartContinuationView status="waiting-for-agent" />;
  }
  if (!candidate) {
    return <ProjectAgentStartContinuationView status="waiting-for-agent" />;
  }
  if (failure) {
    return (
      <ProjectAgentStartContinuationView
        status="error"
        errorMessage={failure}
        onRetry={() => {
          setFailure(null);
          setRetry((value) => value + 1);
        }}
      />
    );
  }
  return <ProjectAgentStartContinuationView status="creating-chat" />;
}

"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircleIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import {
  useAgentMcpDependencyRuntime,
  type AgentMcpDependencyAssessment,
} from "@/components/projects/assets/use-mcp-dependency-runtime";
import {
  canManageProjectChannels,
  ChannelInstanceCard,
  ChannelInstanceConfigDialog,
  projectChannelConfigErrorMessage,
} from "@/components/projects/private-work/project-channel-config";
import {
  ProjectChannelGroupBindings,
  type ProjectChannelGroupAgentOption,
} from "@/components/projects/private-work/project-channel-group-bindings";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  clearProjectChannelInstanceSecret,
  configureProjectChannelInstance,
  deleteProjectChannelInstance,
  listProjectChannelInstances,
  projectChannelInstancesQueryKey,
  setProjectChannelInstanceEnabled,
  type ConfigureProjectChannelInstanceInput,
  type ProjectChannelInstance,
} from "@/core/private-work/connections";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { runPrivateWorkAbortable } from "@/core/private-work/types";
import type { Project } from "@/core/projects/types";
import { useProjectAssets, type ProjectAssetList } from "@/core/shared-assets";

import {
  executableProjectAgents,
  mainProjectAgent,
  type ExecutableProjectAgent,
} from "./agent-selector-dialog";

/** Project channel management currently exposes Feishu only; other providers stay API-capable. */
export const VISIBLE_PROJECT_CHANNEL_PROVIDERS = ["feishu"] as const;

export function isVisibleProjectChannelProvider(provider: string): boolean {
  return (VISIBLE_PROJECT_CHANNEL_PROVIDERS as readonly string[]).includes(
    provider,
  );
}

export function connectionAgentRuntimeOptions(
  agents: readonly ExecutableProjectAgent[],
  assessments: readonly AgentMcpDependencyAssessment[],
): {
  readyAgents: ExecutableProjectAgent[];
  blockedAgents: ExecutableProjectAgent[];
} {
  return {
    readyAgents: agents.filter(
      (_agent, index) => assessments[index]?.status === "ready",
    ),
    blockedAgents: agents.filter(
      (_agent, index) => assessments[index]?.status === "blocked",
    ),
  };
}

export function groupBindingChannelAvailability(
  instance:
    | Pick<
        ProjectChannelInstance,
        "provider" | "configured" | "enabled" | "status"
      >
    | null
    | undefined,
): { enabled: boolean; reason: string | null } {
  if (!instance?.configured) {
    return { enabled: false, reason: "请先配置飞书渠道。" };
  }
  if (!instance.enabled) {
    return { enabled: false, reason: "请先启用飞书渠道。" };
  }
  if (instance.status !== "running") {
    return {
      enabled: false,
      reason: "飞书渠道未运行，暂时无法绑定群聊。",
    };
  }
  return { enabled: true, reason: null };
}

export function groupBindingAgentOptions(
  availableAgents: readonly ExecutableProjectAgent[],
  unavailableAgents: readonly {
    agent: ExecutableProjectAgent;
    reason: string | null;
  }[],
): ProjectChannelGroupAgentOption[] {
  const options: ProjectChannelGroupAgentOption[] = availableAgents.map(
    (agent) => ({
      id: agent.id,
      scope: agent.scope,
      displayName: agent.display_name,
      available: true,
      unavailableReason: null,
    }),
  );
  const keys = new Set(options.map((option) => `${option.scope}:${option.id}`));
  for (const { agent, reason } of unavailableAgents) {
    const key = `${agent.scope}:${agent.id}`;
    if (keys.has(key)) continue;
    options.push({
      id: agent.id,
      scope: agent.scope,
      displayName: agent.display_name,
      available: false,
      unavailableReason: reason,
    });
    keys.add(key);
  }
  return options;
}

export function connectionAgentChoices(
  catalog: ProjectAssetList | undefined,
  readyAgents: readonly ExecutableProjectAgent[],
): ExecutableProjectAgent[] {
  const choices = [...readyAgents];
  const main = mainProjectAgent(catalog);
  if (
    main &&
    !choices.some(
      (candidate) => candidate.id === main.id && candidate.scope === main.scope,
    )
  ) {
    choices.unshift(main);
  }
  return choices;
}

export function ProjectConnectionsPage({ project }: { project: Project }) {
  const { user } = useAuth();
  const privateWork = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  const scope = privateWork.scope;
  const canManageChannels = canManageProjectChannels(project.capabilities);
  const channelInstancesKey = projectChannelInstancesQueryKey(scope);
  const channelInstances = useQuery({
    queryKey: channelInstancesKey,
    queryFn: ({ signal }) => listProjectChannelInstances(privateWork, signal),
    enabled: canManageChannels,
  });
  const agentCatalogEnabled = canManageChannels && Boolean(user);
  const agentsQuery = useProjectAssets(
    user?.id ?? "",
    project.id,
    "agents",
    agentCatalogEnabled,
  );
  const agentCatalog = agentsQuery.data;
  const agents = useMemo(
    () => executableProjectAgents(agentCatalog),
    [agentCatalog],
  );
  const mcpDependencyRuntime = useAgentMcpDependencyRuntime({
    accountId: user?.id ?? "",
    projectId: project.id,
    agents,
    enabled: agentCatalogEnabled,
  });
  const { readyAgents, blockedAgents } = connectionAgentRuntimeOptions(
    agents,
    mcpDependencyRuntime.assessments,
  );
  const selectableAgents = useMemo(
    () => connectionAgentChoices(agentCatalog, readyAgents),
    [agentCatalog, readyAgents],
  );
  const selectableAgentKeys = new Set(
    selectableAgents.map((agent) => `${agent.scope}:${agent.id}`),
  );
  const unavailableAgents = blockedAgents.filter(
    (agent) => !selectableAgentKeys.has(`${agent.scope}:${agent.id}`),
  );
  const groupBindingAgents = useMemo(
    () =>
      groupBindingAgentOptions(
        selectableAgents,
        unavailableAgents.map((agent) => {
          const index = agents.findIndex(
            (candidate) =>
              candidate.id === agent.id && candidate.scope === agent.scope,
          );
          return {
            agent,
            reason:
              mcpDependencyRuntime.assessments[index]?.reason ??
              "Agent 当前不可用",
          };
        }),
      ),
    [
      agents,
      mcpDependencyRuntime.assessments,
      selectableAgents,
      unavailableAgents,
    ],
  );
  const [configTarget, setConfigTarget] =
    useState<ProjectChannelInstance | null>(null);
  const [deleteTarget, setDeleteTarget] =
    useState<ProjectChannelInstance | null>(null);
  const [clearSecretTarget, setClearSecretTarget] =
    useState<ProjectChannelInstance | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [channelPending, setChannelPending] = useState<{
    provider: string;
    action: "configure" | "enable" | "disable" | "clear-secret" | "delete";
  } | null>(null);
  const visibleChannelInstances = (
    channelInstances.data?.instances ?? []
  ).filter((instance) => isVisibleProjectChannelProvider(instance.provider));
  const feishuInstance = visibleChannelInstances.find(
    (instance) => instance.provider === "feishu",
  );
  const groupBindingAvailability =
    groupBindingChannelAvailability(feishuInstance);
  const refreshChannelState = async () => {
    await queryClient.invalidateQueries({ queryKey: channelInstancesKey });
  };

  const handleConfigureChannel = async (
    instance: ProjectChannelInstance,
    input: ConfigureProjectChannelInstanceInput,
  ) => {
    if (channelPending || !canManageChannels) return;
    setConfigError(null);
    setChannelPending({ provider: instance.provider, action: "configure" });
    try {
      await runPrivateWorkAbortable(privateWork, (signal) =>
        configureProjectChannelInstance(
          privateWork,
          instance.provider,
          input,
          signal,
        ),
      );
      await refreshChannelState();
      setConfigTarget(null);
      toast.success(`${instance.display_name} 配置已保存`);
    } catch (error) {
      setConfigError(
        projectChannelConfigErrorMessage(instance.provider, error),
      );
    } finally {
      setChannelPending(null);
    }
  };

  const handleToggleChannel = async (
    instance: ProjectChannelInstance,
    enabled: boolean,
  ) => {
    if (channelPending || !canManageChannels) {
      return;
    }
    setChannelPending({
      provider: instance.provider,
      action: enabled ? "enable" : "disable",
    });
    try {
      await runPrivateWorkAbortable(privateWork, (signal) =>
        setProjectChannelInstanceEnabled(
          privateWork,
          instance.provider,
          enabled,
          signal,
        ),
      );
      await refreshChannelState();
      toast.success(`${instance.display_name} 已${enabled ? "启用" : "停用"}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "无法更新渠道状态");
    } finally {
      setChannelPending(null);
    }
  };

  const handleDeleteChannel = async () => {
    const instance = deleteTarget;
    if (!instance || channelPending || !canManageChannels) {
      return;
    }
    setChannelPending({ provider: instance.provider, action: "delete" });
    try {
      await runPrivateWorkAbortable(privateWork, (signal) =>
        deleteProjectChannelInstance(privateWork, instance.provider, signal),
      );
      await refreshChannelState();
      setDeleteTarget(null);
      toast.success(`${instance.display_name} 已删除`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "无法删除渠道配置");
    } finally {
      setChannelPending(null);
    }
  };

  const handleClearChannelSecret = async () => {
    const instance = clearSecretTarget;
    if (!instance || channelPending || !canManageChannels) return;
    setChannelPending({ provider: instance.provider, action: "clear-secret" });
    try {
      await runPrivateWorkAbortable(privateWork, (signal) =>
        clearProjectChannelInstanceSecret(
          privateWork,
          instance.provider,
          signal,
        ),
      );
      await refreshChannelState();
      setClearSecretTarget(null);
      toast.success(`${instance.display_name} 的秘密已清除`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "无法清除渠道秘密");
    } finally {
      setChannelPending(null);
    }
  };

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 p-6 lg:p-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">渠道连接</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          由项目管理员配置渠道实例、运行状态和群聊 Agent 绑定。
        </p>
      </header>

      <section className="space-y-4" aria-labelledby="channel-config-title">
        <h2 id="channel-config-title" className="text-lg font-semibold">
          渠道配置
        </h2>
        {channelInstances.isLoading ? (
          <p role="status" className="text-muted-foreground text-sm">
            正在加载渠道配置…
          </p>
        ) : channelInstances.error ? (
          <div className="rounded-xl border p-5">
            <p role="alert" className="text-destructive text-sm">
              {channelInstances.error instanceof Error
                ? `无法加载渠道配置：${channelInstances.error.message}`
                : "无法加载渠道配置。"}
            </p>
            <Button
              type="button"
              variant="outline"
              className="mt-4"
              onClick={() => void channelInstances.refetch()}
            >
              重试
            </Button>
          </div>
        ) : visibleChannelInstances.length === 0 ? (
          <p className="text-muted-foreground rounded-xl border border-dashed p-5 text-sm">
            暂无可配置渠道。
          </p>
        ) : (
          <ul className="grid gap-3 md:grid-cols-2">
            {visibleChannelInstances.map((instance) => (
              <ChannelInstanceCard
                key={instance.provider}
                instance={instance}
                manageable={canManageChannels}
                pendingAction={
                  channelPending?.provider === instance.provider
                    ? channelPending.action
                    : null
                }
                onConfigure={(target) => {
                  setConfigError(null);
                  setConfigTarget(target);
                }}
                onToggle={(target, enabled) =>
                  void handleToggleChannel(target, enabled)
                }
                onClearSecret={setClearSecretTarget}
                onDelete={setDeleteTarget}
              />
            ))}
          </ul>
        )}
        {feishuInstance && canManageChannels ? (
          <div className="rounded-xl border p-4">
            <ProjectChannelGroupBindings
              provider="feishu"
              agents={groupBindingAgents}
              manageable={canManageChannels}
              bindingEnabled={groupBindingAvailability.enabled}
              bindingBlockedReason={groupBindingAvailability.reason}
            />
          </div>
        ) : null}
      </section>

      <ChannelInstanceConfigDialog
        instance={configTarget}
        pending={
          configTarget !== null &&
          channelPending?.provider === configTarget.provider &&
          channelPending.action === "configure"
        }
        errorMessage={configError}
        onOpenChange={(open) => {
          if (!open && channelPending?.action !== "configure") {
            setConfigError(null);
            setConfigTarget(null);
          }
        }}
        onSubmit={(instance, input) => handleConfigureChannel(instance, input)}
      />

      <Dialog
        open={clearSecretTarget !== null}
        onOpenChange={(open) => {
          if (!open && channelPending?.action !== "clear-secret") {
            setClearSecretTarget(null);
          }
        }}
      >
        <DialogContent closeLabel="关闭">
          <DialogHeader>
            <DialogTitle>清除渠道秘密</DialogTitle>
            <DialogDescription>
              清除 {clearSecretTarget?.display_name ?? "该渠道"}{" "}
              的秘密值？清除后渠道将变为未就绪，必须重新配置才能启用。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={channelPending?.action === "clear-secret"}
              onClick={() => setClearSecretTarget(null)}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={channelPending?.action === "clear-secret"}
              onClick={() => void handleClearChannelSecret()}
            >
              {channelPending?.action === "clear-secret" ? (
                <LoaderCircleIcon className="animate-spin" />
              ) : null}
              确认清除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open && channelPending?.action !== "delete") {
            setDeleteTarget(null);
          }
        }}
      >
        <DialogContent closeLabel="关闭">
          <DialogHeader>
            <DialogTitle>删除渠道配置</DialogTitle>
            <DialogDescription>
              删除 {deleteTarget?.display_name ?? "该渠道"} 的项目配置？
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={channelPending?.action === "delete"}
              onClick={() => setDeleteTarget(null)}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={channelPending?.action === "delete"}
              onClick={() => void handleDeleteChannel()}
            >
              {channelPending?.action === "delete" ? (
                <LoaderCircleIcon className="animate-spin" />
              ) : null}
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}

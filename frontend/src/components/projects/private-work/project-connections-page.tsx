"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CopyIcon, LoaderCircleIcon, UnplugIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import {
  useAgentMcpDependencyRuntime,
  type AgentMcpDependencyAssessment,
} from "@/components/projects/assets/use-mcp-dependency-runtime";
import { ChannelProviderIcon } from "@/components/projects/private-work/channel-provider-icon";
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
import { writeTextToClipboard } from "@/core/clipboard";
import { startConnectionPoll } from "@/core/private-work/connect-poll";
import {
  labelOfChannelProvider,
  type ChannelProviderId,
} from "@/core/private-work/connection-types";
import {
  connectProjectConnection,
  configureProjectChannelInstance,
  deleteProjectChannelInstance,
  disconnectProjectConnection,
  listProjectChannelInstances,
  listProjectConnectionProviders,
  listProjectConnections,
  projectChannelInstancesQueryKey,
  projectConnectionProvidersQueryKey,
  projectConnectionsQueryKey,
  setProjectChannelInstanceEnabled,
  type ConfigureProjectChannelInstanceInput,
  type ProjectChannelInstance,
} from "@/core/private-work/connections";
import {
  closeConnectWindow,
  openConnectUrl,
  prepareConnectWindow,
  type ChannelConnectWindow,
} from "@/core/private-work/open-connect-url";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { runPrivateWorkAbortable } from "@/core/private-work/types";
import type { Project } from "@/core/projects/types";
import { useProjectAssets, type ProjectAssetList } from "@/core/shared-assets";

import {
  AgentSelectorDialog,
  executableProjectAgents,
  mainProjectAgent,
  projectThreadAgentSelection,
  type ExecutableProjectAgent,
} from "./agent-selector-dialog";

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

export function prepareProviderConnectWindow(
  authMode: "deep_link" | "binding_code" | undefined,
  prepare: () => ChannelConnectWindow = prepareConnectWindow,
): ChannelConnectWindow {
  return authMode === "deep_link" ? prepare() : null;
}

export function bindingCodeCommand(code: string): string {
  return `/connect ${code.trim()}`;
}

export function bindingCodeExpiryLabel(expiresIn: number): string {
  if (!Number.isFinite(expiresIn) || expiresIn <= 0) {
    return "连接码已失效";
  }
  if (expiresIn < 60) {
    return `${Math.ceil(expiresIn)} 秒后失效`;
  }
  return `${Math.ceil(expiresIn / 60)} 分钟内有效`;
}

interface BindingCodeGuide {
  provider: ChannelProviderId;
  code: string;
  expiresAt: number;
}

type BindingGuideStatus = "pending" | "not_found" | "connected";

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
  const scopeAccountId = scope.accountId;
  const scopeProjectId = scope.projectId;
  const canConnect = project.capabilities.includes("private_work.create");
  const canManageChannels = canManageProjectChannels(project.capabilities);
  const queryKey = useMemo(
    () =>
      projectConnectionsQueryKey({
        accountId: scopeAccountId,
        projectId: scopeProjectId,
      }),
    [scopeAccountId, scopeProjectId],
  );
  const providersQueryKey = useMemo(
    () =>
      projectConnectionProvidersQueryKey({
        accountId: scopeAccountId,
        projectId: scopeProjectId,
      }),
    [scopeAccountId, scopeProjectId],
  );
  const connections = useQuery({
    queryKey,
    queryFn: ({ signal }) => listProjectConnections(privateWork, signal),
  });
  const providers = useQuery({
    queryKey: providersQueryKey,
    queryFn: ({ signal }) =>
      listProjectConnectionProviders(privateWork, signal),
  });
  const channelInstancesKey = projectChannelInstancesQueryKey(scope);
  const channelInstances = useQuery({
    queryKey: channelInstancesKey,
    queryFn: ({ signal }) => listProjectChannelInstances(privateWork, signal),
  });
  const agentCatalogEnabled =
    (canConnect || canManageChannels) && Boolean(user);
  const agentsQuery = useProjectAssets(
    user?.id ?? "",
    project.id,
    "agents",
    agentCatalogEnabled,
  );
  const agentCatalog = agentsQuery.data as ProjectAssetList | undefined;
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
  const [selectedProvider, setSelectedProvider] =
    useState<ChannelProviderId | null>(null);
  const [pendingProvider, setPendingProvider] =
    useState<ChannelProviderId | null>(null);
  const [configTarget, setConfigTarget] =
    useState<ProjectChannelInstance | null>(null);
  const [deleteTarget, setDeleteTarget] =
    useState<ProjectChannelInstance | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [bindingCodeGuide, setBindingCodeGuide] =
    useState<BindingCodeGuide | null>(null);
  const [bindingGuideOpen, setBindingGuideOpen] = useState(false);
  const [bindingGuideStatus, setBindingGuideStatus] =
    useState<BindingGuideStatus>("pending");
  const [bindingClock, setBindingClock] = useState(() => Date.now());
  const [bindingCheckMessage, setBindingCheckMessage] = useState<string | null>(
    null,
  );
  const [checkingBinding, setCheckingBinding] = useState(false);
  const [channelPending, setChannelPending] = useState<{
    provider: string;
    action: "configure" | "enable" | "disable" | "delete";
  } | null>(null);
  const providerStates = providers.data?.providers ?? [];
  const feishuInstance = channelInstances.data?.instances.find(
    (instance) => instance.provider === "feishu",
  );
  const groupBindingAvailability =
    groupBindingChannelAvailability(feishuInstance);
  const bindingSecondsRemaining = bindingCodeGuide
    ? Math.max(0, Math.ceil((bindingCodeGuide.expiresAt - bindingClock) / 1000))
    : 0;
  const bindingExpired =
    bindingCodeGuide !== null &&
    bindingGuideStatus !== "connected" &&
    bindingSecondsRemaining <= 0;

  useEffect(() => {
    if (!bindingCodeGuide) return;
    const timer = window.setInterval(() => setBindingClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [bindingCodeGuide]);

  useEffect(() => {
    const guide = bindingCodeGuide;
    if (!guide || bindingGuideStatus === "connected") return;
    const expiresInSeconds = Math.ceil((guide.expiresAt - Date.now()) / 1000);
    if (expiresInSeconds <= 0) return;

    const poll = startConnectionPoll({
      provider: guide.provider,
      expiresInSeconds,
      fetchConnections: async () => {
        const latest = await runPrivateWorkAbortable(privateWork, (signal) =>
          listProjectConnections(privateWork, signal),
        );
        queryClient.setQueryData(queryKey, latest);
        return latest;
      },
      onConnected: () => {
        setBindingGuideStatus("connected");
        setBindingCheckMessage(null);
        setBindingGuideOpen(true);
        toast.success(`${labelOfChannelProvider(guide.provider)} 已连接`);
        void queryClient.invalidateQueries({
          queryKey: providersQueryKey,
        });
      },
    });
    return poll.cancel;
  }, [
    bindingCodeGuide,
    bindingGuideStatus,
    privateWork,
    providersQueryKey,
    queryClient,
    queryKey,
  ]);

  const refreshChannelState = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: channelInstancesKey }),
      queryClient.invalidateQueries({ queryKey: providersQueryKey }),
      queryClient.invalidateQueries({ queryKey }),
    ]);
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

  const handleAgentSelect = async (agent: ExecutableProjectAgent) => {
    const provider = selectedProvider;
    const index = agents.findIndex(
      (candidate) =>
        candidate.id === agent.id && candidate.scope === agent.scope,
    );
    if (
      !provider ||
      pendingProvider ||
      mcpDependencyRuntime.assessments[index]?.status !== "ready"
    ) {
      return;
    }
    const connectWindow = prepareProviderConnectWindow(
      providerStates.find((state) => state.provider === provider)?.auth_mode,
    );
    setPendingProvider(provider);
    try {
      const result = await connectProjectConnection(
        privateWork,
        provider,
        projectThreadAgentSelection(agent),
      );
      if (result.url) {
        openConnectUrl(result.url, connectWindow);
      } else if (result.mode === "binding_code") {
        closeConnectWindow(connectWindow);
        const now = Date.now();
        setBindingClock(now);
        setBindingCheckMessage(null);
        setBindingGuideStatus("pending");
        setBindingCodeGuide({
          provider: result.provider,
          code: result.code,
          expiresAt: now + result.expires_in * 1000,
        });
        setBindingGuideOpen(true);
      } else {
        closeConnectWindow(connectWindow);
        toast.success(result.instruction);
      }
      await queryClient.invalidateQueries({ queryKey });
    } catch (error) {
      closeConnectWindow(connectWindow);
      toast.error(error instanceof Error ? error.message : "无法连接项目渠道");
    } finally {
      setPendingProvider(null);
      setSelectedProvider(null);
    }
  };

  const handleCopyBindingCommand = async () => {
    if (!bindingCodeGuide) return;
    const copied = await writeTextToClipboard(
      bindingCodeCommand(bindingCodeGuide.code),
    );
    if (copied) {
      toast.success("连接命令已复制");
      return;
    }
    setBindingCheckMessage("复制失败，请手动复制连接命令。");
  };

  const handleCheckBinding = async () => {
    if (!bindingCodeGuide || checkingBinding) return;
    if (bindingCodeGuide.expiresAt <= Date.now()) {
      setBindingClock(Date.now());
      setBindingCheckMessage("连接码已失效，请重新生成连接码。");
      return;
    }
    setCheckingBinding(true);
    setBindingCheckMessage(null);
    try {
      const latest = await runPrivateWorkAbortable(privateWork, (signal) =>
        listProjectConnections(privateWork, signal),
      );
      queryClient.setQueryData(queryKey, latest);
      const connected = latest.some(
        (connection) =>
          connection.provider === bindingCodeGuide.provider &&
          connection.status === "connected",
      );
      if (connected) {
        setBindingGuideStatus("connected");
        toast.success(
          `${labelOfChannelProvider(bindingCodeGuide.provider)} 已连接`,
        );
        await queryClient.invalidateQueries({ queryKey: providersQueryKey });
      } else {
        setBindingGuideStatus("not_found");
        setBindingCheckMessage(
          `尚未检测到连接。请先在 ${labelOfChannelProvider(bindingCodeGuide.provider)} 中发送命令，再重新检查。`,
        );
      }
    } catch {
      setBindingCheckMessage("暂时无法检查连接，请稍后重试。");
    } finally {
      setCheckingBinding(false);
    }
  };

  const handleDisconnect = async (connectionId: string, provider: string) => {
    if (pendingProvider) return;
    setPendingProvider(provider);
    try {
      await disconnectProjectConnection(privateWork, connectionId);
      await queryClient.invalidateQueries({ queryKey });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "无法断开项目渠道");
    } finally {
      setPendingProvider(null);
      setSelectedProvider(null);
    }
  };

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 p-6 lg:p-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">渠道连接</h1>
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
        ) : (channelInstances.data?.instances.length ?? 0) === 0 ? (
          <p className="text-muted-foreground rounded-xl border border-dashed p-5 text-sm">
            暂无可配置渠道。
          </p>
        ) : (
          <ul className="grid gap-3 md:grid-cols-2">
            {channelInstances.data?.instances.map((instance) => (
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

      <section className="space-y-4" aria-labelledby="my-connections-title">
        <h2 id="my-connections-title" className="text-lg font-semibold">
          我的连接
        </h2>
        {connections.isLoading || providers.isLoading ? (
          <p role="status" className="text-muted-foreground text-sm">
            正在加载渠道连接…
          </p>
        ) : connections.error || providers.error ? (
          <div className="rounded-xl border p-5">
            <p role="alert" className="text-destructive text-sm">
              {connections.error instanceof Error
                ? `无法加载渠道连接：${connections.error.message}`
                : providers.error instanceof Error
                  ? `无法加载渠道连接：${providers.error.message}`
                  : "无法加载渠道连接。"}
            </p>
            <Button
              type="button"
              variant="outline"
              className="mt-4"
              onClick={() => {
                void connections.refetch();
                void providers.refetch();
              }}
            >
              重试
            </Button>
          </div>
        ) : providers.data?.enabled === false ? (
          <p className="text-muted-foreground rounded-xl border border-dashed p-5 text-sm">
            渠道连接不可用。
          </p>
        ) : providerStates.length === 0 ? (
          <p className="text-muted-foreground rounded-xl border border-dashed p-5 text-sm">
            暂无可用渠道。
          </p>
        ) : (
          <ul className="grid gap-3 md:grid-cols-2">
            {providerStates.map((providerState) => {
              const provider = providerState.provider as ChannelProviderId;
              const connection = connections.data?.find(
                (item) =>
                  item.provider === provider && item.status === "connected",
              );
              const isPending = pendingProvider === provider;
              const canResumeBinding =
                bindingCodeGuide?.provider === provider &&
                bindingGuideStatus !== "connected" &&
                !bindingExpired;
              return (
                <li
                  key={provider}
                  className="bg-card flex items-center gap-3 rounded-xl border p-4"
                >
                  <ChannelProviderIcon provider={provider} className="size-6" />
                  <div className="min-w-0 flex-1">
                    <p className="font-medium">
                      {labelOfChannelProvider(provider)}
                    </p>
                    <p className="text-muted-foreground truncate text-xs">
                      {connection
                        ? (connection.external_account_name ??
                          connection.workspace_name ??
                          "已连接")
                        : "未连接"}
                    </p>
                    {!providerState.connectable ? (
                      <p className="text-muted-foreground truncate text-xs">
                        {providerState.unavailable_reason ?? "渠道当前不可用"}
                      </p>
                    ) : null}
                  </div>
                  {canConnect && providerState.connectable ? (
                    connection ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={isPending}
                        onClick={() =>
                          void handleDisconnect(connection.id, provider)
                        }
                      >
                        {isPending ? (
                          <LoaderCircleIcon className="animate-spin" />
                        ) : (
                          <UnplugIcon />
                        )}
                        断开
                      </Button>
                    ) : (
                      <Button
                        type="button"
                        size="sm"
                        disabled={isPending}
                        onClick={() => {
                          if (canResumeBinding) {
                            setBindingGuideOpen(true);
                            return;
                          }
                          if (bindingCodeGuide?.provider === provider) {
                            setBindingCodeGuide(null);
                            setBindingCheckMessage(null);
                          }
                          setSelectedProvider(provider);
                        }}
                      >
                        {isPending ? (
                          <LoaderCircleIcon className="animate-spin" />
                        ) : null}
                        {canResumeBinding ? "继续连接" : "连接"}
                      </Button>
                    )
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
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

      <Dialog
        open={bindingGuideOpen && bindingCodeGuide !== null}
        onOpenChange={(open) => {
          if (!checkingBinding) setBindingGuideOpen(open);
        }}
      >
        <DialogContent className="min-w-0" closeLabel="关闭连接指引">
          <DialogHeader>
            <DialogTitle>
              {bindingCodeGuide
                ? `完成 ${labelOfChannelProvider(bindingCodeGuide.provider)} 连接`
                : "完成连接"}
            </DialogTitle>
            <DialogDescription>
              {bindingCodeGuide
                ? `在当前项目的 ${labelOfChannelProvider(bindingCodeGuide.provider)} 机器人会话中完成绑定。`
                : "完成当前渠道绑定。"}
            </DialogDescription>
          </DialogHeader>

          {bindingCodeGuide && bindingGuideStatus === "connected" ? (
            <div
              role="status"
              className="rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-emerald-950 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-100"
            >
              <p className="font-medium">连接成功</p>
              <p className="mt-1 text-sm">
                {labelOfChannelProvider(bindingCodeGuide.provider)}
                已连接到当前项目。
              </p>
            </div>
          ) : bindingCodeGuide && bindingExpired ? (
            <div role="alert" className="bg-muted rounded-lg border p-4">
              <p className="font-medium">连接码已失效</p>
              <p className="text-muted-foreground mt-1 text-sm">
                请重新选择 Agent 生成新的连接码。
              </p>
            </div>
          ) : bindingCodeGuide ? (
            <div className="min-w-0 space-y-4">
              <div className="bg-muted flex min-w-0 flex-col gap-3 rounded-lg border p-3 sm:flex-row sm:items-center">
                <code className="min-w-0 flex-1 font-mono text-sm leading-5 font-medium break-all">
                  {bindingCodeCommand(bindingCodeGuide.code)}
                </code>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="shrink-0 self-end sm:self-auto"
                  onClick={() => void handleCopyBindingCommand()}
                >
                  <CopyIcon aria-hidden />
                  复制命令
                </Button>
              </div>
              <ol className="text-muted-foreground list-decimal space-y-1 pl-5 text-sm">
                <li>复制上面的连接命令。</li>
                <li>
                  在当前项目的
                  {` ${labelOfChannelProvider(bindingCodeGuide.provider)} `}
                  机器人会话中发送该命令。
                </li>
                <li>
                  看到机器人回复“
                  {labelOfChannelProvider(bindingCodeGuide.provider)} connected
                  to ActWeave.”后，返回这里检查连接。
                </li>
              </ol>
              <p className="text-muted-foreground text-xs">
                {bindingCodeExpiryLabel(bindingSecondsRemaining)}
              </p>
              {bindingCheckMessage ? (
                <p role="status" className="text-sm">
                  {bindingCheckMessage}
                </p>
              ) : null}
            </div>
          ) : null}

          <DialogFooter>
            {bindingGuideStatus === "connected" ? (
              <Button
                type="button"
                onClick={() => {
                  setBindingGuideOpen(false);
                  setBindingCodeGuide(null);
                  setBindingGuideStatus("pending");
                }}
              >
                完成
              </Button>
            ) : bindingExpired ? (
              <>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setBindingGuideOpen(false)}
                >
                  关闭
                </Button>
                <Button
                  type="button"
                  onClick={() => {
                    const provider = bindingCodeGuide?.provider ?? null;
                    setBindingGuideOpen(false);
                    setBindingCodeGuide(null);
                    setBindingCheckMessage(null);
                    setBindingGuideStatus("pending");
                    if (provider) setSelectedProvider(provider);
                  }}
                >
                  重新生成连接码
                </Button>
              </>
            ) : (
              <>
                <Button
                  type="button"
                  variant="outline"
                  disabled={checkingBinding}
                  onClick={() => setBindingGuideOpen(false)}
                >
                  稍后完成
                </Button>
                <Button
                  type="button"
                  disabled={checkingBinding}
                  onClick={() => void handleCheckBinding()}
                >
                  {checkingBinding ? (
                    <LoaderCircleIcon aria-hidden className="animate-spin" />
                  ) : null}
                  {bindingGuideStatus === "not_found"
                    ? "重新检查"
                    : "我已发送，检查连接"}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AgentSelectorDialog
        open={selectedProvider !== null}
        agents={selectableAgents}
        blockedRuntimeAgents={unavailableAgents}
        isCreating={pendingProvider !== null}
        isLoading={agentsQuery.isLoading || mcpDependencyRuntime.isLoading}
        error={
          agentsQuery.error ??
          (mcpDependencyRuntime.error instanceof Error
            ? mcpDependencyRuntime.error
            : mcpDependencyRuntime.error
              ? new Error("无法验证 Agent 的 MCP 依赖")
              : null)
        }
        onOpenChange={(open) => {
          if (!open && pendingProvider === null) setSelectedProvider(null);
        }}
        onSelect={(agent) => void handleAgentSelect(agent)}
      />
    </main>
  );
}

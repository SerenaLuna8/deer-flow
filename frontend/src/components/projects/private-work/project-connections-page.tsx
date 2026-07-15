"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircleIcon, UnplugIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { ChannelProviderIcon } from "@/components/workspace/channels/channel-provider-icon";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  closeConnectWindow,
  openConnectUrl,
  prepareConnectWindow,
} from "@/core/channels/open-connect-url";
import {
  CHANNEL_PROVIDER_IDS,
  labelOfChannelProvider,
  type ChannelProviderId,
} from "@/core/channels/types";
import {
  connectProjectConnection,
  disconnectProjectConnection,
  listProjectConnections,
  projectConnectionsQueryKey,
} from "@/core/private-work/connections";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { Project } from "@/core/projects/types";
import { useProjectAssets, type ProjectAssetList } from "@/core/shared-assets";

import {
  AgentSelectorDialog,
  executableProjectAgents,
  projectThreadAgentSelection,
  type ExecutableProjectAgent,
} from "./agent-selector-dialog";

export function ProjectConnectionsPage({ project }: { project: Project }) {
  const { user } = useAuth();
  const privateWork = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  const scope = privateWork.scope;
  if (!scope) {
    throw new Error("Project connections require an entered project scope");
  }
  const canManage = project.capabilities.includes("private_work.create");
  const queryKey = projectConnectionsQueryKey(scope);
  const connections = useQuery({
    queryKey,
    queryFn: ({ signal }) => listProjectConnections(privateWork, signal),
  });
  const agentsQuery = useProjectAssets(
    user?.id ?? "",
    project.id,
    "agents",
    canManage && Boolean(user),
  );
  const agents = useMemo(
    () =>
      executableProjectAgents(agentsQuery.data as ProjectAssetList | undefined),
    [agentsQuery.data],
  );
  const [selectedProvider, setSelectedProvider] =
    useState<ChannelProviderId | null>(null);
  const [pendingProvider, setPendingProvider] =
    useState<ChannelProviderId | null>(null);

  const handleAgentSelect = async (agent: ExecutableProjectAgent) => {
    const provider = selectedProvider;
    if (!provider || pendingProvider) return;
    const connectWindow = prepareConnectWindow();
    setPendingProvider(provider);
    try {
      const result = await connectProjectConnection(
        privateWork,
        provider,
        projectThreadAgentSelection(agent),
      );
      if (result.url) {
        openConnectUrl(result.url, connectWindow);
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
        <h1 className="text-2xl font-semibold tracking-tight">Connections</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          将已配置的 IM 渠道绑定到当前项目与当前账号。
        </p>
      </header>

      {connections.isLoading ? (
        <p role="status" className="text-muted-foreground text-sm">
          正在加载 Connections…
        </p>
      ) : connections.error ? (
        <div className="rounded-xl border p-5">
          <p role="alert" className="text-destructive text-sm">
            无法加载 Connections，请稍后重试。
          </p>
          <Button
            type="button"
            variant="outline"
            className="mt-4"
            onClick={() => void connections.refetch()}
          >
            重试
          </Button>
        </div>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2">
          {CHANNEL_PROVIDER_IDS.map((provider) => {
            const connection = connections.data?.find(
              (item) =>
                item.provider === provider && item.status === "connected",
            );
            const isPending = pendingProvider === provider;
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
                </div>
                {canManage ? (
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
                      onClick={() => setSelectedProvider(provider)}
                    >
                      {isPending ? (
                        <LoaderCircleIcon className="animate-spin" />
                      ) : null}
                      连接
                    </Button>
                  )
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      <AgentSelectorDialog
        open={selectedProvider !== null}
        agents={agents}
        isCreating={pendingProvider !== null}
        isLoading={agentsQuery.isLoading}
        error={agentsQuery.error}
        onOpenChange={(open) => {
          if (!open && pendingProvider === null) setSelectedProvider(null);
        }}
        onSelect={(agent) => void handleAgentSelect(agent)}
      />
    </main>
  );
}

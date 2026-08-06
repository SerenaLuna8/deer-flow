"use client";

import {
  CopyIcon,
  LoaderCircleIcon,
  MessageSquarePlusIcon,
  PencilIcon,
  Trash2Icon,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { GatewayApiError } from "@/core/api/errors";
import { writeTextToClipboard } from "@/core/clipboard";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  useProjectChannelGroupBindingActions,
  useProjectChannelGroupBindings,
  type CreateProjectChannelGroupBindingChallengeInput,
  type ProjectChannelGroupBinding,
  type ProjectChannelGroupBindingChallenge,
} from "@/core/project-channel-group-bindings";
import { cn } from "@/lib/utils";

export type ProjectChannelGroupAgentOption = {
  id: string;
  scope: "project" | "system";
  displayName: string;
  available: boolean;
  unavailableReason?: string | null;
};

type PendingAction = "create" | "check" | "update" | "toggle" | "delete";
type ChallengeStatus = "pending" | "not_found" | "connected";

type ChallengeGuide = {
  challenge: ProjectChannelGroupBindingChallenge;
  agent: ProjectChannelGroupAgentOption;
  initialBindings: readonly Pick<
    ProjectChannelGroupBinding,
    "id" | "revision"
  >[];
};

function agentKey(agent: Pick<ProjectChannelGroupAgentOption, "id" | "scope">) {
  return `${agent.scope}:${agent.id}`;
}

export async function createGroupBindingChallenge({
  provider,
  agent,
  createChallenge,
}: {
  provider: string;
  agent: ProjectChannelGroupAgentOption;
  createChallenge: (
    input: CreateProjectChannelGroupBindingChallengeInput,
  ) => Promise<ProjectChannelGroupBindingChallenge>;
}) {
  if (!agent.available) {
    throw new Error("Selected Agent is unavailable");
  }
  return createChallenge({
    provider,
    agentAssetId: agent.id,
    agentScope: agent.scope,
  });
}

export function groupBindingChallengeExpiryLabel(seconds: number) {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "绑定命令已失效";
  }
  if (seconds < 60) return `${Math.ceil(seconds)} 秒后失效`;
  return `${Math.ceil(seconds / 60)} 分钟内有效`;
}

export function projectChannelGroupBindingErrorMessage(error: unknown) {
  if (error instanceof GatewayApiError) {
    if (
      error.code === "CHANNEL_GROUP_BINDING_CHALLENGE_EXPIRED" ||
      error.code === "GROUP_BINDING_CHALLENGE_EXPIRED"
    ) {
      return "绑定命令已失效，请重新生成。";
    }
    if (
      error.code === "CHANNEL_GROUP_BINDING_AGENT_UNAVAILABLE" ||
      error.code === "GROUP_BINDING_AGENT_UNAVAILABLE"
    ) {
      return "所选 Agent 当前不可用，请选择其他 Agent。";
    }
    if (
      error.code === "CHANNEL_GROUP_BINDING_NOT_FOUND" ||
      error.code === "GROUP_BINDING_NOT_FOUND"
    ) {
      return "尚未检测到群聊连接，请在飞书群发送命令后重试。";
    }
    if (error.code === "GROUP_BINDING_FORBIDDEN") {
      return "需要项目 Admin 权限才能管理群聊连接。";
    }
    if (error.code === "GROUP_BINDING_CONFLICT") {
      return "群聊连接已发生变化，请刷新后重试。";
    }
    if (error.code === "GROUP_BINDING_INVALID") {
      const agentInvalid = error.fields.some(
        (field) => field === "agent_asset_id" || field === "agent_scope",
      );
      return agentInvalid
        ? "所选 Agent 当前不可用，请选择其他 Agent。"
        : "群聊连接信息无效，请重新操作。";
    }
    if (error.code === "GROUP_BINDING_UNAVAILABLE") {
      return "群聊连接服务暂时不可用，请稍后重试。";
    }
  }
  return "群聊连接操作失败，请稍后重试。";
}

function formatBindingTime(value: string | null) {
  if (!value) return "暂无活动";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "暂无活动";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function findBindingAgent(
  binding: ProjectChannelGroupBinding,
  agents: readonly ProjectChannelGroupAgentOption[],
) {
  return agents.find(
    (agent) =>
      agent.id === binding.agent_asset_id &&
      agent.scope === binding.agent_scope,
  );
}

export function findCompletedGroupBinding(
  bindings: readonly ProjectChannelGroupBinding[],
  guide: Pick<ChallengeGuide, "agent" | "challenge" | "initialBindings">,
) {
  const initialRevisions = new Map(
    guide.initialBindings.map((binding) => [binding.id, binding.revision]),
  );
  return bindings.find((binding) => {
    if (
      binding.provider !== guide.challenge.provider ||
      binding.agent_asset_id !== guide.agent.id ||
      binding.agent_scope !== guide.agent.scope
    ) {
      return false;
    }
    const initialRevision = initialRevisions.get(binding.id);
    return initialRevision === undefined || binding.revision > initialRevision;
  });
}

export function ProjectChannelGroupBindingRows({
  bindings,
  agents,
  manageable,
  pendingBindingId,
  onEditAgent,
  onToggle,
  onDelete,
}: {
  bindings: readonly ProjectChannelGroupBinding[];
  agents: readonly ProjectChannelGroupAgentOption[];
  manageable: boolean;
  pendingBindingId: string | null;
  onEditAgent: (binding: ProjectChannelGroupBinding) => void;
  onToggle: (binding: ProjectChannelGroupBinding) => void;
  onDelete: (binding: ProjectChannelGroupBinding) => void;
}) {
  if (bindings.length === 0) {
    return (
      <div className="text-muted-foreground rounded-xl border border-dashed px-4 py-7 text-center text-sm">
        暂未绑定群聊
      </div>
    );
  }

  return (
    <div className="divide-y rounded-xl border">
      {bindings.map((binding) => {
        const agent = findBindingAgent(binding, agents);
        const active = binding.status === "active";
        const pending = pendingBindingId === binding.id;
        return (
          <article
            key={binding.id}
            className="flex flex-col gap-4 px-4 py-4 lg:flex-row lg:items-center"
          >
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-3">
                <h4 className="truncate font-medium">{binding.display_name}</h4>
                <span
                  role="status"
                  aria-label={`群聊状态：${active ? "运行中" : "已停用"}`}
                  className="text-muted-foreground inline-flex shrink-0 items-center gap-1.5 text-xs"
                >
                  <span
                    aria-hidden="true"
                    className={cn(
                      "size-2 rounded-full",
                      active ? "bg-success" : "bg-muted-foreground/45",
                    )}
                  />
                  {active ? "运行中" : "已停用"}
                </span>
              </div>
              <div className="text-muted-foreground mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm">
                <span>
                  Agent：
                  {agent
                    ? `${agent.displayName}${agent.available ? "" : "（不可用）"}`
                    : "Agent 不可用"}
                </span>
                <span>
                  最近活动：{formatBindingTime(binding.last_activity_at)}
                </span>
              </div>
            </div>
            {manageable ? (
              <div className="flex shrink-0 flex-wrap items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={pending}
                  onClick={() => onEditAgent(binding)}
                >
                  <PencilIcon />
                  修改 Agent
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={pending}
                  onClick={() => onToggle(binding)}
                >
                  {pending ? (
                    <LoaderCircleIcon className="animate-spin" />
                  ) : null}
                  {active ? "停用" : "启用"}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="text-destructive hover:text-destructive"
                  disabled={pending}
                  onClick={() => onDelete(binding)}
                >
                  <Trash2Icon />
                  删除
                </Button>
              </div>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}

function AgentSelect({
  agents,
  value,
  onChange,
}: {
  agents: readonly ProjectChannelGroupAgentOption[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-2 text-sm font-medium">
      Agent
      <select
        aria-label="Agent"
        className="border-input bg-background h-10 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-2"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">选择 Agent</option>
        {agents.map((agent) => (
          <option
            key={agentKey(agent)}
            value={agentKey(agent)}
            disabled={!agent.available}
          >
            {agent.displayName}
            {agent.available ? "" : "（不可用）"}
          </option>
        ))}
      </select>
    </label>
  );
}

export function ProjectChannelGroupBindingStartControl({
  manageable,
  enabled,
  blockedReason,
  resumable,
  onStart,
}: {
  manageable: boolean;
  enabled: boolean;
  blockedReason: string | null;
  resumable: boolean;
  onStart: () => void;
}) {
  if (!manageable) return null;
  return (
    <div className="flex flex-col items-end gap-2">
      <Button type="button" size="sm" disabled={!enabled} onClick={onStart}>
        <MessageSquarePlusIcon />
        {resumable ? "继续绑定" : "绑定群聊"}
      </Button>
      {!enabled && blockedReason ? (
        <p role="alert" className="text-destructive text-right text-xs">
          {blockedReason}
        </p>
      ) : null}
    </div>
  );
}

export function ProjectChannelGroupBindings({
  provider = "feishu",
  agents,
  manageable,
  bindingEnabled = true,
  bindingBlockedReason = null,
}: {
  provider?: string;
  agents: readonly ProjectChannelGroupAgentOption[];
  manageable: boolean;
  bindingEnabled?: boolean;
  bindingBlockedReason?: string | null;
}) {
  const access = usePrivateWorkAccess();
  const bindingsQuery = useProjectChannelGroupBindings(access);
  const actions = useProjectChannelGroupBindingActions(access);
  const refreshBindings = actions.refresh;
  const providerBindings = useMemo(
    () =>
      (bindingsQuery.data?.bindings ?? []).filter(
        (binding) => binding.provider === provider,
      ),
    [bindingsQuery.data, provider],
  );
  const availableAgents = useMemo(
    () => agents.filter((agent) => agent.available),
    [agents],
  );
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedAgentKey, setSelectedAgentKey] = useState("");
  const [challengeGuide, setChallengeGuide] = useState<ChallengeGuide | null>(
    null,
  );
  const [guideOpen, setGuideOpen] = useState(false);
  const [challengeStatus, setChallengeStatus] =
    useState<ChallengeStatus>("pending");
  const [connectedGroupName, setConnectedGroupName] = useState<string | null>(
    null,
  );
  const [clock, setClock] = useState(() => Date.now());
  const [editTarget, setEditTarget] =
    useState<ProjectChannelGroupBinding | null>(null);
  const [editAgentKey, setEditAgentKey] = useState("");
  const [deleteTarget, setDeleteTarget] =
    useState<ProjectChannelGroupBinding | null>(null);
  const [pending, setPending] = useState<{
    action: PendingAction;
    bindingId: string | null;
  } | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const challengeSecondsRemaining = challengeGuide
    ? Math.max(
        0,
        Math.ceil(
          (Date.parse(challengeGuide.challenge.expires_at) - clock) / 1000,
        ),
      )
    : 0;
  const challengeExpired =
    challengeGuide !== null &&
    challengeStatus !== "connected" &&
    challengeSecondsRemaining <= 0;

  useEffect(() => {
    if (!guideOpen || !challengeGuide || challengeStatus === "connected") {
      return;
    }
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [challengeGuide, challengeStatus, guideOpen]);

  const openCreateDialog = () => {
    setErrorMessage(null);
    const first = availableAgents[0];
    setSelectedAgentKey(first ? agentKey(first) : "");
    setCreateOpen(true);
  };

  const openBindingFlow = () => {
    if (!bindingEnabled) return;
    if (
      challengeGuide &&
      !challengeExpired &&
      challengeStatus !== "connected"
    ) {
      setGuideOpen(true);
      return;
    }
    openCreateDialog();
  };

  const handleCreateChallenge = async () => {
    if (!manageable || pending) return;
    const agent = agents.find(
      (candidate) => agentKey(candidate) === selectedAgentKey,
    );
    if (!agent?.available) {
      setErrorMessage("所选 Agent 当前不可用，请选择其他 Agent。");
      return;
    }
    setPending({ action: "create", bindingId: null });
    setErrorMessage(null);
    try {
      const challenge = await createGroupBindingChallenge({
        provider,
        agent,
        createChallenge: actions.createChallenge,
      });
      setChallengeGuide({
        challenge,
        agent,
        initialBindings: providerBindings.map(({ id, revision }) => ({
          id,
          revision,
        })),
      });
      setChallengeStatus("pending");
      setConnectedGroupName(null);
      setClock(Date.now());
      setCreateOpen(false);
      setGuideOpen(true);
    } catch (error) {
      setErrorMessage(projectChannelGroupBindingErrorMessage(error));
    } finally {
      setPending(null);
    }
  };

  const handleCheckChallenge = async () => {
    if (!challengeGuide || pending || challengeExpired) return;
    setPending({ action: "check", bindingId: null });
    setErrorMessage(null);
    try {
      const latest = await actions.refresh();
      const connected = findCompletedGroupBinding(
        latest.bindings,
        challengeGuide,
      );
      if (!connected) {
        setChallengeStatus("not_found");
        setErrorMessage("尚未检测到群聊连接，请在飞书群发送命令后重试。");
        return;
      }
      setChallengeStatus("connected");
      setConnectedGroupName(connected.display_name);
      toast.success(`${connected.display_name} 已连接`);
    } catch (error) {
      setErrorMessage(projectChannelGroupBindingErrorMessage(error));
    } finally {
      setPending(null);
    }
  };

  useEffect(() => {
    if (
      !guideOpen ||
      !challengeGuide ||
      challengeExpired ||
      challengeStatus === "connected"
    ) {
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const latest = await refreshBindings();
        const connected = findCompletedGroupBinding(
          latest.bindings,
          challengeGuide,
        );
        if (connected && !cancelled) {
          setChallengeStatus("connected");
          setConnectedGroupName(connected.display_name);
          setErrorMessage(null);
          toast.success(`${connected.display_name} 已连接`);
          return;
        }
      } catch {
        // Keep the one-time command usable during a transient refresh failure.
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 2000);
    };
    timer = window.setTimeout(() => void poll(), 2000);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [
    challengeExpired,
    challengeGuide,
    challengeStatus,
    guideOpen,
    refreshBindings,
  ]);

  const handleToggle = async (binding: ProjectChannelGroupBinding) => {
    if (!manageable || pending) return;
    setPending({ action: "toggle", bindingId: binding.id });
    setErrorMessage(null);
    try {
      const enabled = binding.status !== "active";
      await actions.update(binding.id, {
        expectedRevision: binding.revision,
        enabled,
      });
      toast.success(`${binding.display_name} 已${enabled ? "启用" : "停用"}`);
    } catch (error) {
      setErrorMessage(projectChannelGroupBindingErrorMessage(error));
    } finally {
      setPending(null);
    }
  };

  const openEditAgent = (binding: ProjectChannelGroupBinding) => {
    setErrorMessage(null);
    setEditTarget(binding);
    setEditAgentKey(
      agentKey({ id: binding.agent_asset_id, scope: binding.agent_scope }),
    );
  };

  const handleEditAgent = async () => {
    if (!editTarget || !manageable || pending) return;
    const agent = agents.find(
      (candidate) => agentKey(candidate) === editAgentKey,
    );
    if (!agent?.available) {
      setErrorMessage("所选 Agent 当前不可用，请选择其他 Agent。");
      return;
    }
    setPending({ action: "update", bindingId: editTarget.id });
    setErrorMessage(null);
    try {
      await actions.update(editTarget.id, {
        expectedRevision: editTarget.revision,
        agentAssetId: agent.id,
        agentScope: agent.scope,
      });
      toast.success(`${editTarget.display_name} 的 Agent 已更新`);
      setEditTarget(null);
    } catch (error) {
      setErrorMessage(projectChannelGroupBindingErrorMessage(error));
    } finally {
      setPending(null);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget || !manageable || pending) return;
    setPending({ action: "delete", bindingId: deleteTarget.id });
    setErrorMessage(null);
    try {
      await actions.remove(deleteTarget.id, deleteTarget.revision);
      toast.success(`${deleteTarget.display_name} 已删除`);
      setDeleteTarget(null);
    } catch (error) {
      setErrorMessage(projectChannelGroupBindingErrorMessage(error));
    } finally {
      setPending(null);
    }
  };

  return (
    <section className="grid gap-4" aria-labelledby="group-bindings-title">
      <div className="flex items-center justify-between gap-4">
        <h3 id="group-bindings-title" className="text-base font-semibold">
          群聊连接
        </h3>
        <ProjectChannelGroupBindingStartControl
          manageable={manageable}
          enabled={bindingEnabled}
          blockedReason={bindingBlockedReason}
          resumable={
            challengeGuide !== null &&
            !challengeExpired &&
            challengeStatus !== "connected"
          }
          onStart={openBindingFlow}
        />
      </div>

      {errorMessage &&
      !createOpen &&
      !guideOpen &&
      !editTarget &&
      !deleteTarget ? (
        <p role="alert" className="text-destructive text-sm">
          {errorMessage}
        </p>
      ) : null}

      {bindingsQuery.isPending ? (
        <div className="text-muted-foreground flex items-center gap-2 py-6 text-sm">
          <LoaderCircleIcon className="animate-spin" />
          加载中
        </div>
      ) : bindingsQuery.isError ? (
        <div className="flex items-center gap-3 rounded-xl border px-4 py-4">
          <p role="alert" className="text-destructive flex-1 text-sm">
            无法加载群聊连接
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void bindingsQuery.refetch()}
          >
            重试
          </Button>
        </div>
      ) : (
        <ProjectChannelGroupBindingRows
          bindings={providerBindings}
          agents={agents}
          manageable={manageable}
          pendingBindingId={pending?.bindingId ?? null}
          onEditAgent={openEditAgent}
          onToggle={(binding) => void handleToggle(binding)}
          onDelete={setDeleteTarget}
        />
      )}

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent closeLabel="关闭">
          <DialogHeader>
            <DialogTitle>绑定飞书群聊</DialogTitle>
            <DialogDescription className="sr-only">
              选择群聊使用的 Agent
            </DialogDescription>
          </DialogHeader>
          <AgentSelect
            agents={agents}
            value={selectedAgentKey}
            onChange={setSelectedAgentKey}
          />
          {availableAgents.length === 0 ? (
            <p role="alert" className="text-destructive text-sm">
              暂无可用 Agent
            </p>
          ) : null}
          {createOpen && errorMessage ? (
            <p role="alert" className="text-destructive text-sm">
              {errorMessage}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setCreateOpen(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              disabled={
                !selectedAgentKey ||
                availableAgents.length === 0 ||
                pending?.action === "create"
              }
              onClick={() => void handleCreateChallenge()}
            >
              {pending?.action === "create" ? (
                <LoaderCircleIcon className="animate-spin" />
              ) : null}
              生成绑定命令
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={guideOpen}
        onOpenChange={(open) => {
          setGuideOpen(open);
          if (!open && challengeStatus === "connected") {
            setChallengeGuide(null);
          }
        }}
      >
        <DialogContent closeLabel="关闭" className="min-w-0">
          <DialogHeader>
            <DialogTitle>完成飞书群聊连接</DialogTitle>
            <DialogDescription className="sr-only">
              在飞书群聊中发送绑定命令
            </DialogDescription>
          </DialogHeader>
          {challengeGuide ? (
            <div className="grid min-w-0 gap-4">
              <p className="text-muted-foreground text-sm">
                复制并发送到要绑定的飞书群
              </p>
              <div className="bg-muted flex min-w-0 items-start gap-2 rounded-lg p-3">
                <code className="min-w-0 flex-1 text-sm leading-6 break-all">
                  {challengeGuide.challenge.command}
                </code>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="shrink-0"
                  onClick={() => {
                    void writeTextToClipboard(challengeGuide.challenge.command)
                      .then(() => toast.success("绑定命令已复制"))
                      .catch(() => toast.error("复制失败，请手动复制"));
                  }}
                >
                  <CopyIcon />
                  复制
                </Button>
              </div>
              {challengeStatus === "connected" ? (
                <div
                  role="status"
                  className="border-success/30 bg-success/10 text-success rounded-lg border px-4 py-3 text-sm"
                >
                  {connectedGroupName ?? "群聊"}连接成功
                </div>
              ) : challengeExpired ? (
                <p role="alert" className="text-destructive text-sm">
                  绑定命令已失效，请重新生成。
                </p>
              ) : (
                <p className="text-muted-foreground text-sm">
                  {groupBindingChallengeExpiryLabel(challengeSecondsRemaining)}
                </p>
              )}
              {guideOpen && errorMessage ? (
                <p role="alert" className="text-destructive text-sm">
                  {errorMessage}
                </p>
              ) : null}
            </div>
          ) : null}
          <DialogFooter>
            {challengeStatus === "connected" ? (
              <Button type="button" onClick={() => setGuideOpen(false)}>
                完成
              </Button>
            ) : challengeExpired ? (
              <Button
                type="button"
                onClick={() => {
                  setGuideOpen(false);
                  setChallengeGuide(null);
                  openCreateDialog();
                }}
              >
                重新生成
              </Button>
            ) : (
              <>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setGuideOpen(false)}
                >
                  稍后完成
                </Button>
                <Button
                  type="button"
                  disabled={pending?.action === "check"}
                  onClick={() => void handleCheckChallenge()}
                >
                  {pending?.action === "check" ? (
                    <LoaderCircleIcon className="animate-spin" />
                  ) : null}
                  {challengeStatus === "not_found" ? "重新检查" : "检查连接"}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={editTarget !== null}
        onOpenChange={(open) => {
          if (!open) setEditTarget(null);
        }}
      >
        <DialogContent closeLabel="关闭">
          <DialogHeader>
            <DialogTitle>修改 Agent</DialogTitle>
            <DialogDescription className="sr-only">
              修改群聊使用的 Agent
            </DialogDescription>
          </DialogHeader>
          <AgentSelect
            agents={agents}
            value={editAgentKey}
            onChange={setEditAgentKey}
          />
          {editTarget && errorMessage ? (
            <p role="alert" className="text-destructive text-sm">
              {errorMessage}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setEditTarget(null)}
            >
              取消
            </Button>
            <Button
              type="button"
              disabled={!editAgentKey || pending?.action === "update"}
              onClick={() => void handleEditAgent()}
            >
              {pending?.action === "update" ? (
                <LoaderCircleIcon className="animate-spin" />
              ) : null}
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
      >
        <DialogContent closeLabel="关闭">
          <DialogHeader>
            <DialogTitle>
              删除{deleteTarget?.display_name ?? "群聊"}
            </DialogTitle>
            <DialogDescription>删除后该群将无法继续使用。</DialogDescription>
          </DialogHeader>
          {deleteTarget && errorMessage ? (
            <p role="alert" className="text-destructive text-sm">
              {errorMessage}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDeleteTarget(null)}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={pending?.action === "delete"}
              onClick={() => void handleDelete()}
            >
              {pending?.action === "delete" ? (
                <LoaderCircleIcon className="animate-spin" />
              ) : null}
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

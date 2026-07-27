"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  adminAssetErrorMessage,
  versionWorkflowActions,
} from "@/components/admin/assets/admin-asset-view-model";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import type { Capability } from "@/core/projects/types";
import {
  useApproveProjectMcpVersion,
  useChangeProjectAssetStatus,
  useDeleteProjectSkill,
  useProjectAssets,
  useProjectAssetVersions,
  usePublishProjectAssetVersion,
  useSubmitProjectMcpVersion,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectCredentialList,
} from "@/core/shared-assets";
import { mcpVersionRuntimeBlockReason } from "@/core/shared-assets/mcp-runtime";

import { McpApprovalDialog } from "./mcp-approval-dialog";
import {
  projectAssetDetailLifecycleActions,
  projectAssetCanAuthor,
  projectSkillStatusToggleState,
  projectSkillCanDelete,
} from "./project-asset-view-model";
import { ProjectSkillDeleteDialog } from "./project-skill-delete-dialog";
import { SystemBindingDialog } from "./system-binding-dialog";

type MutableAssetKind = Exclude<AssetListKind, "credentials">;
type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;
type VersionStatus = ReturnType<typeof workflowStatus>;

export type ProjectSkillDeleteSnapshot = Readonly<{
  assetId: string;
  skillName: string;
  expectedAssetVersion: number;
  startedAt: number;
}>;

export function createProjectSkillDeleteSnapshot(
  item: Pick<ProjectAssetItem, "id" | "display_name" | "version">,
  startedAt: number,
): ProjectSkillDeleteSnapshot {
  return Object.freeze({
    assetId: item.id,
    skillName: item.display_name,
    expectedAssetVersion: item.version,
    startedAt,
  });
}

export type ProjectAssetVersionRenderContext = {
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  canAuthor: boolean;
  onDirtyChange: (dirty: boolean) => void;
  onVersionCreated: (versionId: string) => void;
};

const VERSION_STATUS_LABEL: Record<VersionStatus, string> = {
  draft: "草稿",
  pending_approval: "待审批",
  published: "已发布",
  rejected: "已拒绝",
  active: "启用",
  retired: "已替换",
  revoked: "已撤销",
};

function isMcpVersion(version: AssetVersion): version is McpVersion {
  return "mcp_server_id" in version;
}

function workflowStatus(
  version: AssetVersion,
):
  | "draft"
  | "pending_approval"
  | "published"
  | "rejected"
  | "active"
  | "retired"
  | "revoked" {
  return "workflow_status" in version
    ? version.workflow_status
    : version.status;
}

export function versionPublishDisabled(
  actionPending: boolean,
  versionDirty: boolean,
  versionSelectionPending = false,
): boolean {
  return (
    versionActionDisabled(actionPending, versionSelectionPending) ||
    versionDirty
  );
}

export function versionActionDisabled(
  actionPending: boolean,
  versionSelectionPending: boolean,
): boolean {
  return actionPending || versionSelectionPending;
}

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <p role="alert" className="text-destructive text-sm">
      {adminAssetErrorMessage(error)}
    </p>
  );
}

export function ProjectAssetDetailHeader({
  kind,
  item,
  statusPending,
  optimisticSkillStatus,
  onToggleProjectSkillStatus,
}: {
  kind: MutableAssetKind;
  item: ProjectAssetItem;
  statusPending: boolean;
  optimisticSkillStatus?: boolean;
  onToggleProjectSkillStatus: (checked: boolean) => void;
}) {
  if (kind !== "skills") {
    return (
      <SheetHeader className="border-border/70 border-b px-6 py-5 pr-12 text-left">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={item.scope === "system" ? "secondary" : "default"}>
            {item.scope === "system" ? "系统提供" : "项目自建"}
          </Badge>
          <AssetStatusBadge status={item.status} />
        </div>
        <SheetTitle className="mt-2 text-xl">{item.display_name}</SheetTitle>
        <SheetDescription className="font-mono">{item.slug}</SheetDescription>
      </SheetHeader>
    );
  }

  const toggleState = projectSkillStatusToggleState(item);
  const checked = optimisticSkillStatus ?? toggleState.checked;
  const showToggle = item.scope === "project";
  return (
    <SheetHeader className="border-border/70 border-b px-6 py-5 pr-12 text-left">
      <div className="flex min-w-0 items-start justify-between gap-4">
        <SheetTitle className="min-w-0 truncate text-xl">
          {item.display_name}
        </SheetTitle>
        {showToggle ? (
          <div className="flex shrink-0 flex-col items-end gap-1.5">
            <Switch
              checked={checked}
              disabled={toggleState.disabled || statusPending}
              className="data-[state=checked]:bg-success focus-visible:ring-selection/30"
              aria-busy={statusPending || undefined}
              aria-label={`${checked ? "停用" : "启用"} ${item.display_name}`}
              title={toggleState.disabledReason ?? undefined}
              onCheckedChange={onToggleProjectSkillStatus}
            />
            {toggleState.disabledReason ? (
              <span className="text-muted-foreground text-xs">
                {toggleState.disabledReason}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
      <SheetDescription className="sr-only">
        Skill 详情与版本文件
      </SheetDescription>
    </SheetHeader>
  );
}

export function ProjectAssetDetailSheet({
  accountId,
  projectId,
  projectCapabilities,
  kind,
  item,
  open,
  requestedVersionId,
  onOpenChange,
  onCreateVersion,
  onDeleted,
  onVersionCreated,
  onRequestedVersionHandled,
  renderVersion,
}: {
  accountId: string;
  projectId: string;
  projectCapabilities: readonly Capability[];
  kind: MutableAssetKind;
  item: ProjectAssetItem;
  open: boolean;
  requestedVersionId: string | null;
  onOpenChange: (open: boolean) => void;
  onCreateVersion: (item: ProjectAssetItem) => void;
  onDeleted: (assetId: string) => void;
  onVersionCreated: (assetId: string, versionId: string) => void;
  onRequestedVersionHandled: (assetId: string, versionId: string) => void;
  renderVersion: (
    version: AssetVersion,
    context: ProjectAssetVersionRenderContext,
  ) => ReactNode;
}) {
  const history = useProjectAssetVersions(accountId, projectId, kind, item.id);
  const publish = usePublishProjectAssetVersion(accountId, projectId, kind);
  const submit = useSubmitProjectMcpVersion(accountId, projectId);
  const approve = useApproveProjectMcpVersion(accountId, projectId);
  const changeStatus = useChangeProjectAssetStatus(accountId, projectId, kind);
  const deleteSkill = useDeleteProjectSkill(accountId, projectId);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [bindingOpen, setBindingOpen] = useState(false);
  const [approvalVersion, setApprovalVersion] = useState<McpVersion | null>(
    null,
  );
  const [versionDirty, setVersionDirty] = useState(false);
  const [deleteSnapshot, setDeleteSnapshot] =
    useState<ProjectSkillDeleteSnapshot | null>(null);
  const [discardAction, setDiscardAction] = useState<
    { type: "close" } | { type: "version"; versionId: string } | null
  >(null);

  const versions = useMemo(() => history.data?.data ?? [], [history.data]);
  const selectedVersion =
    versions.find((version) => version.id === selectedVersionId) ?? null;
  const selectedRuntimeBlockReason =
    selectedVersion && isMcpVersion(selectedVersion)
      ? mcpVersionRuntimeBlockReason(selectedVersion, item.scope)
      : null;
  const currentPublished = versions.find(
    (version) => version.id === item.current_published_version_id,
  );
  const pinnedVersion = versions.find(
    (version) => version.id === item.binding?.version_id,
  );

  useEffect(() => {
    if (!open || versions.length === 0) return;
    if (
      requestedVersionId &&
      versions.some((version) => version.id === requestedVersionId)
    ) {
      setSelectedVersionId(requestedVersionId);
      onRequestedVersionHandled(item.id, requestedVersionId);
      return;
    }
    const preferred =
      versions.find(
        (version) => version.id === item.current_published_version_id,
      ) ?? versions[0];
    setSelectedVersionId((current) =>
      versions.some((version) => version.id === current)
        ? current
        : (preferred?.id ?? ""),
    );
  }, [
    item.current_published_version_id,
    item.id,
    onRequestedVersionHandled,
    open,
    requestedVersionId,
    versions,
  ]);

  useEffect(() => {
    if (open) return;
    setSelectedVersionId("");
    setBindingOpen(false);
    setApprovalVersion(null);
    setVersionDirty(false);
    setDeleteSnapshot(null);
    setDiscardAction(null);
  }, [open]);

  const canAuthor =
    item.scope === "project" && projectAssetCanAuthor(item, kind);
  const canApprove =
    item.scope === "project" &&
    item.status === "active" &&
    item.capabilities.includes("mcp.credentials.approve");
  const canManageBinding =
    item.scope === "system" &&
    item.capabilities.includes("shared_assets.manage_bindings") &&
    (item.status === "active" || Boolean(item.binding?.enabled));
  const lifecycleActions =
    item.scope === "project"
      ? projectAssetDetailLifecycleActions(kind, item, projectCapabilities)
      : [];
  const canDeleteSkill = projectSkillCanDelete(kind, item);
  const versionActions = useMemo(() => {
    if (
      item.scope !== "project" ||
      !selectedVersion ||
      !("workflow_status" in selectedVersion)
    ) {
      return [];
    }
    return versionWorkflowActions(
      kind,
      selectedVersion.workflow_status,
      isMcpVersion(selectedVersion) &&
        selectedVersion.credential_slots.length > 0,
    );
  }, [item.scope, kind, selectedVersion]);

  const credentialCatalog = useProjectAssets(
    accountId,
    projectId,
    "credentials",
    approvalVersion !== null && canApprove,
  );
  const credentials = credentialCatalog.data as
    | ProjectCredentialList
    | undefined;
  const actionPending =
    publish.isPending ||
    submit.isPending ||
    approve.isPending ||
    changeStatus.isPending ||
    deleteSkill.isPending;
  const versionSelectionPending = requestedVersionId !== null;
  const actionError =
    publish.error ?? submit.error ?? approve.error ?? changeStatus.error;
  const optimisticSkillStatus =
    kind === "skills" && changeStatus.isPending
      ? changeStatus.variables?.action === "activate"
      : undefined;

  const handleWorkbenchVersionCreated = useCallback(
    (versionId: string) => {
      setVersionDirty(false);
      onVersionCreated(item.id, versionId);
      void history.refetch();
    },
    [history, item.id, onVersionCreated],
  );

  function publishSelectedVersion(version: AssetVersion) {
    if (
      isMcpVersion(version) &&
      mcpVersionRuntimeBlockReason(version, item.scope)
    ) {
      return;
    }
    publish.mutate(
      {
        assetId: item.id,
        versionId: version.id,
        input: { expected_asset_version: item.version },
      },
      {
        onSuccess: (result) => {
          setSelectedVersionId(result.data.id);
          handleWorkbenchVersionCreated(result.data.id);
        },
      },
    );
  }

  function requestOpenChange(next: boolean) {
    if (!next && versionDirty) {
      setDiscardAction({ type: "close" });
      return;
    }
    onOpenChange(next);
  }

  function requestVersionChange(versionId: string) {
    if (versionId === selectedVersionId) return;
    if (versionDirty) {
      setDiscardAction({ type: "version", versionId });
      return;
    }
    setSelectedVersionId(versionId);
  }

  function confirmDiscardNavigation() {
    const action = discardAction;
    setDiscardAction(null);
    setVersionDirty(false);
    if (action?.type === "close") {
      onOpenChange(false);
    } else if (action?.type === "version") {
      setSelectedVersionId(action.versionId);
    }
  }

  async function approveVersion(
    version: McpVersion,
    credentialVersions: Record<string, string>,
  ): Promise<boolean> {
    if (mcpVersionRuntimeBlockReason(version, item.scope)) return false;
    try {
      await approve.mutateAsync({
        assetId: item.id,
        versionId: version.id,
        input: {
          credential_versions: credentialVersions,
          expected_asset_version: item.version,
        },
      });
      return true;
    } catch {
      return false;
    }
  }

  async function confirmSkillDelete() {
    const snapshot = deleteSnapshot;
    if (!snapshot) return;
    try {
      await deleteSkill.mutateAsync({
        assetId: snapshot.assetId,
        input: {
          expected_asset_version: snapshot.expectedAssetVersion,
        },
      });
      setDeleteSnapshot(null);
      onDeleted(snapshot.assetId);
    } catch {
      // The mutation exposes only its mapped public error inside the dialog.
    }
  }

  function toggleProjectSkillStatus(checked: boolean) {
    if (kind !== "skills") return;
    const toggleState = projectSkillStatusToggleState(item);
    if (toggleState.disabled || toggleState.checked === checked) return;
    changeStatus.mutate({
      assetId: item.id,
      action: checked ? "activate" : "suspend",
      input: { expected_asset_version: item.version },
    });
  }

  return (
    <>
      <Sheet open={open} onOpenChange={requestOpenChange}>
        <SheetContent
          className={`w-full gap-0 p-0 ${kind === "skills" ? "sm:max-w-[1080px]" : "sm:max-w-[640px]"}`}
        >
          <ProjectAssetDetailHeader
            kind={kind}
            item={item}
            statusPending={changeStatus.isPending}
            optimisticSkillStatus={optimisticSkillStatus}
            onToggleProjectSkillStatus={toggleProjectSkillStatus}
          />

          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="space-y-6 px-6 py-5">
              <section className="grid gap-3 sm:grid-cols-2">
                <div className="bg-muted/35 rounded-xl p-4">
                  <p className="text-muted-foreground text-xs">
                    {item.scope === "system" ? "系统最新发布" : "当前发布"}
                  </p>
                  <p className="mt-2 text-sm font-medium">
                    {currentPublished
                      ? `版本 ${currentPublished.version_number}`
                      : item.current_published_version_id
                        ? "已有发布版本"
                        : "尚未发布"}
                  </p>
                </div>
                {item.scope === "system" && (
                  <div className="bg-muted/35 rounded-xl p-4">
                    <p className="text-muted-foreground text-xs">项目使用</p>
                    <p className="mt-2 text-sm font-medium">
                      {!item.binding
                        ? "未启用"
                        : !item.binding.enabled
                          ? "已从项目停用"
                          : pinnedVersion
                            ? `版本 ${pinnedVersion.version_number}${
                                item.current_published_version_id !==
                                pinnedVersion.id
                                  ? " · 有新版本"
                                  : ""
                              }`
                            : "已固定版本"}
                    </p>
                  </div>
                )}
                <div className="bg-muted/35 rounded-xl p-4">
                  <p className="text-muted-foreground text-xs">最近更新</p>
                  <time className="mt-2 block text-sm">
                    {new Date(item.updated_at).toLocaleString("zh-CN")}
                  </time>
                </div>
              </section>

              <div className="flex flex-wrap gap-2">
                {canManageBinding && (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setBindingOpen(true)}
                  >
                    {item.binding?.enabled ? "切换版本" : "启用到项目"}
                  </Button>
                )}
                {canAuthor && (
                  <Button
                    type="button"
                    disabled={versionDirty || versionSelectionPending}
                    title={
                      versionDirty
                        ? "请先保存或放弃当前未保存修改"
                        : versionSelectionPending
                          ? "正在加载新版本，请稍候"
                          : undefined
                    }
                    onClick={() => onCreateVersion(item)}
                  >
                    {kind === "skills" ? "从空白创建" : "创建新版本"}
                  </Button>
                )}
                {lifecycleActions.map((action) => (
                  <Button
                    key={action}
                    type="button"
                    variant={action === "archive" ? "outline" : "destructive"}
                    disabled={actionPending}
                    onClick={() =>
                      changeStatus.mutate({
                        assetId: item.id,
                        action,
                        input: { expected_asset_version: item.version },
                      })
                    }
                  >
                    {action === "archive" ? "归档" : "暂停"}
                  </Button>
                ))}
                {canDeleteSkill && (
                  <Button
                    type="button"
                    variant="destructive"
                    disabled={actionPending}
                    onClick={() => {
                      deleteSkill.reset();
                      setDeleteSnapshot(
                        createProjectSkillDeleteSnapshot(item, Date.now()),
                      );
                    }}
                  >
                    删除 Skill
                  </Button>
                )}
              </div>

              <section className="space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <h2 className="text-sm font-semibold">版本</h2>
                  {versions.length > 0 && (
                    <label className="text-muted-foreground flex items-center gap-2 text-xs">
                      查看
                      <select
                        aria-label="查看版本"
                        value={selectedVersionId}
                        disabled={versionSelectionPending}
                        onChange={(event) =>
                          requestVersionChange(event.target.value)
                        }
                        className="border-input bg-background h-8 rounded-md border px-2 text-xs"
                      >
                        {versions.map((version) => (
                          <option key={version.id} value={version.id}>
                            版本 {version.version_number} ·{" "}
                            {VERSION_STATUS_LABEL[workflowStatus(version)]}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                </div>

                {history.isLoading ? (
                  <div className="space-y-3" aria-label="正在加载版本">
                    <Skeleton className="h-8 w-40" />
                    <Skeleton className="h-40 w-full rounded-xl" />
                  </div>
                ) : history.error ? (
                  <div className="border-destructive/30 space-y-3 rounded-xl border p-4">
                    <ErrorNotice error={history.error} />
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={history.isFetching}
                      onClick={() => void history.refetch()}
                    >
                      {history.isFetching ? "重试中…" : "重试"}
                    </Button>
                  </div>
                ) : !selectedVersion ? (
                  <p className="text-muted-foreground rounded-xl border border-dashed p-5 text-sm">
                    尚未创建版本。
                  </p>
                ) : (
                  <div className="space-y-5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-lg font-semibold">
                        版本 {selectedVersion.version_number}
                      </span>
                      <AssetStatusBadge
                        status={workflowStatus(selectedVersion)}
                      />
                      <time className="text-muted-foreground ml-auto text-xs">
                        {new Date(selectedVersion.created_at).toLocaleString(
                          "zh-CN",
                        )}
                      </time>
                    </div>

                    {item.scope === "project" &&
                      versionActions.length > 0 && (
                        <div className="flex flex-wrap gap-2">
                          {versionActions.includes("publish") &&
                            canAuthor && (
                              <Button
                                type="button"
                                size="sm"
                                disabled={versionPublishDisabled(
                                  actionPending ||
                                    Boolean(selectedRuntimeBlockReason),
                                  versionDirty,
                                  versionSelectionPending,
                                )}
                                title={
                                  selectedRuntimeBlockReason ??
                                  (versionDirty
                                    ? "请先保存或放弃当前未保存修改"
                                    : versionSelectionPending
                                      ? "正在加载新版本，请稍候"
                                      : undefined)
                                }
                                onClick={() =>
                                  publishSelectedVersion(selectedVersion)
                                }
                              >
                                发布版本
                              </Button>
                            )}
                          {versionActions.includes("submit") &&
                            canAuthor &&
                            isMcpVersion(selectedVersion) && (
                              <Button
                                type="button"
                                size="sm"
                                disabled={versionActionDisabled(
                                  actionPending ||
                                    Boolean(selectedRuntimeBlockReason),
                                  versionSelectionPending,
                                )}
                                title={
                                  selectedRuntimeBlockReason ??
                                  (versionSelectionPending
                                    ? "正在加载新版本，请稍候"
                                    : undefined)
                                }
                                onClick={() =>
                                  submit.mutate({
                                    assetId: item.id,
                                    versionId: selectedVersion.id,
                                    input: {
                                      expected_asset_version: item.version,
                                    },
                                  })
                                }
                              >
                                提交审批
                              </Button>
                            )}
                          {versionActions.includes("approve") &&
                            canApprove &&
                            isMcpVersion(selectedVersion) && (
                              <Button
                                type="button"
                                size="sm"
                                disabled={versionActionDisabled(
                                  actionPending ||
                                    Boolean(selectedRuntimeBlockReason),
                                  versionSelectionPending,
                                )}
                                title={
                                  selectedRuntimeBlockReason ??
                                  (versionSelectionPending
                                    ? "正在加载新版本，请稍候"
                                    : undefined)
                                }
                                onClick={() =>
                                  setApprovalVersion(selectedVersion)
                                }
                              >
                                批准并发布
                              </Button>
                            )}
                        </div>
                      )}

                    {selectedRuntimeBlockReason ? (
                      <p role="alert" className="text-destructive text-sm">
                        {selectedRuntimeBlockReason}
                      </p>
                    ) : null}

                    {isMcpVersion(selectedVersion) &&
                      selectedVersion.workflow_status === "pending_approval" &&
                      !canApprove && (
                        <p className="text-muted-foreground rounded-xl border p-4 text-sm">
                          已提交，正在等待项目 Admin 审批。
                        </p>
                      )}

                    <div
                      key={selectedVersion.id}
                      className="border-border/70 border-t pt-5"
                    >
                      {renderVersion(selectedVersion, {
                        accountId,
                        projectId,
                        item,
                        canAuthor: canAuthor && !versionSelectionPending,
                        onDirtyChange: setVersionDirty,
                        onVersionCreated: handleWorkbenchVersionCreated,
                      })}
                    </div>

                    <details className="border-border/70 rounded-xl border px-4 py-3">
                      <summary className="cursor-pointer text-sm font-medium">
                        版本技术信息
                      </summary>
                      <dl className="mt-3 grid gap-3 text-xs">
                        {"payload_checksum" in selectedVersion && (
                          <div>
                            <dt className="text-muted-foreground">
                              载荷校验和
                            </dt>
                            <dd className="mt-1 font-mono break-all">
                              {selectedVersion.payload_checksum}
                            </dd>
                          </div>
                        )}
                        <div>
                          <dt className="text-muted-foreground">创建者</dt>
                          <dd className="mt-1 font-mono break-all">
                            {selectedVersion.created_by_user_id}
                          </dd>
                        </div>
                      </dl>
                    </details>
                  </div>
                )}
              </section>

              <ErrorNotice error={actionError} />
            </div>
          </div>
        </SheetContent>
      </Sheet>

      {bindingOpen && (
        <SystemBindingDialog
          accountId={accountId}
          projectId={projectId}
          kind={kind}
          item={item}
          open
          onOpenChange={setBindingOpen}
        />
      )}

      <McpApprovalDialog
        version={approvalVersion}
        open={approvalVersion !== null}
        pending={approve.isPending}
        credentials={credentials?.project_items ?? []}
        credentialScope="project"
        credentialsLoading={credentialCatalog.isLoading}
        credentialsError={credentialCatalog.error}
        approvalError={approve.error}
        onRetryCredentials={() => void credentialCatalog.refetch()}
        onOpenChange={(next) => {
          if (next) return;
          setApprovalVersion(null);
          approve.reset();
        }}
        onApprove={approveVersion}
      />

      {deleteSnapshot !== null && (
        <ProjectSkillDeleteDialog
          key={`${deleteSnapshot.assetId}:${deleteSnapshot.startedAt}`}
          skillName={deleteSnapshot.skillName}
          startedAt={deleteSnapshot.startedAt}
          pending={deleteSkill.isPending}
          errorMessage={
            deleteSkill.error ? adminAssetErrorMessage(deleteSkill.error) : null
          }
          onOpenChange={(next) => {
            if (next) return;
            setDeleteSnapshot(null);
            deleteSkill.reset();
          }}
          onConfirm={() => void confirmSkillDelete()}
        />
      )}

      <Dialog
        open={discardAction !== null}
        onOpenChange={(next) => !next && setDiscardAction(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>放弃未保存的文件修改？</DialogTitle>
            <DialogDescription>
              切换版本或关闭详情会清除当前编辑副本，已保存的 Skill
              版本不会受影响。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDiscardAction(null)}
            >
              继续编辑
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={confirmDiscardNavigation}
            >
              放弃并继续
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

"use client";

import { useQueryClient } from "@tanstack/react-query";
import { KeyRoundIcon, PlusIcon } from "lucide-react";
import { useEffect, useState } from "react";

import {
  CreateAssetDialog,
  CreateVersionDialog,
  CredentialSecretDialog,
  type VersionAuthoringInput,
} from "@/components/admin/assets/admin-asset-dialogs";
import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { AssetVersionHistory } from "@/components/assets/asset-version-history";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  createProjectCredential,
  projectAssetKey,
  replaceProjectCredential,
  revokeProjectCredential,
  useApproveProjectMcpVersion,
  useChangeProjectAssetStatus,
  useCreateProjectAsset,
  useCreateProjectAssetVersion,
  useProjectAssets,
  useProjectAssetVersions,
  usePublishProjectAssetVersion,
  useSubmitProjectMcpVersion,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetList,
  type ProjectAssetItem,
  type ProjectCredentialList,
  type ProjectCredentialItem,
  type CreateCredentialInput,
  type ReplaceCredentialInput,
} from "@/core/shared-assets";

import { useCurrentProject } from "../project-context";

import { ProjectAssetSection } from "./project-asset-section";
import {
  projectAssetCanAuthor,
  projectAssetLifecycleActions,
} from "./project-asset-view-model";
import { SystemAssetSection } from "./system-asset-section";
import { SystemBindingDialog } from "./system-binding-dialog";

type MutableKind = Exclude<AssetListKind, "credentials">;

export function ProjectAssetCatalogView({
  kind: _kind,
  data,
  onManageBinding,
  onCreateVersion,
  renderSystemDetails,
  renderProjectDetails,
}: {
  kind: MutableKind;
  data: ProjectAssetList;
  onManageBinding?: (item: ProjectAssetItem) => void;
  onCreateVersion?: (item: ProjectAssetItem) => void;
  renderSystemDetails?: (item: ProjectAssetItem) => React.ReactNode;
  renderProjectDetails?: (item: ProjectAssetItem) => React.ReactNode;
}) {
  return (
    <div className="space-y-10">
      <SystemAssetSection
        items={data.system_items}
        onManageBinding={onManageBinding}
        renderDetails={renderSystemDetails}
      />
      <ProjectAssetSection
        items={data.project_items}
        onCreateVersion={onCreateVersion}
        renderDetails={renderProjectDetails}
      />
    </div>
  );
}

export function ProjectCredentialCatalogView({
  data,
  pending = false,
  onReplace,
  onRevoke,
  renderDetails,
}: {
  data: ProjectCredentialList;
  pending?: boolean;
  onReplace?: (credential: ProjectCredentialItem) => void;
  onRevoke?: (credential: ProjectCredentialItem) => void;
  renderDetails?: (credential: ProjectCredentialItem) => React.ReactNode;
}) {
  const groups = [
    ["系统 Credential", "系统", data.system_items],
    ["项目 Credential", "项目", data.project_items],
  ] as const;
  return (
    <div className="space-y-10">
      {groups.map(([title, source, items]) => (
        <section key={title} className="space-y-4">
          <h2 className="text-xl font-semibold">{title}</h2>
          {items.length === 0 ? (
            <p className="text-muted-foreground rounded-xl border border-dashed p-6 text-sm">
              暂无 {title}。
            </p>
          ) : (
            <div className="grid gap-4 xl:grid-cols-2">
              {items.map((credential) => (
                <Card key={credential.id} className="relative">
                  <CardHeader>
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <CardTitle className="flex items-center gap-2">
                          <KeyRoundIcon aria-hidden className="size-4" />
                          <span className="truncate">
                            {credential.display_name}
                          </span>
                        </CardTitle>
                        <p className="text-muted-foreground mt-1 font-mono text-xs">
                          {credential.name}
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        <Badge
                          variant={source === "系统" ? "secondary" : "default"}
                        >
                          {source}
                        </Badge>
                        <AssetStatusBadge status={credential.status} />
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <dl className="grid gap-2 text-sm sm:grid-cols-2">
                      <div>
                        <dt className="text-muted-foreground text-xs">类型</dt>
                        <dd>{credential.credential_type}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground text-xs">
                          元数据版本
                        </dt>
                        <dd>{credential.version}</dd>
                      </div>
                      <div className="sm:col-span-2">
                        <dt className="text-muted-foreground text-xs">
                          更新时间
                        </dt>
                        <dd>
                          {new Date(credential.updated_at).toLocaleString(
                            "zh-CN",
                          )}
                        </dd>
                      </div>
                    </dl>
                    {credential.status === "active" &&
                      credential.capabilities.includes(
                        "mcp.credentials.approve",
                      ) && (
                        <div className="flex flex-wrap gap-2">
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            disabled={pending}
                            onClick={() => onReplace?.(credential)}
                          >
                            替换凭据
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="destructive"
                            disabled={pending}
                            onClick={() => onRevoke?.(credential)}
                          >
                            撤销凭据
                          </Button>
                        </div>
                      )}
                    {renderDetails?.(credential)}
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </section>
      ))}
    </div>
  );
}

export function projectCredentialShowsHistory(
  credential: Pick<ProjectCredentialItem, "scope">,
): boolean {
  return credential.scope === "project";
}

export { projectAssetCanAuthor, projectAssetLifecycleActions };

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <p role="alert" className="text-destructive text-sm">
      {adminAssetErrorMessage(error)}
    </p>
  );
}

type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;

export function ProjectAssetHistoryView({
  kind,
  item,
  versions,
  approvalCredentials = [],
  approvalCredentialsLoading = false,
  approvalCredentialsError,
  isLoading = false,
  error,
  actionError,
  pending = false,
  onChangeStatus,
  onPublish,
  onSubmit,
  onApprove,
  onRetryApprovalCredentials,
}: {
  kind: MutableKind;
  item: ProjectAssetItem;
  versions: AssetVersion[];
  approvalCredentials?: ProjectCredentialItem[];
  approvalCredentialsLoading?: boolean;
  approvalCredentialsError?: unknown;
  isLoading?: boolean;
  error?: unknown;
  actionError?: unknown;
  pending?: boolean;
  onChangeStatus?: (action: "archive" | "suspend") => void;
  onPublish?: (version: AssetVersion) => void;
  onSubmit?: (version: McpVersion) => void;
  onApprove?: (
    version: McpVersion,
    credentialVersions: Record<string, string>,
  ) => void;
  onRetryApprovalCredentials?: () => void;
}) {
  const canAuthor = projectAssetCanAuthor(item);
  const canApprove = item.capabilities.includes("mcp.credentials.approve");
  const waiting = versions.some(
    (version) =>
      "workflow_status" in version &&
      version.workflow_status === "pending_approval",
  );

  return (
    <div className="border-border/70 mt-4 space-y-3 border-t pt-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold">版本历史</h3>
        <div className="flex flex-wrap gap-2">
          {projectAssetLifecycleActions(item).map((action) => (
            <Button
              key={action}
              type="button"
              size="sm"
              variant={action === "archive" ? "outline" : "destructive"}
              disabled={pending}
              onClick={() => onChangeStatus?.(action)}
            >
              {action === "archive" ? "归档" : "暂停"}
            </Button>
          ))}
        </div>
      </div>
      {isLoading ? (
        <Skeleton className="h-20 w-full" />
      ) : error ? (
        <ErrorNotice error={error} />
      ) : (
        <AssetVersionHistory
          kind={kind}
          versions={versions}
          pending={pending}
          approvalCredentials={approvalCredentials}
          approvalCredentialsLoading={approvalCredentialsLoading}
          approvalCredentialsError={approvalCredentialsError}
          onRetryApprovalCredentials={onRetryApprovalCredentials}
          onPublish={canAuthor ? onPublish : undefined}
          onSubmit={canAuthor && kind === "mcp-servers" ? onSubmit : undefined}
          onApprove={
            canApprove && item.status === "active" && kind === "mcp-servers"
              ? onApprove
              : undefined
          }
        />
      )}
      {waiting && !canApprove && (
        <p className="text-muted-foreground text-sm">等待 Admin 审批</p>
      )}
      <ErrorNotice error={actionError} />
    </div>
  );
}

function ProjectAssetHistory({
  accountId,
  projectId,
  kind,
  item,
}: {
  accountId: string;
  projectId: string;
  kind: MutableKind;
  item: ProjectAssetItem;
}) {
  const history = useProjectAssetVersions(accountId, projectId, kind, item.id);
  const publish = usePublishProjectAssetVersion(accountId, projectId, kind);
  const submit = useSubmitProjectMcpVersion(accountId, projectId);
  const approve = useApproveProjectMcpVersion(accountId, projectId);
  const changeStatus = useChangeProjectAssetStatus(accountId, projectId, kind);
  const canApprove = item.capabilities.includes("mcp.credentials.approve");
  const credentialCatalog = useProjectAssets(
    accountId,
    projectId,
    "credentials",
    kind === "mcp-servers" && canApprove,
  );
  const pending =
    publish.isPending ||
    submit.isPending ||
    approve.isPending ||
    changeStatus.isPending;
  const versions = history.data?.data ?? [];
  const credentialData = credentialCatalog.data as
    | ProjectCredentialList
    | undefined;
  const approvalCredentials = credentialData
    ? credentialData.project_items
    : [];

  return (
    <ProjectAssetHistoryView
      kind={kind}
      item={item}
      versions={versions}
      approvalCredentials={approvalCredentials}
      approvalCredentialsLoading={credentialCatalog.isLoading}
      approvalCredentialsError={credentialCatalog.error}
      onRetryApprovalCredentials={() => void credentialCatalog.refetch()}
      isLoading={history.isLoading}
      error={history.error}
      actionError={
        publish.error ?? submit.error ?? approve.error ?? changeStatus.error
      }
      pending={pending}
      onChangeStatus={(action) =>
        changeStatus.mutate({
          assetId: item.id,
          action,
          input: { expected_asset_version: item.version },
        })
      }
      onPublish={(version) =>
        publish.mutate({
          assetId: item.id,
          versionId: version.id,
          input: { expected_asset_version: item.version },
        })
      }
      onSubmit={(version) =>
        submit.mutate({
          assetId: item.id,
          versionId: version.id,
          input: { expected_asset_version: item.version },
        })
      }
      onApprove={(version, credentialVersions) =>
        approve.mutate({
          assetId: item.id,
          versionId: version.id,
          input: {
            credential_versions: credentialVersions,
            expected_asset_version: item.version,
          },
        })
      }
    />
  );
}

function useSecureProjectCredentialWrite(accountId: string, projectId: string) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  async function run(operation: () => Promise<unknown>): Promise<boolean> {
    setPending(true);
    setErrorMessage(null);
    try {
      await operation();
      await queryClient.invalidateQueries({
        queryKey: projectAssetKey(accountId, projectId, "credentials"),
      });
      return true;
    } catch (error) {
      setErrorMessage(adminAssetErrorMessage(error));
      return false;
    } finally {
      setPending(false);
    }
  }

  return {
    pending,
    errorMessage,
    run,
    clearError: () => setErrorMessage(null),
  };
}

function CredentialHistory({
  accountId,
  projectId,
  credential,
}: {
  accountId: string;
  projectId: string;
  credential: ProjectCredentialItem;
}) {
  const history = useProjectAssetVersions(
    accountId,
    projectId,
    "credentials",
    credential.id,
  );
  return (
    <div className="border-border/70 border-t pt-4">
      <h3 className="mb-3 text-sm font-semibold">版本历史</h3>
      {history.isLoading ? (
        <Skeleton className="h-16 w-full" />
      ) : history.error ? (
        <ErrorNotice error={history.error} />
      ) : (
        <AssetVersionHistory
          kind="credentials"
          versions={history.data?.data ?? []}
        />
      )}
    </div>
  );
}

const PAGE_META: Record<AssetListKind, { title: string; description: string }> =
  {
    agents: {
      title: "项目 Agent",
      description: "查看系统 Agent 绑定，并维护当前项目的 Agent 版本。",
    },
    skills: {
      title: "项目 Skill",
      description: "查看系统 Skill 绑定，并维护完整的项目 Skill 快照。",
    },
    "mcp-servers": {
      title: "项目 MCP",
      description: "维护不含敏感信息的定义；含 Credential 槽位的版本必须审批。",
    },
    credentials: {
      title: "项目 Credential",
      description: "只展示安全元数据；Credential 值写入后永不回显。",
    },
  };

function MutableProjectAssets({
  accountId,
  projectId,
  kind,
}: {
  accountId: string;
  projectId: string;
  kind: MutableKind;
}) {
  const project = useCurrentProject();
  const query = useProjectAssets(accountId, projectId, kind);
  const createAsset = useCreateProjectAsset(accountId, projectId, kind);
  const createVersion = useCreateProjectAssetVersion(
    accountId,
    projectId,
    kind,
  );
  const [createOpen, setCreateOpen] = useState(false);
  const [versionAsset, setVersionAsset] = useState<ProjectAssetItem | null>(
    null,
  );
  const [bindingAssetId, setBindingAssetId] = useState<string | null>(null);

  useEffect(() => {
    if (createAsset.isSuccess) setCreateOpen(false);
  }, [createAsset.isSuccess]);
  useEffect(() => {
    if (createVersion.isSuccess) setVersionAsset(null);
  }, [createVersion.isSuccess]);

  if (query.isLoading) return <Skeleton className="h-72 w-full" />;
  if (query.error) {
    return (
      <div className="border-destructive/30 rounded-xl border p-6">
        <ErrorNotice error={query.error} />
        <Button
          type="button"
          className="mt-4"
          variant="outline"
          onClick={() => void query.refetch()}
        >
          重试
        </Button>
      </div>
    );
  }
  const data = query.data as ProjectAssetList;
  const bindingAsset =
    data.system_items.find((item) => item.id === bindingAssetId) ?? null;
  const canCreate = project.capabilities.includes("shared_assets.edit");
  return (
    <>
      {canCreate && (
        <div className="mb-6 flex justify-end">
          <Button type="button" onClick={() => setCreateOpen(true)}>
            <PlusIcon aria-hidden className="size-4" />
            创建项目资产
          </Button>
        </div>
      )}
      <ProjectAssetCatalogView
        kind={kind}
        data={data}
        onManageBinding={(item) => setBindingAssetId(item.id)}
        onCreateVersion={setVersionAsset}
        renderProjectDetails={(item) => (
          <ProjectAssetHistory
            accountId={accountId}
            projectId={projectId}
            kind={kind}
            item={item}
          />
        )}
      />
      <CreateAssetDialog
        kind={kind}
        scope="project"
        open={createOpen}
        pending={createAsset.isPending}
        errorMessage={
          createAsset.error ? adminAssetErrorMessage(createAsset.error) : null
        }
        onOpenChange={setCreateOpen}
        onSubmit={(input) => createAsset.mutate(input)}
      />
      {versionAsset && (
        <CreateVersionDialog
          kind={kind}
          asset={versionAsset}
          open
          pending={createVersion.isPending}
          errorMessage={
            createVersion.error
              ? adminAssetErrorMessage(createVersion.error)
              : null
          }
          onOpenChange={(open) => !open && setVersionAsset(null)}
          onSubmit={(input: VersionAuthoringInput) =>
            createVersion.mutate({
              assetId: versionAsset.id,
              input,
            })
          }
        />
      )}
      {bindingAsset && (
        <SystemBindingDialog
          accountId={accountId}
          projectId={projectId}
          kind={kind}
          item={bindingAsset}
          open
          onOpenChange={(open) => !open && setBindingAssetId(null)}
        />
      )}
    </>
  );
}

function ProjectCredentials({
  accountId,
  projectId,
}: {
  accountId: string;
  projectId: string;
}) {
  const project = useCurrentProject();
  const query = useProjectAssets(accountId, projectId, "credentials");
  const secureWrite = useSecureProjectCredentialWrite(accountId, projectId);
  const [createOpen, setCreateOpen] = useState(false);
  const [replaceCredential, setReplaceCredential] =
    useState<ProjectCredentialItem | null>(null);

  if (query.isLoading) return <Skeleton className="h-72 w-full" />;
  if (query.error) return <ErrorNotice error={query.error} />;
  const data = query.data as ProjectCredentialList;
  const canCreate = project.capabilities.includes("mcp.credentials.approve");
  return (
    <>
      {secureWrite.errorMessage && (
        <p role="alert" className="text-destructive mb-4 text-sm">
          {secureWrite.errorMessage}
        </p>
      )}
      {canCreate && (
        <div className="mb-6 flex justify-end">
          <Button
            type="button"
            onClick={() => {
              secureWrite.clearError();
              setCreateOpen(true);
            }}
          >
            <PlusIcon aria-hidden className="size-4" />
            创建 Credential
          </Button>
        </div>
      )}
      <ProjectCredentialCatalogView
        data={data}
        pending={secureWrite.pending}
        onReplace={(credential) => {
          secureWrite.clearError();
          setReplaceCredential(credential);
        }}
        onRevoke={(credential) =>
          void secureWrite.run(() =>
            revokeProjectCredential(project.id, credential.id, {
              expected_credential_version: credential.version,
            }),
          )
        }
        renderDetails={(credential) =>
          projectCredentialShowsHistory(credential) ? (
            <CredentialHistory
              accountId={accountId}
              projectId={projectId}
              credential={credential}
            />
          ) : null
        }
      />
      <CredentialSecretDialog
        mode="create"
        open={createOpen}
        pending={secureWrite.pending}
        errorMessage={secureWrite.errorMessage}
        onOpenChange={setCreateOpen}
        onCreate={(input: CreateCredentialInput) => {
          void secureWrite
            .run(() => createProjectCredential(project.id, input))
            .then((success) => success && setCreateOpen(false));
        }}
      />
      {replaceCredential && (
        <CredentialSecretDialog
          mode="replace"
          open
          expectedVersion={replaceCredential.version}
          pending={secureWrite.pending}
          errorMessage={secureWrite.errorMessage}
          onOpenChange={(open) => !open && setReplaceCredential(null)}
          onReplace={(input: ReplaceCredentialInput) => {
            const credential = replaceCredential;
            void secureWrite
              .run(() =>
                replaceProjectCredential(project.id, credential.id, input),
              )
              .then((success) => success && setReplaceCredential(null));
          }}
        />
      )}
    </>
  );
}

function AuthenticatedProjectAssetPage({
  accountId,
  kind,
}: {
  accountId: string;
  kind: AssetListKind;
}) {
  const project = useCurrentProject();
  const meta = PAGE_META[kind];
  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <header className="mb-8">
        <p className="text-primary mb-2 text-sm font-medium">
          {project.display_name} · 共享资产
        </p>
        <h1 className="text-3xl font-semibold tracking-tight">{meta.title}</h1>
        <p className="text-muted-foreground mt-2">{meta.description}</p>
      </header>
      {kind === "credentials" ? (
        <ProjectCredentials accountId={accountId} projectId={project.id} />
      ) : (
        <MutableProjectAssets
          accountId={accountId}
          projectId={project.id}
          kind={kind}
        />
      )}
    </main>
  );
}

export function ProjectAssetsPage({ kind }: { kind: AssetListKind }) {
  const { user } = useAuth();
  if (!user) return null;
  return <AuthenticatedProjectAssetPage accountId={user.id} kind={kind} />;
}

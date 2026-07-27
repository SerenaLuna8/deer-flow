"use client";

import { useQueryClient } from "@tanstack/react-query";
import { PlusIcon } from "lucide-react";
import { useEffect, useState } from "react";

import {
  CreateAssetDialog,
  CreateVersionDialog,
  CredentialGrantMigrationDialog,
  CredentialRevokeDialog,
  CredentialSecretDialog,
  type VersionAuthoringInput,
} from "@/components/admin/assets/admin-asset-dialogs";
import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { AssetVersionHistory } from "@/components/assets/asset-version-history";
import { settleMcpApproval } from "@/components/projects/assets/mcp-approval-dialog";
import {
  ProjectAssetCatalogView,
  ProjectAssetHistoryView,
  ProjectCredentialCatalogView,
  credentialPayloadFieldsFromVersions,
} from "@/components/projects/assets/project-assets-page";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  adminProjectAssetKey,
  createAdminProjectCredential,
  migrateAdminProjectCredentialGrants,
  replaceAdminProjectCredential,
  revokeAdminProjectCredential,
  useAdminProjectAssets,
  useAdminProjectAssetVersions,
  useApproveAdminProjectMcpVersion,
  useChangeAdminProjectAssetStatus,
  useCreateAdminProjectAsset,
  useCreateAdminProjectAssetVersion,
  usePublishAdminProjectAssetVersion,
  useSubmitAdminProjectMcpVersion,
  type AssetListKind,
  type AssetVersion,
  type CreateCredentialInput,
  type ProjectAssetItem,
  type ProjectAssetList,
  type ProjectCredentialItem,
  type ProjectCredentialList,
  type ReplaceCredentialInput,
} from "@/core/shared-assets";

import { AdminProjectSystemBindingDialog } from "./admin-project-system-binding-dialog";

type MutableKind = Exclude<AssetListKind, "credentials">;
type VersionedKind = Exclude<MutableKind, "agents">;
type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;

const PAGE_META: Record<AssetListKind, { title: string; description: string }> =
  {
    agents: {
      title: "项目 Agent 代管",
      description: "查看所选项目的 Agent；系统 Agent 仅可绑定。",
    },
    skills: {
      title: "项目 Skill 代管",
      description: "维护所选项目的完整 Skill 版本；系统 Skill 仅可绑定。",
    },
    "mcp-servers": {
      title: "项目 MCP 代管",
      description:
        "维护所选项目的 MCP 定义、审批和 Credential Grant；系统 MCP 定义保持只读。",
    },
    credentials: {
      title: "项目 Credential 代管",
      description:
        "只治理所选项目的 Credential 安全元数据；凭据值写入后永不回显。",
    },
  };

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <p role="alert" className="text-destructive text-sm">
      {adminAssetErrorMessage(error)}
    </p>
  );
}

function AdminProjectAssetHistory({
  accountId,
  projectId,
  kind,
  item,
}: {
  accountId: string;
  projectId: string;
  kind: VersionedKind;
  item: ProjectAssetItem;
}) {
  const history = useAdminProjectAssetVersions(
    accountId,
    projectId,
    kind,
    item.id,
  );
  const publish = usePublishAdminProjectAssetVersion(
    accountId,
    projectId,
    kind,
  );
  const submit = useSubmitAdminProjectMcpVersion(accountId, projectId);
  const approve = useApproveAdminProjectMcpVersion(accountId, projectId);
  const changeStatus = useChangeAdminProjectAssetStatus(
    accountId,
    projectId,
    kind,
  );
  const credentialCatalog = useAdminProjectAssets(
    accountId,
    projectId,
    "credentials",
    kind === "mcp-servers",
  );
  const credentialData = credentialCatalog.data as
    | ProjectCredentialList
    | undefined;
  const pending =
    publish.isPending ||
    submit.isPending ||
    approve.isPending ||
    changeStatus.isPending;

  return (
    <ProjectAssetHistoryView
      kind={kind}
      item={item}
      versions={history.data?.data ?? []}
      approvalCredentials={credentialData?.project_items ?? []}
      approvalCredentialsLoading={credentialCatalog.isLoading}
      approvalCredentialsError={credentialCatalog.error}
      approvalError={approve.error}
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
      onSubmit={(version: McpVersion) =>
        submit.mutate({
          assetId: item.id,
          versionId: version.id,
          input: { expected_asset_version: item.version },
        })
      }
      onApprove={(version: McpVersion, credentialVersions) =>
        settleMcpApproval(async () => {
          await approve.mutateAsync({
            assetId: item.id,
            versionId: version.id,
            input: {
              credential_versions: credentialVersions,
              expected_asset_version: item.version,
            },
          });
          return true;
        })
      }
    />
  );
}

function MutableAdminProjectAssets({
  accountId,
  projectId,
  kind,
}: {
  accountId: string;
  projectId: string;
  kind: MutableKind;
}) {
  const query = useAdminProjectAssets(accountId, projectId, kind);
  const createAsset = useCreateAdminProjectAsset(accountId, projectId, kind);
  const createVersion = useCreateAdminProjectAssetVersion(
    accountId,
    projectId,
    kind === "agents" ? null : kind,
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
  if (query.error || !query.data) {
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
  return (
    <>
      {kind !== "agents" ? (
        <div className="mb-6 flex justify-end">
          <Button type="button" onClick={() => setCreateOpen(true)}>
            <PlusIcon aria-hidden className="size-4" />
            创建项目资产
          </Button>
        </div>
      ) : null}
      <ProjectAssetCatalogView
        kind={kind}
        data={data}
        onManageBinding={(item) => setBindingAssetId(item.id)}
        onCreateVersion={
          kind === "agents" ? undefined : (item) => setVersionAsset(item)
        }
        renderProjectDetails={
          kind === "agents"
            ? undefined
            : (item) => (
                <AdminProjectAssetHistory
                  accountId={accountId}
                  projectId={projectId}
                  kind={kind}
                  item={item}
                />
              )
        }
      />
      {kind !== "agents" ? (
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
      ) : null}
      {versionAsset && kind !== "agents" ? (
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
            createVersion.mutate({ assetId: versionAsset.id, input })
          }
        />
      ) : null}
      {bindingAsset ? (
        <AdminProjectSystemBindingDialog
          accountId={accountId}
          projectId={projectId}
          kind={kind}
          item={bindingAsset}
          open
          onOpenChange={(open) => !open && setBindingAssetId(null)}
        />
      ) : null}
    </>
  );
}

function useSecureAdminProjectCredentialWrite(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [noticeMessage, setNoticeMessage] = useState<string | null>(null);

  async function run(
    operation: () => Promise<unknown>,
    successMessage?: string,
  ): Promise<boolean> {
    setPending(true);
    setErrorMessage(null);
    setNoticeMessage(null);
    try {
      await operation();
      await queryClient.invalidateQueries({
        queryKey: adminProjectAssetKey(accountId, projectId, "credentials"),
      });
      setNoticeMessage(successMessage ?? null);
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
    noticeMessage,
    run,
    clearMessage: () => {
      setErrorMessage(null);
      setNoticeMessage(null);
    },
  };
}

function AdminProjectCredentialHistory({
  accountId,
  projectId,
  credential,
}: {
  accountId: string;
  projectId: string;
  credential: ProjectCredentialItem;
}) {
  const history = useAdminProjectAssetVersions(
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
          scope="project"
          versions={history.data?.data ?? []}
        />
      )}
    </div>
  );
}

function AdminProjectCredentialReplaceDialog({
  accountId,
  projectId,
  credential,
  secureWrite,
  onClose,
}: {
  accountId: string;
  projectId: string;
  credential: ProjectCredentialItem;
  secureWrite: ReturnType<typeof useSecureAdminProjectCredentialWrite>;
  onClose: () => void;
}) {
  const history = useAdminProjectAssetVersions(
    accountId,
    projectId,
    "credentials",
    credential.id,
  );
  const initialFields = history.data
    ? credentialPayloadFieldsFromVersions(
        history.data.data,
        credential.current_version_id,
      )
    : null;
  const errorMessage = history.error
    ? adminAssetErrorMessage(history.error)
    : history.data && !initialFields
      ? "无法确认当前 Credential 的字段结构，请重新加载后再试。"
      : secureWrite.errorMessage;

  return (
    <CredentialSecretDialog
      mode="replace"
      open
      expectedVersion={credential.version}
      initialFields={initialFields ?? []}
      disabled={history.isLoading || Boolean(history.error) || !initialFields}
      pending={secureWrite.pending}
      errorMessage={errorMessage}
      onRetry={history.error ? () => void history.refetch() : undefined}
      onOpenChange={(open) => !open && onClose()}
      onReplace={(input: ReplaceCredentialInput) => {
        void secureWrite
          .run(() =>
            replaceAdminProjectCredential(projectId, credential.id, input),
          )
          .then((success) => success && onClose());
      }}
    />
  );
}

function AdminProjectCredentials({
  accountId,
  projectId,
}: {
  accountId: string;
  projectId: string;
}) {
  const query = useAdminProjectAssets(accountId, projectId, "credentials");
  const secureWrite = useSecureAdminProjectCredentialWrite(
    accountId,
    projectId,
  );
  const [createOpen, setCreateOpen] = useState(false);
  const [replaceCredential, setReplaceCredential] =
    useState<ProjectCredentialItem | null>(null);
  const [migrateCredential, setMigrateCredential] =
    useState<ProjectCredentialItem | null>(null);
  const [revokeCredential, setRevokeCredential] =
    useState<ProjectCredentialItem | null>(null);

  if (query.isLoading) return <Skeleton className="h-72 w-full" />;
  if (query.error || !query.data) return <ErrorNotice error={query.error} />;
  const data = query.data as ProjectCredentialList;

  return (
    <>
      {secureWrite.errorMessage ? (
        <p role="alert" className="text-destructive mb-4 text-sm">
          {secureWrite.errorMessage}
        </p>
      ) : null}
      {secureWrite.noticeMessage ? (
        <p
          role="status"
          className="border-border bg-muted/30 text-muted-foreground mb-4 rounded-xl border px-4 py-3 text-sm"
        >
          {secureWrite.noticeMessage}
        </p>
      ) : null}
      <div className="mb-6 flex justify-end">
        <Button
          type="button"
          onClick={() => {
            secureWrite.clearMessage();
            setCreateOpen(true);
          }}
        >
          <PlusIcon aria-hidden className="size-4" />
          创建项目 Credential
        </Button>
      </div>
      <ProjectCredentialCatalogView
        data={data}
        pending={secureWrite.pending}
        onReplace={(credential) => {
          secureWrite.clearMessage();
          setReplaceCredential(credential);
        }}
        onMigrate={(credential) => {
          secureWrite.clearMessage();
          setMigrateCredential(credential);
        }}
        onRevoke={(credential) => {
          secureWrite.clearMessage();
          setRevokeCredential(credential);
        }}
        renderDetails={(credential) => (
          <AdminProjectCredentialHistory
            accountId={accountId}
            projectId={projectId}
            credential={credential}
          />
        )}
      />
      <CredentialSecretDialog
        mode="create"
        open={createOpen}
        pending={secureWrite.pending}
        errorMessage={secureWrite.errorMessage}
        onOpenChange={setCreateOpen}
        onCreate={(input: CreateCredentialInput) => {
          void secureWrite
            .run(() => createAdminProjectCredential(projectId, input))
            .then((success) => success && setCreateOpen(false));
        }}
      />
      {replaceCredential ? (
        <AdminProjectCredentialReplaceDialog
          accountId={accountId}
          projectId={projectId}
          credential={replaceCredential}
          secureWrite={secureWrite}
          onClose={() => setReplaceCredential(null)}
        />
      ) : null}
      {migrateCredential ? (
        <CredentialGrantMigrationDialog
          open
          credentialName={migrateCredential.display_name}
          pending={secureWrite.pending}
          onOpenChange={(open) => !open && setMigrateCredential(null)}
          onConfirm={() => {
            void secureWrite
              .run(
                () =>
                  migrateAdminProjectCredentialGrants(
                    projectId,
                    migrateCredential.id,
                    {
                      expected_credential_version: migrateCredential.version,
                    },
                  ),
                "已完成兼容 Grant 迁移；没有待迁移 Grant 时不会更改授权。",
              )
              .then((success) => success && setMigrateCredential(null));
          }}
        />
      ) : null}
      {revokeCredential ? (
        <CredentialRevokeDialog
          open
          credentialName={revokeCredential.display_name}
          pending={secureWrite.pending}
          onOpenChange={(open) => !open && setRevokeCredential(null)}
          onConfirm={() => {
            void secureWrite
              .run(() =>
                revokeAdminProjectCredential(projectId, revokeCredential.id, {
                  expected_credential_version: revokeCredential.version,
                }),
              )
              .then((success) => success && setRevokeCredential(null));
          }}
        />
      ) : null}
    </>
  );
}

export function AdminProjectAssetPage({
  projectId,
  kind,
}: {
  projectId: string;
  kind: AssetListKind;
}) {
  const { user } = useAuth();
  if (!user || user.system_role !== "system_admin") return null;
  const meta = PAGE_META[kind];
  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <header className="mb-7 max-w-3xl">
        <p className="text-primary mb-2 text-sm font-medium">
          System admin · 当前项目共享资产
        </p>
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
          {meta.title}
        </h1>
        <p className="text-muted-foreground mt-2 text-sm sm:text-base">
          {meta.description}
        </p>
      </header>
      {kind === "credentials" ? (
        <AdminProjectCredentials accountId={user.id} projectId={projectId} />
      ) : (
        <MutableAdminProjectAssets
          accountId={user.id}
          projectId={projectId}
          kind={kind}
        />
      )}
    </main>
  );
}

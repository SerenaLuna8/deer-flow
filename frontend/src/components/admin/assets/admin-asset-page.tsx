"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  ArchiveIcon,
  BotIcon,
  KeyRoundIcon,
  NetworkIcon,
  PlusIcon,
  SparklesIcon,
} from "lucide-react";
import { useState } from "react";

import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { AssetVersionHistory } from "@/components/assets/asset-version-history";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  adminAssetKey,
  createAdminCredential,
  migrateAdminCredentialGrants,
  replaceAdminCredential,
  revokeAdminCredential,
  useAdminAssets,
  useAdminAssetVersions,
  useAdminCredentialRotationStatus,
  useConfigureAdminMcpCredentialGrants,
  type AdminAssetList,
  type AdminCredentialList,
  type AssetListKind,
  type AssetSummary,
  type CreateCredentialInput,
  type CredentialMetadata,
  type ReplaceCredentialInput,
} from "@/core/shared-assets";

import {
  CredentialGrantMigrationDialog,
  CredentialRevokeDialog,
  CredentialSecretDialog,
} from "./admin-asset-dialogs";
import { adminAssetErrorMessage } from "./admin-asset-view-model";
import { CredentialRotationStatusCard } from "./credential-rotation-status";

export {
  adminAssetErrorMessage,
  assetLifecycleActions,
  versionWorkflowActions,
} from "./admin-asset-view-model";

type SystemCatalogKind = Exclude<AssetListKind, "credentials">;

const PAGE_META = {
  agents: {
    title: "系统 Agent",
    description: "查看由 packaged catalog 初始化的系统 Agent 治理元数据。",
    label: "Agent",
    icon: BotIcon,
  },
  skills: {
    title: "系统 Skill",
    description: "查看由 packaged catalog 初始化的系统 Skill 与版本元数据。",
    label: "Skill",
    icon: SparklesIcon,
  },
  "mcp-servers": {
    title: "系统 MCP",
    description: "查看由 packaged catalog 初始化的系统 MCP 治理元数据。",
    label: "MCP",
    icon: NetworkIcon,
  },
  credentials: {
    title: "系统 Credential",
    description: "只展示凭据元数据；凭据值写入后永不回显。",
    label: "Credential",
    icon: KeyRoundIcon,
  },
} as const;

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <p role="alert" className="text-destructive text-sm">
      {adminAssetErrorMessage(error)}
    </p>
  );
}

function SystemAssetCard({
  accountId,
  kind,
  asset,
}: {
  accountId: string;
  kind: SystemCatalogKind;
  asset: AssetSummary;
}) {
  const history = useAdminAssetVersions(accountId, kind, asset.id);
  const configureGrants = useConfigureAdminMcpCredentialGrants(accountId);
  const credentialCatalog = useAdminAssets(
    accountId,
    "credentials",
    kind === "mcp-servers",
  );

  return (
    <Card data-testid={`asset-card-${asset.id}`}>
      <CardHeader>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <CardTitle className="truncate">{asset.display_name}</CardTitle>
            <p className="text-muted-foreground mt-1 font-mono text-xs">
              {asset.slug}
            </p>
          </div>
          <AssetStatusBadge status={asset.status} />
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        <dl className="grid gap-3 text-sm sm:grid-cols-3">
          <div>
            <dt className="text-muted-foreground text-xs">资产版本</dt>
            <dd>{asset.version}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">当前发布版本</dt>
            <dd className="truncate font-mono text-xs">
              {asset.current_published_version_id ?? "—"}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">更新时间</dt>
            <dd>{new Date(asset.updated_at).toLocaleString("zh-CN")}</dd>
          </div>
        </dl>

        <section aria-label={`${asset.display_name} 版本历史`}>
          <h3 className="mb-3 text-sm font-semibold">版本历史</h3>
          {history.isLoading ? (
            <Skeleton className="h-20 w-full" />
          ) : history.error ? (
            <div className="space-y-2">
              <ErrorNotice error={history.error} />
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => void history.refetch()}
              >
                重试
              </Button>
            </div>
          ) : (
            <AssetVersionHistory
              kind={kind}
              scope="system"
              versions={history.data?.data ?? []}
              pending={configureGrants.isPending}
              approvalCredentials={
                (credentialCatalog.data as AdminCredentialList | undefined)
                  ?.items ?? []
              }
              approvalCredentialScope="system"
              approvalCredentialsLoading={credentialCatalog.isLoading}
              approvalCredentialsError={credentialCatalog.error}
              approvalError={configureGrants.error}
              configureCredentialGrantsVersionId={
                asset.current_published_version_id
              }
              onRetryApprovalCredentials={() =>
                void credentialCatalog.refetch()
              }
              onConfigureCredentialGrants={async (
                version,
                credentialVersions,
                expectedActiveGrantVersions,
              ) => {
                try {
                  await configureGrants.mutateAsync({
                    assetId: asset.id,
                    versionId: version.id,
                    input: {
                      credential_versions: credentialVersions,
                      expected_active_grant_versions:
                        expectedActiveGrantVersions,
                    },
                  });
                  return true;
                } catch {
                  return false;
                }
              }}
            />
          )}
        </section>
      </CardContent>
    </Card>
  );
}

export function CredentialMetadataCard({
  credential,
  pending = false,
  onReplace,
  onMigrate,
  onRevoke,
}: {
  credential: CredentialMetadata;
  pending?: boolean;
  onReplace: () => void;
  onMigrate: () => void;
  onRevoke: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2">
              <KeyRoundIcon aria-hidden className="size-4" />
              <span className="truncate">{credential.display_name}</span>
            </CardTitle>
            <p className="text-muted-foreground mt-1 font-mono text-xs">
              {credential.name}
            </p>
          </div>
          <AssetStatusBadge status={credential.status} />
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid gap-2 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-muted-foreground text-xs">类型</dt>
            <dd>{credential.credential_type}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">元数据版本</dt>
            <dd>{credential.version}</dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-muted-foreground text-xs">更新时间</dt>
            <dd>{new Date(credential.updated_at).toLocaleString("zh-CN")}</dd>
          </div>
        </dl>
        {credential.status === "active" && (
          <div className="space-y-3">
            {credential.version > 1 && (
              <div
                role="note"
                className="border-border bg-muted/30 text-muted-foreground rounded-lg border px-3 py-2 text-xs"
              >
                替换只创建新版本；既有 Grant 仍固定到 retired
                version，轮换需要显式迁移。
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={pending}
                onClick={onReplace}
              >
                替换凭据
              </Button>
              {credential.version > 1 && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={pending}
                  onClick={onMigrate}
                >
                  迁移兼容 Grant
                </Button>
              )}
              <Button
                type="button"
                size="sm"
                variant="destructive"
                disabled={pending}
                onClick={onRevoke}
              >
                撤销凭据
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function useSecureCredentialWrite(accountId: string) {
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
        queryKey: adminAssetKey(accountId, "credentials"),
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
    clearError: () => {
      setErrorMessage(null);
      setNoticeMessage(null);
    },
  };
}

function CredentialCardWithHistory({
  accountId,
  credential,
  secureWrite,
}: {
  accountId: string;
  credential: CredentialMetadata;
  secureWrite: ReturnType<typeof useSecureCredentialWrite>;
}) {
  const [replaceOpen, setReplaceOpen] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [migrationOpen, setMigrationOpen] = useState(false);
  const history = useAdminAssetVersions(
    accountId,
    "credentials",
    credential.id,
  );

  return (
    <div className="space-y-3" data-testid={`credential-card-${credential.id}`}>
      <CredentialMetadataCard
        credential={credential}
        pending={secureWrite.pending}
        onReplace={() => {
          secureWrite.clearError();
          setReplaceOpen(true);
        }}
        onMigrate={() => {
          secureWrite.clearError();
          setMigrationOpen(true);
        }}
        onRevoke={() => {
          secureWrite.clearError();
          setRevokeOpen(true);
        }}
      />
      <div className="border-border/70 rounded-xl border p-4">
        <h3 className="mb-3 text-sm font-semibold">版本历史</h3>
        {history.isLoading ? (
          <Skeleton className="h-16 w-full" />
        ) : history.error ? (
          <ErrorNotice error={history.error} />
        ) : (
          <AssetVersionHistory
            kind="credentials"
            scope="system"
            versions={history.data?.data ?? []}
          />
        )}
      </div>
      <CredentialSecretDialog
        mode="replace"
        open={replaceOpen}
        expectedVersion={credential.version}
        pending={secureWrite.pending}
        errorMessage={secureWrite.errorMessage}
        onOpenChange={setReplaceOpen}
        onReplace={(input: ReplaceCredentialInput) => {
          void secureWrite
            .run(() => replaceAdminCredential(credential.id, input))
            .then((success) => success && setReplaceOpen(false));
        }}
      />
      <CredentialGrantMigrationDialog
        open={migrationOpen}
        credentialName={credential.display_name}
        pending={secureWrite.pending}
        onOpenChange={setMigrationOpen}
        onConfirm={() => {
          void secureWrite
            .run(
              () =>
                migrateAdminCredentialGrants(credential.id, {
                  expected_credential_version: credential.version,
                }),
              "已完成兼容 Grant 迁移；没有待迁移 Grant 时不会更改授权。",
            )
            .then((success) => success && setMigrationOpen(false));
        }}
      />
      <CredentialRevokeDialog
        open={revokeOpen}
        credentialName={credential.display_name}
        pending={secureWrite.pending}
        onOpenChange={setRevokeOpen}
        onConfirm={() => {
          void secureWrite
            .run(() =>
              revokeAdminCredential(credential.id, {
                expected_credential_version: credential.version,
              }),
            )
            .then((success) => success && setRevokeOpen(false));
        }}
      />
    </div>
  );
}

export function CredentialWriteError({ message }: { message: string | null }) {
  if (!message) {
    return null;
  }

  return (
    <div
      role="alert"
      className="border-destructive/30 bg-destructive/5 text-destructive mb-6 rounded-xl border px-4 py-3 text-sm"
    >
      {message}
    </div>
  );
}

function CredentialList({
  accountId,
  data,
}: {
  accountId: string;
  data: AdminCredentialList;
}) {
  const [createOpen, setCreateOpen] = useState(false);
  const secureWrite = useSecureCredentialWrite(accountId);
  const rotationStatus = useAdminCredentialRotationStatus(accountId);

  return (
    <>
      <div className="mb-6">
        {rotationStatus.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : rotationStatus.error ? (
          <div className="border-destructive/30 bg-destructive/5 rounded-xl border p-4">
            <ErrorNotice error={rotationStatus.error} />
          </div>
        ) : rotationStatus.data ? (
          <CredentialRotationStatusCard status={rotationStatus.data} />
        ) : null}
      </div>
      <CredentialWriteError message={secureWrite.errorMessage} />
      {secureWrite.noticeMessage && (
        <p
          role="status"
          className="border-border bg-muted/30 text-muted-foreground mb-6 rounded-xl border px-4 py-3 text-sm"
        >
          {secureWrite.noticeMessage}
        </p>
      )}
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
      {data.items.length === 0 ? (
        <EmptyState label="Credential" />
      ) : (
        <div className="grid gap-5 xl:grid-cols-2">
          {data.items.map((credential) => (
            <CredentialCardWithHistory
              key={credential.id}
              accountId={accountId}
              credential={credential}
              secureWrite={secureWrite}
            />
          ))}
        </div>
      )}
      <CredentialSecretDialog
        mode="create"
        open={createOpen}
        pending={secureWrite.pending}
        errorMessage={secureWrite.errorMessage}
        onOpenChange={setCreateOpen}
        onCreate={(input: CreateCredentialInput) => {
          void secureWrite
            .run(() => createAdminCredential(input))
            .then((success) => success && setCreateOpen(false));
        }}
      />
    </>
  );
}

function EmptyState({
  label,
  bootstrapOnly = false,
}: {
  label: string;
  bootstrapOnly?: boolean;
}) {
  return (
    <div className="border-border/70 bg-muted/20 rounded-xl border border-dashed p-12 text-center">
      <ArchiveIcon
        aria-hidden
        className="text-muted-foreground mx-auto size-8"
      />
      <p className="mt-3 font-medium">暂无系统 {label}</p>
      <p className="text-muted-foreground mt-1 text-sm">
        {bootstrapOnly
          ? "当前 packaged catalog 中没有该类系统资产。"
          : "使用页面上方的创建按钮添加第一项。"}
      </p>
    </div>
  );
}

function SystemAssetList({
  accountId,
  kind,
  data,
}: {
  accountId: string;
  kind: SystemCatalogKind;
  data: AdminAssetList;
}) {
  return (
    <div>
      <div
        role="note"
        className="border-border bg-muted/30 text-muted-foreground mb-6 rounded-xl border px-4 py-3 text-sm"
      >
        系统 {PAGE_META[kind].label} 由版本化、摘要校验的 packaged catalog
        在数据库初始化时写入，运行期只读。更新系统资产需修改仓库内的 packaged
        catalog，并通过显式数据库初始化流程发布。
      </div>
      {data.items.length === 0 ? (
        <EmptyState label={PAGE_META[kind].label} bootstrapOnly />
      ) : (
        <div className="grid gap-5">
          {data.items.map((asset) => (
            <SystemAssetCard
              key={asset.id}
              accountId={accountId}
              kind={kind}
              asset={asset}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function AuthenticatedAdminAssetPage({
  accountId,
  kind,
}: {
  accountId: string;
  kind: AssetListKind;
}) {
  const query = useAdminAssets(accountId, kind);
  const meta = PAGE_META[kind];
  const Icon = meta.icon;

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <header className="mb-8">
        <div className="text-primary mb-2 flex items-center gap-2 text-sm font-medium">
          <Icon aria-hidden className="size-4" />
          平台共享资产
        </div>
        <h1 className="text-3xl font-semibold tracking-tight">{meta.title}</h1>
        <p className="text-muted-foreground mt-2">{meta.description}</p>
      </header>

      {query.isLoading ? (
        <div className="space-y-4" aria-label="正在加载">
          <Skeleton className="h-52 w-full" />
          <Skeleton className="h-52 w-full" />
        </div>
      ) : query.error ? (
        <div className="border-destructive/30 bg-destructive/5 rounded-xl border p-6">
          <h2 className="font-semibold">资产加载失败</h2>
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
      ) : kind === "credentials" ? (
        <CredentialList
          accountId={accountId}
          data={query.data as AdminCredentialList}
        />
      ) : (
        <SystemAssetList
          accountId={accountId}
          kind={kind}
          data={query.data as AdminAssetList}
        />
      )}
    </main>
  );
}

export function AdminAssetPage({ kind }: { kind: AssetListKind }) {
  const { user } = useAuth();

  if (user === null) {
    return null;
  }

  return <AuthenticatedAdminAssetPage accountId={user.id} kind={kind} />;
}

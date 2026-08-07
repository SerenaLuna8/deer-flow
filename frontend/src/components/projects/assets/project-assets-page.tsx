"use client";

import { useQueryClient } from "@tanstack/react-query";
import { KeyRoundIcon, PlusIcon } from "lucide-react";
import { useState } from "react";

import {
  CredentialGrantMigrationDialog,
  CredentialRevokeDialog,
  CredentialSecretDialog,
} from "@/components/admin/assets/admin-asset-dialogs";
import {
  adminAssetErrorMessage,
  adminCredentialTypeLabel,
} from "@/components/admin/assets/admin-asset-view-model";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { AssetVersionHistory } from "@/components/assets/asset-version-history";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useI18n } from "@/core/i18n/hooks";
import {
  createProjectCredential,
  deleteProjectCredential,
  migrateProjectCredentialGrants,
  projectAssetKey,
  projectAssetVersionsKey,
  replaceProjectCredential,
  revokeProjectCredential,
  useProjectAssets,
  useProjectAssetVersions,
  type AdminProjectAssetStatusAction,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetList,
  type ProjectAssetItem,
  type ProjectCredentialList,
  type ProjectCredentialItem,
  type CreateCredentialInput,
  CREDENTIAL_PAYLOAD_GROUPS,
  type CredentialPayloadGroup,
  type ReplaceCredentialInput,
} from "@/core/shared-assets";

import { useCurrentProject } from "../project-context";

import {
  CredentialDeleteDialog,
  createCredentialDeleteSnapshot,
  type CredentialDeleteSnapshot,
} from "./credential-delete-dialog";
import { ProjectAssetSection } from "./project-asset-section";
import {
  adminProjectAssetDetailLifecycleActions,
  projectAssetCanAuthor,
} from "./project-asset-view-model";
import { SystemAssetSection } from "./system-asset-section";

type MutableKind = Exclude<AssetListKind, "credentials">;

export type CredentialPayloadField = {
  group: CredentialPayloadGroup;
  field: string;
};

export function credentialPayloadFieldsFromVersions(
  versions: AssetVersion[],
  currentVersionId: string | null,
): CredentialPayloadField[] | null {
  if (!currentVersionId) return null;
  const current = versions.find(
    (version) =>
      "credential_id" in version &&
      version.id === currentVersionId &&
      version.status === "active",
  );
  if (!current || !("credential_id" in current)) return null;

  const schemaGroups = Object.keys(current.payload_schema);
  if (
    schemaGroups.length === 0 ||
    schemaGroups.some(
      (group) =>
        !CREDENTIAL_PAYLOAD_GROUPS.includes(group as CredentialPayloadGroup),
    )
  ) {
    return null;
  }

  const result: CredentialPayloadField[] = [];
  for (const group of CREDENTIAL_PAYLOAD_GROUPS) {
    const fields = current.payload_schema[group];
    if (!fields) continue;
    if (
      fields.length === 0 ||
      new Set(fields).size !== fields.length ||
      fields.some((field) => !field || field.length > 255)
    ) {
      return null;
    }
    for (const field of fields) result.push({ group, field });
  }
  return result.length > 0 ? result : null;
}

export function ProjectAssetCatalogView({
  kind,
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
        kind={kind}
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
  actions,
  onReplace,
  onMigrate,
  onRevoke,
  onDelete,
  renderDetails,
}: {
  data: ProjectCredentialList;
  pending?: boolean;
  actions?: React.ReactNode;
  onReplace?: (credential: ProjectCredentialItem) => void;
  onMigrate?: (credential: ProjectCredentialItem) => void;
  onRevoke?: (credential: ProjectCredentialItem) => void;
  onDelete?: (credential: ProjectCredentialItem) => void;
  renderDetails?: (credential: ProjectCredentialItem) => React.ReactNode;
}) {
  const { locale, t } = useI18n();
  const [source, setSource] = useState<"system" | "project">(() =>
    data.project_items.length === 0 && data.system_items.length > 0
      ? "system"
      : "project",
  );
  const groups = [
    {
      value: "system",
      label: t.adminAssets.common.systemProvided,
      title: t.adminAssets.catalog.systemCredentials,
      items: data.system_items,
    },
    {
      value: "project",
      label: t.adminAssets.common.projectOwned,
      title: t.adminAssets.catalog.projectCredentials,
      items: data.project_items,
    },
  ] as const;
  return (
    <Tabs
      value={source}
      onValueChange={(value) => setSource(value as typeof source)}
      className="gap-0"
    >
      <div className="flex flex-col gap-3 border-b pb-3 sm:flex-row sm:items-center sm:justify-between">
        <TabsList
          variant="line"
          aria-label={t.adminAssets.catalog.credentialSource}
        >
          {groups.map((group) => (
            <TabsTrigger key={group.value} value={group.value}>
              {group.label}
              <span className="text-muted-foreground text-xs tabular-nums">
                {group.items.length}
              </span>
            </TabsTrigger>
          ))}
        </TabsList>
        {source === "project" ? actions : null}
      </div>

      {groups.map(({ value, title, items }) => (
        <TabsContent key={value} value={value} className="pt-4">
          {items.length === 0 ? (
            <p className="text-muted-foreground rounded-xl border border-dashed p-8 text-center text-sm">
              {t.adminAssets.catalog.emptyCredentials(title)}
            </p>
          ) : (
            <div className="grid gap-4 xl:grid-cols-2">
              {items.map((credential) => (
                <Card key={credential.id} className="relative gap-4 py-4">
                  <CardHeader className="px-4">
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
                      <AssetStatusBadge
                        status={credential.status}
                        label={
                          credential.status === "active"
                            ? t.adminAssets.common.active
                            : t.adminAssets.common.revoked
                        }
                      />
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-4 px-4">
                    <dl className="grid gap-2 text-sm sm:grid-cols-2">
                      <div>
                        <dt className="text-muted-foreground text-xs">
                          {t.adminAssets.common.type}
                        </dt>
                        <dd>
                          {adminCredentialTypeLabel(
                            credential.credential_type,
                            t.adminAssets.common.credentialTypes,
                          )}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground text-xs">
                          {t.adminAssets.common.metadataVersion}
                        </dt>
                        <dd>{credential.version}</dd>
                      </div>
                      <div className="sm:col-span-2">
                        <dt className="text-muted-foreground text-xs">
                          {t.adminAssets.common.updatedAt}
                        </dt>
                        <dd>
                          {new Date(credential.updated_at).toLocaleString(
                            locale,
                          )}
                        </dd>
                      </div>
                    </dl>
                    {projectCredentialCanDelete(credential) && (
                      <div className="space-y-3">
                        {credential.status === "active" &&
                          credential.version > 1 && (
                            <div
                              role="note"
                              className="border-border bg-muted/30 text-muted-foreground rounded-lg border px-3 py-2 text-xs"
                            >
                              {t.adminAssets.common.credentialRotationNote}
                            </div>
                          )}
                        <div className="flex flex-wrap gap-2">
                          {credential.status === "active" ? (
                            <>
                              <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                disabled={pending}
                                onClick={() => onReplace?.(credential)}
                              >
                                {t.adminAssets.common.replaceCredential}
                              </Button>
                              {credential.version > 1 && (
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="outline"
                                  disabled={pending}
                                  onClick={() => onMigrate?.(credential)}
                                >
                                  {t.adminAssets.common.migrateReferences}
                                </Button>
                              )}
                              <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                disabled={pending}
                                onClick={() => onRevoke?.(credential)}
                              >
                                {t.adminAssets.common.revokeCredential}
                              </Button>
                            </>
                          ) : null}
                          {onDelete ? (
                            <Button
                              type="button"
                              size="sm"
                              variant="destructive"
                              disabled={pending}
                              onClick={() => onDelete(credential)}
                            >
                              {t.adminAssets.common.delete}
                            </Button>
                          ) : null}
                        </div>
                      </div>
                    )}
                    {renderDetails?.(credential)}
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </TabsContent>
      ))}
    </Tabs>
  );
}

export function projectCredentialCanDelete(
  credential: Pick<ProjectCredentialItem, "scope" | "capabilities">,
): boolean {
  return (
    credential.scope === "project" &&
    credential.capabilities.includes("mcp.credentials.approve")
  );
}

export function projectCredentialShowsHistory(
  credential: Pick<ProjectCredentialItem, "scope">,
): boolean {
  return credential.scope === "project";
}

export { projectAssetCanAuthor };

function ErrorNotice({ error }: { error: unknown }) {
  const { t } = useI18n();
  if (!error) return null;
  return (
    <p role="alert" className="text-destructive text-sm">
      {adminAssetErrorMessage(error, t.adminAssets.errors)}
    </p>
  );
}

type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;

export function ProjectAssetHistoryView<Kind extends MutableKind>({
  kind,
  item,
  versions,
  approvalCredentials = [],
  approvalCredentialsLoading = false,
  approvalCredentialsError,
  approvalError,
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
  kind: Kind;
  item: ProjectAssetItem;
  versions: AssetVersion[];
  approvalCredentials?: ProjectCredentialItem[];
  approvalCredentialsLoading?: boolean;
  approvalCredentialsError?: unknown;
  approvalError?: unknown;
  isLoading?: boolean;
  error?: unknown;
  actionError?: unknown;
  pending?: boolean;
  onChangeStatus?: (action: AdminProjectAssetStatusAction<Kind>) => void;
  onPublish?: (version: AssetVersion) => void;
  onSubmit?: (version: McpVersion) => void;
  onApprove?: (
    version: McpVersion,
    credentialVersions: Record<string, string>,
  ) => boolean | void | Promise<boolean | void>;
  onRetryApprovalCredentials?: () => void;
}) {
  const { t } = useI18n();
  const canAuthor = projectAssetCanAuthor(item, kind);
  const canApprove = item.capabilities.includes("mcp.credentials.approve");
  const waiting = versions.some(
    (version) =>
      "workflow_status" in version &&
      version.workflow_status === "pending_approval",
  );

  return (
    <div className="border-border/70 mt-4 space-y-3 border-t pt-4">
      <div className="flex items-center justify-between gap-3">
        {kind !== "mcp-servers" ? (
          <h3 className="text-sm font-semibold">
            {t.adminAssets.common.versionHistory}
          </h3>
        ) : null}
        <div className="flex flex-wrap gap-2">
          {adminProjectAssetDetailLifecycleActions(kind, item).map((action) => (
            <Button
              key={action}
              type="button"
              size="sm"
              variant={action === "archive" ? "outline" : "destructive"}
              disabled={pending}
              onClick={() => onChangeStatus?.(action)}
            >
              {action === "archive"
                ? t.adminAssets.catalog.archive
                : action === "activate"
                  ? t.adminAssets.catalog.activate
                  : kind === "agents"
                    ? t.adminAssets.catalog.disable
                    : t.adminAssets.catalog.suspend}
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
          scope={item.scope}
          versions={versions}
          currentVersionId={item.current_published_version_id}
          pending={pending}
          approvalCredentials={approvalCredentials}
          approvalCredentialsLoading={approvalCredentialsLoading}
          approvalCredentialsError={approvalCredentialsError}
          approvalError={approvalError}
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
        <p className="text-muted-foreground text-sm">
          {t.adminAssets.catalog.waitingForAdmin}
        </p>
      )}
      <ErrorNotice error={actionError} />
    </div>
  );
}

function useSecureProjectCredentialWrite(accountId: string, projectId: string) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [noticeMessage, setNoticeMessage] = useState<string | null>(null);

  async function run(
    operation: () => Promise<unknown>,
    successMessage?: string,
    onSuccess?: () => void,
  ): Promise<boolean> {
    setPending(true);
    setErrorMessage(null);
    setNoticeMessage(null);
    try {
      await operation();
      onSuccess?.();
      await queryClient.invalidateQueries({
        queryKey: projectAssetKey(accountId, projectId, "credentials"),
      });
      setNoticeMessage(successMessage ?? null);
      return true;
    } catch (error) {
      setErrorMessage(adminAssetErrorMessage(error, t.adminAssets.errors));
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

function CredentialHistory({
  accountId,
  projectId,
  credential,
}: {
  accountId: string;
  projectId: string;
  credential: ProjectCredentialItem;
}) {
  const { t } = useI18n();
  const history = useProjectAssetVersions(
    accountId,
    projectId,
    "credentials",
    credential.id,
  );
  return (
    <div className="border-border/70 border-t pt-4">
      <h3 className="mb-3 text-sm font-semibold">
        {t.adminAssets.common.versionHistory}
      </h3>
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

export function ProjectCredentialsWorkspace({
  accountId,
  projectId,
}: {
  accountId: string;
  projectId: string;
}) {
  const { t } = useI18n();
  const project = useCurrentProject();
  const queryClient = useQueryClient();
  const query = useProjectAssets(accountId, projectId, "credentials");
  const secureWrite = useSecureProjectCredentialWrite(accountId, projectId);
  const [createOpen, setCreateOpen] = useState(false);
  const [replaceCredential, setReplaceCredential] =
    useState<ProjectCredentialItem | null>(null);
  const [credentialToRevoke, setCredentialToRevoke] =
    useState<ProjectCredentialItem | null>(null);
  const [credentialToMigrate, setCredentialToMigrate] =
    useState<ProjectCredentialItem | null>(null);
  const [credentialDeleteSnapshot, setCredentialDeleteSnapshot] =
    useState<CredentialDeleteSnapshot | null>(null);

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
      {secureWrite.noticeMessage && (
        <p
          role="status"
          className="border-border bg-muted/30 text-muted-foreground mb-4 rounded-xl border px-4 py-3 text-sm"
        >
          {secureWrite.noticeMessage}
        </p>
      )}
      <ProjectCredentialCatalogView
        data={data}
        pending={secureWrite.pending}
        actions={
          canCreate ? (
            <Button
              type="button"
              size="sm"
              onClick={() => {
                secureWrite.clearError();
                setCreateOpen(true);
              }}
            >
              <PlusIcon aria-hidden className="size-4" />
              {t.adminAssets.common.createCredential}
            </Button>
          ) : null
        }
        onReplace={(credential) => {
          secureWrite.clearError();
          setReplaceCredential(credential);
        }}
        onMigrate={(credential) => {
          secureWrite.clearError();
          setCredentialToMigrate(credential);
        }}
        onRevoke={(credential) => {
          secureWrite.clearError();
          setCredentialToRevoke(credential);
        }}
        onDelete={(credential) => {
          secureWrite.clearError();
          setCredentialDeleteSnapshot(
            createCredentialDeleteSnapshot(credential, Date.now()),
          );
        }}
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
        <ProjectCredentialReplaceDialog
          accountId={accountId}
          projectId={projectId}
          credential={replaceCredential}
          secureWrite={secureWrite}
          onClose={() => setReplaceCredential(null)}
        />
      )}
      {credentialToMigrate && (
        <CredentialGrantMigrationDialog
          open
          credentialName={credentialToMigrate.display_name}
          pending={secureWrite.pending}
          onOpenChange={(open) => !open && setCredentialToMigrate(null)}
          onConfirm={() => {
            void secureWrite
              .run(
                () =>
                  migrateProjectCredentialGrants(
                    project.id,
                    credentialToMigrate.id,
                    {
                      expected_credential_version: credentialToMigrate.version,
                    },
                  ),
                t.adminAssets.common.migrationSuccess,
              )
              .then((success) => success && setCredentialToMigrate(null));
          }}
        />
      )}
      {credentialToRevoke && (
        <CredentialRevokeDialog
          open
          credentialName={credentialToRevoke.display_name}
          pending={secureWrite.pending}
          onOpenChange={(open) => !open && setCredentialToRevoke(null)}
          onConfirm={() => {
            void secureWrite
              .run(() =>
                revokeProjectCredential(project.id, credentialToRevoke.id, {
                  expected_credential_version: credentialToRevoke.version,
                }),
              )
              .then((success) => success && setCredentialToRevoke(null));
          }}
        />
      )}
      {credentialDeleteSnapshot ? (
        <CredentialDeleteDialog
          key={`${credentialDeleteSnapshot.credentialId}:${credentialDeleteSnapshot.startedAt}`}
          snapshot={credentialDeleteSnapshot}
          pending={secureWrite.pending}
          errorMessage={secureWrite.errorMessage}
          onOpenChange={(open) => !open && setCredentialDeleteSnapshot(null)}
          onConfirm={() => {
            const snapshot = credentialDeleteSnapshot;
            void secureWrite
              .run(
                () =>
                  deleteProjectCredential(project.id, snapshot.credentialId, {
                    expected_credential_version:
                      snapshot.expectedCredentialVersion,
                  }),
                undefined,
                () => {
                  queryClient.setQueryData<ProjectCredentialList>(
                    projectAssetKey(accountId, projectId, "credentials"),
                    (current) =>
                      current
                        ? {
                            ...current,
                            system_items: current.system_items.filter(
                              (item) => item.id !== snapshot.credentialId,
                            ),
                            project_items: current.project_items.filter(
                              (item) => item.id !== snapshot.credentialId,
                            ),
                          }
                        : current,
                  );
                  queryClient.removeQueries({
                    queryKey: projectAssetVersionsKey(
                      accountId,
                      projectId,
                      "credentials",
                      snapshot.credentialId,
                    ),
                    exact: true,
                  });
                },
              )
              .then((success) => success && setCredentialDeleteSnapshot(null));
          }}
        />
      ) : null}
    </>
  );
}

function ProjectCredentialReplaceDialog({
  accountId,
  projectId,
  credential,
  secureWrite,
  onClose,
}: {
  accountId: string;
  projectId: string;
  credential: ProjectCredentialItem;
  secureWrite: ReturnType<typeof useSecureProjectCredentialWrite>;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const history = useProjectAssetVersions(
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
  const historyMessage = history.error
    ? adminAssetErrorMessage(history.error, t.adminAssets.errors)
    : history.data && !initialFields
      ? t.adminAssets.common.historySchemaUnavailable
      : null;
  const disabled =
    history.isLoading || Boolean(history.error) || initialFields === null;

  return (
    <CredentialSecretDialog
      mode="replace"
      open
      expectedVersion={credential.version}
      initialFields={initialFields ?? []}
      disabled={disabled}
      pending={secureWrite.pending}
      errorMessage={historyMessage ?? secureWrite.errorMessage}
      onRetry={history.error ? () => void history.refetch() : undefined}
      onOpenChange={(open) => !open && onClose()}
      onReplace={(input: ReplaceCredentialInput) => {
        void secureWrite
          .run(() => replaceProjectCredential(projectId, credential.id, input))
          .then((success) => success && onClose());
      }}
    />
  );
}

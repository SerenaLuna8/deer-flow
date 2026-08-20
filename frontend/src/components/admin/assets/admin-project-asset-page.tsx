"use client";

import { useQueryClient } from "@tanstack/react-query";
import { PlusIcon, SearchIcon, XIcon } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import {
  CreateVersionDialog,
  CredentialGrantMigrationDialog,
  CredentialMigrationReferenceList,
  CredentialRevokeDialog,
  CredentialSecretDialog,
  type VersionAuthoringInput,
} from "@/components/admin/assets/admin-asset-dialogs";
import {
  adminAssetErrorMessage,
  adminCredentialTypeLabel,
  credentialMigrationActionVisible,
  credentialMigrationCompleteMessage,
  credentialPendingMigrationMessage,
  filterAdminProjectCatalogItems,
} from "@/components/admin/assets/admin-asset-view-model";
import {
  AdminPage,
  AdminPageHeader,
  AdminSection,
} from "@/components/admin/ui/admin-page";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { AssetVersionHistory } from "@/components/assets/asset-version-history";
import {
  CredentialDeleteDialog,
  createCredentialDeleteSnapshot,
  type CredentialDeleteSnapshot,
} from "@/components/projects/assets/credential-delete-dialog";
import { settleMcpApproval } from "@/components/projects/assets/mcp-approval-dialog";
import {
  projectAssetCanAuthor,
  projectAssetCanCreateVersion,
} from "@/components/projects/assets/project-asset-view-model";
import {
  ProjectAssetHistoryView,
  credentialPayloadFieldsFromVersions,
  projectCredentialCanDelete,
  projectCredentialShowsHistory,
} from "@/components/projects/assets/project-assets-page";
import { SystemAssetSection } from "@/components/projects/assets/system-asset-section";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import {
  adminProjectAssetKey,
  adminProjectAssetVersionsKey,
  createAdminProjectCredential,
  deleteAdminProjectCredential,
  migrateAdminProjectCredentialGrants,
  replaceAdminProjectCredential,
  revokeAdminProjectCredential,
  useAdminProjectAssets,
  useAdminProjectAssetVersions,
  useAdminProjectCredentialMigrationStatus,
  useApproveAdminProjectMcpVersion,
  useChangeAdminProjectAssetStatus,
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
import { cn } from "@/lib/utils";

import { AdminProjectSystemBindingDialog } from "./admin-project-system-binding-dialog";

type MutableKind = Exclude<AssetListKind, "credentials">;
type VersionedKind = Exclude<MutableKind, "agents">;
type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;
const ADMIN_PROJECT_ASSET_DETAIL_ID = "admin-project-asset-detail";
const ADMIN_PROJECT_CREDENTIAL_DETAIL_ID = "admin-project-credential-detail";
type DirectorySearchItem = {
  display_name: string;
  name?: string;
  slug?: string;
};

export function filterAdminProjectDirectoryItems<T extends DirectorySearchItem>(
  items: T[],
  query: string,
): T[] {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return items;
  return items.filter((item) =>
    [item.display_name, item.slug, item.name].some((value) =>
      value?.toLocaleLowerCase().includes(normalized),
    ),
  );
}

function useAdminProjectDetailFocus(selectedId: string | null) {
  const detailRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!selectedId) return;
    detailRef.current?.scrollIntoView({ block: "nearest" });
    detailRef.current?.focus({ preventScroll: true });
  }, [selectedId]);
  return detailRef;
}

function projectPageTitle(
  kind: AssetListKind,
  pages: Translations["adminAssets"]["pages"],
) {
  switch (kind) {
    case "agents":
      return pages.project.agentsTitle;
    case "skills":
      return pages.project.skillsTitle;
    case "mcp-servers":
      return pages.project.mcpTitle;
    case "credentials":
      return pages.project.credentialsTitle;
  }
}

function ErrorNotice({ error }: { error: unknown }) {
  const { t } = useI18n();
  if (!error) return null;
  return (
    <p role="alert" className="text-destructive text-sm">
      {adminAssetErrorMessage(error, t.adminAssets.errors)}
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
  const { t } = useI18n();
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
    <div
      data-testid="admin-project-selected-asset-history"
      className="[&>div]:mt-0 [&>div]:border-t-0 [&>div]:pt-0"
    >
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
      {history.error ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={history.isFetching}
          onClick={() => void history.refetch()}
        >
          {history.isFetching
            ? t.adminAssets.common.retrying
            : t.adminAssets.common.retry}
        </Button>
      ) : null}
    </div>
  );
}

function AdminProjectDirectorySkeleton() {
  const { t } = useI18n();
  return (
    <div
      role="status"
      aria-label={t.adminAssets.common.loading}
      className="space-y-3"
    >
      <div className="flex items-center justify-between gap-3">
        <Skeleton className="h-9 w-56 max-w-[55%]" />
        <Skeleton className="h-9 w-32" />
      </div>
      <div className="border-border/70 overflow-hidden rounded-lg border">
        <Skeleton className="h-10 w-full rounded-none" />
        {Array.from({ length: 5 }, (_, index) => (
          <Skeleton
            key={index}
            className="border-background h-16 w-full rounded-none border-t"
          />
        ))}
      </div>
    </div>
  );
}

function DirectorySearch({
  actions,
  query,
  onQueryChange,
}: {
  actions?: ReactNode;
  query: string;
  onQueryChange: (query: string) => void;
}) {
  const { t } = useI18n();
  return (
    <div className="flex min-w-0 flex-col gap-3 border-b p-3 sm:flex-row sm:items-center sm:justify-between">
      <label className="relative min-w-0 flex-1 sm:max-w-md">
        <SearchIcon
          aria-hidden
          className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
        />
        <Input
          value={query}
          onChange={(event) => onQueryChange(event.currentTarget.value)}
          aria-label={t.adminAssets.catalog.searchPlaceholder}
          placeholder={t.adminAssets.catalog.searchPlaceholder}
          className="pl-9"
        />
      </label>
      {actions ? <div className="shrink-0">{actions}</div> : null}
    </div>
  );
}

function DirectoryEmpty({
  emptyMessage,
  filtered,
}: {
  emptyMessage: string;
  filtered: boolean;
}) {
  const { t } = useI18n();
  return (
    <div className="bg-muted/10 px-5 py-10 text-center">
      <p className="text-muted-foreground text-sm">
        {filtered ? t.adminAssets.catalog.noResults : emptyMessage}
      </p>
    </div>
  );
}

function AssetDirectoryRows({
  kind,
  items,
  selectedProjectAssetId,
  onCreateVersion,
  onInspectProject,
}: {
  kind: MutableKind;
  items: ProjectAssetItem[];
  selectedProjectAssetId: string | null;
  onCreateVersion: (item: ProjectAssetItem) => void;
  onInspectProject: (
    item: ProjectAssetItem,
    trigger: HTMLButtonElement,
  ) => void;
}) {
  const { locale, t } = useI18n();
  if (items.length === 0) {
    return (
      <DirectoryEmpty
        filtered={false}
        emptyMessage={t.adminAssets.catalog.noProjectAssets}
      />
    );
  }

  return (
    <div className="min-w-0">
      <div className="bg-muted/25 text-muted-foreground hidden min-w-0 grid-cols-[minmax(13rem,1.7fr)_7rem_minmax(10rem,1fr)_8rem_auto] items-center gap-3 border-b px-4 py-2 text-xs font-medium xl:grid">
        <span>{t.adminAssets.catalog.identifier}</span>
        <span>{t.adminAssets.catalog.lifecycleStatus}</span>
        <span>{t.adminAssets.catalog.publicationStatus}</span>
        <span>
          {kind === "mcp-servers"
            ? t.adminAssets.common.updatedAt
            : t.adminAssets.common.assetVersion}
        </span>
        <span className="text-right">{t.adminAssets.catalog.actions}</span>
      </div>
      {items.map((item) => {
        const selected = selectedProjectAssetId === item.id;
        const canCreateVersion =
          kind === "skills" &&
          projectAssetCanCreateVersion(kind, projectAssetCanAuthor(item, kind));
        const publication = item.current_published_version_id
          ? t.adminAssets.catalog.publishedAvailable
          : t.adminAssets.catalog.unpublished;

        return (
          <div
            key={item.id}
            data-testid={`admin-project-asset-row-${item.id}`}
            data-selected={selected || undefined}
            className={cn(
              "border-border/70 grid min-w-0 gap-3 border-b px-4 py-3 last:border-b-0 xl:grid-cols-[minmax(13rem,1.7fr)_7rem_minmax(10rem,1fr)_8rem_auto] xl:items-center",
              selected && "bg-primary/5 ring-primary ring-1 ring-inset",
            )}
          >
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">
                {item.display_name}
              </p>
              <p className="text-muted-foreground mt-1 truncate font-mono text-xs">
                {item.slug}
              </p>
            </div>
            <div className="flex min-w-0 items-center gap-2">
              <span className="text-muted-foreground text-xs xl:hidden">
                {t.adminAssets.catalog.lifecycleStatus}
              </span>
              <AssetStatusBadge status={item.status} />
            </div>
            <div className="min-w-0 text-sm">
              <span className="text-muted-foreground mr-2 text-xs xl:hidden">
                {t.adminAssets.catalog.publicationStatus}
              </span>
              <span>{publication}</span>
            </div>
            <div className="min-w-0 text-sm tabular-nums">
              <span className="text-muted-foreground mr-2 text-xs xl:hidden">
                {kind === "mcp-servers"
                  ? t.adminAssets.common.updatedAt
                  : t.adminAssets.common.assetVersion}
              </span>
              {kind === "mcp-servers"
                ? new Date(item.updated_at).toLocaleString(locale)
                : item.version}
            </div>
            <div className="flex min-w-0 flex-wrap gap-2 xl:justify-end">
              {kind !== "agents" ? (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  aria-pressed={selected}
                  aria-controls={ADMIN_PROJECT_ASSET_DETAIL_ID}
                  aria-expanded={selected}
                  onClick={(event) =>
                    onInspectProject(item, event.currentTarget)
                  }
                >
                  {t.adminAssets.catalog.viewDetails}
                </Button>
              ) : null}
              {canCreateVersion ? (
                <Button
                  type="button"
                  size="sm"
                  onClick={() => onCreateVersion(item)}
                >
                  {t.adminAssets.catalog.createNewVersion}
                </Button>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function AdminProjectAssetDirectory({
  actions,
  data,
  kind,
  projectId,
  selectedProjectAssetId,
  onCreateVersion,
  onInspectProject,
}: {
  actions?: ReactNode;
  data: ProjectAssetList;
  kind: MutableKind;
  projectId: string;
  selectedProjectAssetId: string | null;
  onCreateVersion: (item: ProjectAssetItem) => void;
  onInspectProject: (
    item: ProjectAssetItem,
    trigger: HTMLButtonElement,
  ) => void;
}) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const items = filterAdminProjectCatalogItems(data.project_items, projectId);
  const filtered = filterAdminProjectDirectoryItems(items, query);

  return (
    <div
      data-testid="admin-project-asset-directory"
      data-density="dense-directory"
      className="border-border/70 bg-card min-w-0 gap-0 overflow-hidden rounded-lg border"
    >
      <DirectorySearch
        query={query}
        onQueryChange={setQuery}
        actions={actions}
      />
      {filtered.length === 0 ? (
        <DirectoryEmpty
          filtered={query.trim().length > 0}
          emptyMessage={t.adminAssets.catalog.noProjectAssets}
        />
      ) : (
        <AssetDirectoryRows
          kind={kind}
          items={filtered}
          selectedProjectAssetId={selectedProjectAssetId}
          onCreateVersion={onCreateVersion}
          onInspectProject={onInspectProject}
        />
      )}
    </div>
  );
}

export function adminProjectSystemSkillItems(
  data: ProjectAssetList,
  kind: MutableKind,
): ProjectAssetItem[] {
  return kind === "skills"
    ? data.system_items.filter(
        (item) => item.scope === "system" && item.project_id === null,
      )
    : [];
}

function AdminProjectCredentialDirectory({
  actions,
  data,
  pending,
  projectId,
  selectedCredentialId,
  onInspect,
}: {
  actions?: ReactNode;
  data: ProjectCredentialList;
  pending: boolean;
  projectId: string;
  selectedCredentialId: string | null;
  onInspect: (
    credential: ProjectCredentialItem,
    trigger: HTMLButtonElement,
  ) => void;
}) {
  const { locale, t } = useI18n();
  const [query, setQuery] = useState("");
  const items = filterAdminProjectCatalogItems(data.project_items, projectId);
  const filtered = filterAdminProjectDirectoryItems(items, query);

  return (
    <div
      data-testid="admin-project-credential-directory"
      data-density="dense-directory"
      className="border-border/70 bg-card min-w-0 gap-0 overflow-hidden rounded-lg border"
    >
      <DirectorySearch
        query={query}
        onQueryChange={setQuery}
        actions={actions}
      />
      {filtered.length === 0 ? (
        <DirectoryEmpty
          filtered={query.trim().length > 0}
          emptyMessage={t.adminAssets.catalog.emptyCredentials(
            t.adminAssets.catalog.projectCredentials,
          )}
        />
      ) : (
        <div className="min-w-0">
          <div className="bg-muted/25 text-muted-foreground hidden min-w-0 grid-cols-[minmax(14rem,1.7fr)_7rem_9rem_7rem_12rem_auto] items-center gap-3 border-b px-4 py-2 text-xs font-medium xl:grid">
            <span>{t.adminAssets.catalog.identifier}</span>
            <span>{t.adminAssets.catalog.lifecycleStatus}</span>
            <span>{t.adminAssets.common.type}</span>
            <span>{t.adminAssets.common.metadataVersion}</span>
            <span>{t.adminAssets.common.updatedAt}</span>
            <span className="text-right">{t.adminAssets.catalog.actions}</span>
          </div>
          {filtered.map((credential) => {
            const selected = selectedCredentialId === credential.id;
            return (
              <div
                key={credential.id}
                data-testid={`admin-project-credential-row-${credential.id}`}
                data-selected={selected || undefined}
                className={cn(
                  "border-border/70 grid min-w-0 gap-3 border-b px-4 py-3 last:border-b-0 xl:grid-cols-[minmax(14rem,1.7fr)_7rem_9rem_7rem_12rem_auto] xl:items-center",
                  selected && "bg-primary/5 ring-primary ring-1 ring-inset",
                )}
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">
                    {credential.display_name}
                  </p>
                  <p className="text-muted-foreground mt-1 truncate font-mono text-xs">
                    {credential.name}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-muted-foreground text-xs xl:hidden">
                    {t.adminAssets.catalog.lifecycleStatus}
                  </span>
                  <AssetStatusBadge status={credential.status} />
                </div>
                <div className="min-w-0 text-sm">
                  <span className="text-muted-foreground mr-2 text-xs xl:hidden">
                    {t.adminAssets.common.type}
                  </span>
                  {adminCredentialTypeLabel(
                    credential.credential_type,
                    t.adminAssets.common.credentialTypes,
                  )}
                </div>
                <div className="text-sm tabular-nums">
                  <span className="text-muted-foreground mr-2 text-xs xl:hidden">
                    {t.adminAssets.common.metadataVersion}
                  </span>
                  {credential.version}
                </div>
                <div className="min-w-0">
                  <span className="text-muted-foreground mr-2 text-xs xl:hidden">
                    {t.adminAssets.common.updatedAt}
                  </span>
                  <time className="text-muted-foreground text-xs">
                    {new Date(credential.updated_at).toLocaleString(locale)}
                  </time>
                </div>
                <div className="flex justify-start xl:justify-end">
                  {projectCredentialShowsHistory(credential) ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      aria-pressed={selected}
                      aria-controls={ADMIN_PROJECT_CREDENTIAL_DETAIL_ID}
                      aria-expanded={selected}
                      disabled={pending}
                      onClick={(event) =>
                        onInspect(credential, event.currentTarget)
                      }
                    >
                      {t.adminAssets.catalog.viewDetails}
                    </Button>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
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
  const { t } = useI18n();
  const query = useAdminProjectAssets(accountId, projectId, kind);
  const createVersion = useCreateAdminProjectAssetVersion(
    accountId,
    projectId,
    kind === "agents" ? null : kind,
  );
  const [versionAsset, setVersionAsset] = useState<ProjectAssetItem | null>(
    null,
  );
  const [selectedProjectAssetId, setSelectedProjectAssetId] = useState<
    string | null
  >(null);
  const [bindingAssetId, setBindingAssetId] = useState<string | null>(null);
  const selectedProjectAssetTriggerRef = useRef<HTMLButtonElement>(null);
  const selectedProjectAssetDetailRef = useAdminProjectDetailFocus(
    selectedProjectAssetId,
  );

  useEffect(() => {
    if (createVersion.isSuccess) setVersionAsset(null);
  }, [createVersion.isSuccess]);
  const bindingItem = useMemo(() => {
    const current = query.data as ProjectAssetList | undefined;
    if (!current || kind !== "skills" || !bindingAssetId) return null;
    return (
      current.system_items.find((item) => item.id === bindingAssetId) ?? null
    );
  }, [bindingAssetId, kind, query.data]);
  useEffect(() => {
    if (bindingAssetId && query.data && !bindingItem) {
      setBindingAssetId(null);
    }
  }, [bindingAssetId, bindingItem, query.data]);

  if (query.isLoading) return <AdminProjectDirectorySkeleton />;
  if (query.error || !query.data) {
    return (
      <AdminSection
        title={t.adminAssets.pages.projectLoadFailed}
        className="border-destructive/30 bg-destructive/5"
      >
        <ErrorNotice error={query.error} />
        <Button
          type="button"
          className="mt-4"
          variant="outline"
          onClick={() => void query.refetch()}
        >
          {t.adminAssets.common.retry}
        </Button>
      </AdminSection>
    );
  }

  const data = query.data as ProjectAssetList;
  const projectItems = filterAdminProjectCatalogItems(
    data.project_items,
    projectId,
  );
  const selectedProjectAsset =
    kind === "agents"
      ? null
      : (projectItems.find((item) => item.id === selectedProjectAssetId) ??
        null);
  return (
    <>
      {kind === "skills" ? (
        <SystemAssetSection
          items={adminProjectSystemSkillItems(data, kind)}
          onManageBinding={(item) => setBindingAssetId(item.id)}
        />
      ) : null}
      <AdminProjectAssetDirectory
        kind={kind}
        data={data}
        projectId={projectId}
        selectedProjectAssetId={selectedProjectAssetId}
        onCreateVersion={(item) => setVersionAsset(item)}
        onInspectProject={(item, trigger) => {
          selectedProjectAssetTriggerRef.current = trigger;
          setSelectedProjectAssetId((current) =>
            current === item.id ? null : item.id,
          );
        }}
      />
      {selectedProjectAsset && kind !== "agents" ? (
        <AdminSection
          id={ADMIN_PROJECT_ASSET_DETAIL_ID}
          ref={selectedProjectAssetDetailRef}
          tabIndex={-1}
          data-testid="admin-project-asset-detail"
          className="focus-visible:ring-ring scroll-mt-20 focus-visible:ring-2 focus-visible:outline-none"
          title={selectedProjectAsset.display_name}
          description={
            <span className="font-mono text-xs">
              {selectedProjectAsset.slug}
            </span>
          }
          actions={
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              aria-label={t.adminOperations.ui.close}
              title={t.adminOperations.ui.close}
              onClick={() => {
                const trigger = selectedProjectAssetTriggerRef.current;
                setSelectedProjectAssetId(null);
                window.requestAnimationFrame(() => trigger?.focus());
              }}
            >
              <XIcon aria-hidden className="size-4" />
            </Button>
          }
          contentClassName="min-w-0"
        >
          <AdminProjectAssetHistory
            accountId={accountId}
            projectId={projectId}
            kind={kind}
            item={selectedProjectAsset}
          />
        </AdminSection>
      ) : null}
      {versionAsset && kind === "skills" ? (
        <CreateVersionDialog
          kind={kind}
          asset={versionAsset}
          open
          pending={createVersion.isPending}
          errorMessage={
            createVersion.error
              ? adminAssetErrorMessage(
                  createVersion.error,
                  t.adminAssets.errors,
                )
              : null
          }
          onOpenChange={(open) => !open && setVersionAsset(null)}
          onSubmit={(input: VersionAuthoringInput) =>
            createVersion.mutate({ assetId: versionAsset.id, input })
          }
        />
      ) : null}
      {kind === "skills" && bindingItem ? (
        <AdminProjectSystemBindingDialog
          accountId={accountId}
          projectId={projectId}
          kind="skills"
          item={bindingItem}
          open
          onOpenChange={(nextOpen) => {
            if (!nextOpen) setBindingAssetId(null);
          }}
          onConflict={() => void query.refetch()}
        />
      ) : null}
    </>
  );
}

function useSecureAdminProjectCredentialWrite(
  accountId: string,
  projectId: string,
) {
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
        queryKey: adminProjectAssetKey(accountId, projectId, "credentials"),
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
  const { t } = useI18n();
  const history = useAdminProjectAssetVersions(
    accountId,
    projectId,
    "credentials",
    credential.id,
  );
  return (
    <div className="border-border/70 bg-muted/20 rounded-lg border p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold">
          {t.adminAssets.common.versionHistory}
        </h3>
        {!history.isLoading && !history.error ? (
          <span className="text-muted-foreground text-xs tabular-nums">
            {t.adminAssets.common.versionCount(history.data?.data.length ?? 0)}
          </span>
        ) : null}
      </div>
      {history.isLoading ? (
        <Skeleton className="h-16 w-full" />
      ) : history.error ? (
        <div className="space-y-3">
          <ErrorNotice error={history.error} />
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={history.isFetching}
            onClick={() => void history.refetch()}
          >
            {history.isFetching
              ? t.adminAssets.common.retrying
              : t.adminAssets.common.retry}
          </Button>
        </div>
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
  const { t } = useI18n();
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
    ? adminAssetErrorMessage(history.error, t.adminAssets.errors)
    : history.data && !initialFields
      ? t.adminAssets.common.historySchemaUnavailable
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
  const { locale, t } = useI18n();
  const queryClient = useQueryClient();
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
  const [deleteSnapshot, setDeleteSnapshot] =
    useState<CredentialDeleteSnapshot | null>(null);
  const [selectedCredentialId, setSelectedCredentialId] = useState<
    string | null
  >(null);
  const selectedCredentialTriggerRef = useRef<HTMLButtonElement>(null);
  const selectedCredentialDetailRef =
    useAdminProjectDetailFocus(selectedCredentialId);

  const credentialData = query.data as ProjectCredentialList | undefined;
  const projectCredentials = credentialData
    ? filterAdminProjectCatalogItems(credentialData.project_items, projectId)
    : [];
  const selectedCredential =
    projectCredentials.find(
      (credential) => credential.id === selectedCredentialId,
    ) ?? null;
  const selectedCredentialCanWrite = selectedCredential
    ? projectCredentialCanDelete(selectedCredential)
    : false;
  const migrationStatus = useAdminProjectCredentialMigrationStatus(
    accountId,
    projectId,
    selectedCredential?.id ?? "",
    selectedCredential?.current_version_id ?? "",
    Boolean(
      selectedCredential &&
      selectedCredentialCanWrite &&
      selectedCredential.status === "active" &&
      selectedCredential.version > 1,
    ),
  );
  const pendingMigration = migrationStatus.data?.data ?? null;

  if (query.isLoading) return <AdminProjectDirectorySkeleton />;
  if (query.error || !query.data) {
    return (
      <AdminSection
        title={t.adminAssets.pages.projectLoadFailed}
        className="border-destructive/30 bg-destructive/5"
      >
        <ErrorNotice error={query.error} />
        <Button
          type="button"
          className="mt-4"
          variant="outline"
          onClick={() => void query.refetch()}
        >
          {t.adminAssets.common.retry}
        </Button>
      </AdminSection>
    );
  }
  const data = query.data as ProjectCredentialList;

  return (
    <>
      {secureWrite.errorMessage ? (
        <p
          role="alert"
          className="border-destructive/30 bg-destructive/5 text-destructive mb-4 rounded-lg border px-4 py-3 text-sm"
        >
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
      <AdminProjectCredentialDirectory
        data={data}
        pending={secureWrite.pending}
        projectId={projectId}
        selectedCredentialId={selectedCredentialId}
        actions={
          <Button
            type="button"
            size="sm"
            onClick={() => {
              secureWrite.clearMessage();
              setCreateOpen(true);
            }}
          >
            <PlusIcon aria-hidden className="size-4" />
            {t.adminAssets.common.createProjectCredential}
          </Button>
        }
        onInspect={(credential, trigger) => {
          selectedCredentialTriggerRef.current = trigger;
          setSelectedCredentialId((current) =>
            current === credential.id ? null : credential.id,
          );
        }}
      />
      {selectedCredential ? (
        <AdminSection
          id={ADMIN_PROJECT_CREDENTIAL_DETAIL_ID}
          ref={selectedCredentialDetailRef}
          tabIndex={-1}
          data-testid="admin-project-credential-detail"
          className="focus-visible:ring-ring scroll-mt-20 focus-visible:ring-2 focus-visible:outline-none"
          title={selectedCredential.display_name}
          description={
            <span className="font-mono text-xs">{selectedCredential.name}</span>
          }
          actions={
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              aria-label={t.adminOperations.ui.close}
              title={t.adminOperations.ui.close}
              onClick={() => {
                const trigger = selectedCredentialTriggerRef.current;
                setSelectedCredentialId(null);
                window.requestAnimationFrame(() => trigger?.focus());
              }}
            >
              <XIcon aria-hidden className="size-4" />
            </Button>
          }
          contentClassName="min-w-0 space-y-5"
        >
          <dl className="grid min-w-0 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <dt className="text-muted-foreground text-xs">
                {t.adminAssets.catalog.lifecycleStatus}
              </dt>
              <dd className="mt-1">
                <AssetStatusBadge status={selectedCredential.status} />
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">
                {t.adminAssets.common.type}
              </dt>
              <dd className="mt-1 text-sm">
                {adminCredentialTypeLabel(
                  selectedCredential.credential_type,
                  t.adminAssets.common.credentialTypes,
                )}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">
                {t.adminAssets.common.metadataVersion}
              </dt>
              <dd className="mt-1 text-sm tabular-nums">
                {selectedCredential.version}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">
                {t.adminAssets.common.updatedAt}
              </dt>
              <dd className="mt-1 text-sm">
                {new Date(selectedCredential.updated_at).toLocaleString(locale)}
              </dd>
            </div>
          </dl>
          {selectedCredentialCanWrite && selectedCredential.version > 1 ? (
            <section className="space-y-3 rounded-xl border p-4">
              {migrationStatus.isLoading ? (
                <p className="text-muted-foreground text-sm">
                  {t.adminAssets.common.credentialMigrationChecking}
                </p>
              ) : migrationStatus.error || !migrationStatus.data ? (
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <p role="alert" className="text-destructive text-sm">
                    {t.adminAssets.common.credentialMigrationUnavailable}
                  </p>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => void migrationStatus.refetch()}
                  >
                    {t.adminAssets.common.retry}
                  </Button>
                </div>
              ) : pendingMigration ? (
                <>
                  <p className="text-muted-foreground text-sm">
                    {credentialPendingMigrationMessage(
                      pendingMigration,
                      t.adminAssets.common,
                    ) ??
                      credentialMigrationCompleteMessage(
                        pendingMigration,
                        t.adminAssets.common,
                      )}
                  </p>
                  <CredentialMigrationReferenceList
                    pendingMigration={pendingMigration}
                  />
                </>
              ) : (
                <p className="text-muted-foreground text-sm">
                  {t.adminAssets.common.credentialMigrationUnavailable}
                </p>
              )}
            </section>
          ) : null}
          {selectedCredentialCanWrite ? (
            <div className="border-border/70 flex flex-col gap-3 border-t pt-4 lg:flex-row lg:items-start lg:justify-between">
              <div className="flex flex-wrap gap-2">
                {selectedCredential.status === "active" ? (
                  <>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={secureWrite.pending}
                      onClick={() => {
                        secureWrite.clearMessage();
                        setReplaceCredential(selectedCredential);
                      }}
                    >
                      {t.adminAssets.common.replaceCredential}
                    </Button>
                    {credentialMigrationActionVisible(pendingMigration) ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={secureWrite.pending}
                        onClick={() => {
                          secureWrite.clearMessage();
                          setMigrateCredential(selectedCredential);
                        }}
                      >
                        {t.adminAssets.common.migrateReferences}
                      </Button>
                    ) : null}
                  </>
                ) : null}
              </div>
              <div className="border-destructive/20 bg-destructive/5 min-w-0 rounded-lg border p-3">
                <p className="text-destructive mb-2 text-xs font-semibold">
                  {t.adminAssets.common.dangerZone}
                </p>
                <div className="flex flex-wrap gap-2">
                  {selectedCredential.status === "active" ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={secureWrite.pending}
                      onClick={() => {
                        secureWrite.clearMessage();
                        setRevokeCredential(selectedCredential);
                      }}
                    >
                      {t.adminAssets.common.revokeCredential}
                    </Button>
                  ) : null}
                  <Button
                    type="button"
                    size="sm"
                    variant="destructive"
                    disabled={secureWrite.pending}
                    onClick={() => {
                      secureWrite.clearMessage();
                      setDeleteSnapshot(
                        createCredentialDeleteSnapshot(
                          selectedCredential,
                          Date.now(),
                        ),
                      );
                    }}
                  >
                    {t.adminAssets.common.delete}
                  </Button>
                </div>
              </div>
            </div>
          ) : null}
          {projectCredentialShowsHistory(selectedCredential) ? (
            <AdminProjectCredentialHistory
              accountId={accountId}
              projectId={projectId}
              credential={selectedCredential}
            />
          ) : null}
        </AdminSection>
      ) : null}
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
      {migrateCredential && pendingMigration ? (
        <CredentialGrantMigrationDialog
          open
          credentialName={migrateCredential.display_name}
          pendingMigration={pendingMigration}
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
                t.adminAssets.common.migrationSuccess,
              )
              .then(async (success) => {
                if (!success) return;
                await migrationStatus.refetch();
                setMigrateCredential(null);
              });
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
      {deleteSnapshot ? (
        <CredentialDeleteDialog
          key={`${deleteSnapshot.credentialId}:${deleteSnapshot.startedAt}`}
          snapshot={deleteSnapshot}
          pending={secureWrite.pending}
          errorMessage={secureWrite.errorMessage}
          onOpenChange={(open) => !open && setDeleteSnapshot(null)}
          onConfirm={() => {
            const snapshot = deleteSnapshot;
            void secureWrite
              .run(
                () =>
                  deleteAdminProjectCredential(
                    projectId,
                    snapshot.credentialId,
                    {
                      expected_credential_version:
                        snapshot.expectedCredentialVersion,
                    },
                  ),
                undefined,
                () => {
                  queryClient.setQueryData<ProjectCredentialList>(
                    adminProjectAssetKey(accountId, projectId, "credentials"),
                    (current) =>
                      current
                        ? {
                            ...current,
                            project_items: current.project_items.filter(
                              (item) => item.id !== snapshot.credentialId,
                            ),
                          }
                        : current,
                  );
                  queryClient.removeQueries({
                    queryKey: adminProjectAssetVersionsKey(
                      accountId,
                      projectId,
                      "credentials",
                      snapshot.credentialId,
                    ),
                    exact: true,
                  });
                  setSelectedCredentialId((current) =>
                    current === snapshot.credentialId ? null : current,
                  );
                },
              )
              .then((success) => success && setDeleteSnapshot(null));
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
  const { t } = useI18n();
  if (user?.system_role !== "system_admin") return null;
  const title = projectPageTitle(kind, t.adminAssets.pages);
  return (
    <AdminPage data-testid="admin-project-asset-page">
      <AdminPageHeader title={title} />
      {kind === "credentials" ? (
        <AdminProjectCredentials accountId={user.id} projectId={projectId} />
      ) : (
        <MutableAdminProjectAssets
          accountId={user.id}
          projectId={projectId}
          kind={kind}
        />
      )}
    </AdminPage>
  );
}

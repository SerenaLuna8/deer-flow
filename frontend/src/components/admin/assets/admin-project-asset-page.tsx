"use client";

import { SearchIcon } from "lucide-react";
import { useMemo, useState } from "react";

import {
  CreateVersionDialog,
  type VersionAuthoringInput,
} from "@/components/admin/assets/admin-asset-dialogs";
import {
  AdminPage,
  AdminPageHeader,
  AdminSection,
} from "@/components/admin/ui/admin-page";
import {
  ProjectAssetCatalogView,
  ProjectAssetHistoryView,
} from "@/components/projects/assets/project-assets-page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import {
  useAdminProjectAssets,
  useAdminProjectAssetVersions,
  useChangeAdminProjectAssetStatus,
  useCreateAdminProjectAssetVersion,
  usePublishAdminProjectMcpVersion,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

import { AdminProjectSystemBindingDialog } from "./admin-project-system-binding-dialog";

type VersionedKind = Exclude<AssetListKind, "agents">;

export function adminProjectSystemSkillItems(
  data: ProjectAssetList,
  kind: AssetListKind,
): ProjectAssetItem[] {
  return kind === "skills" ? data.system_items : [];
}

export function filterAdminProjectDirectoryItems<
  T extends { display_name: string; slug?: string },
>(items: T[], query: string): T[] {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return items;
  return items.filter(
    (item) =>
      item.display_name.toLocaleLowerCase().includes(normalized) ||
      item.slug?.toLocaleLowerCase().includes(normalized),
  );
}

function AssetHistory({
  accountId,
  projectId,
  kind,
  item,
}: {
  accountId: string;
  projectId: string;
  kind: AssetListKind;
  item: ProjectAssetItem;
}) {
  const history = useAdminProjectAssetVersions(
    accountId,
    projectId,
    kind,
    item.id,
  );
  const publish = usePublishAdminProjectMcpVersion(accountId, projectId);
  const changeStatus = useChangeAdminProjectAssetStatus(
    accountId,
    projectId,
    kind,
  );
  return (
    <ProjectAssetHistoryView
      kind={kind}
      item={item}
      versions={history.data?.data ?? []}
      isLoading={history.isLoading}
      error={history.error}
      actionError={publish.error ?? changeStatus.error}
      pending={publish.isPending || changeStatus.isPending}
      onChangeStatus={(action) =>
        changeStatus.mutate({
          assetId: item.id,
          action,
          input:
            kind === "skills"
              ? { expected_revision: item.revision }
              : { expected_asset_version: item.revision },
        })
      }
      onPublish={
        kind === "mcp-servers"
          ? (version: AssetVersion) =>
              publish.mutate({
                assetId: item.id,
                versionId: version.id,
                input: { expected_asset_version: item.revision },
              })
          : undefined
      }
    />
  );
}

function ProjectAssets({
  accountId,
  projectId,
  kind,
}: {
  accountId: string;
  projectId: string;
  kind: AssetListKind;
}) {
  const query = useAdminProjectAssets(accountId, projectId, kind);
  const { t } = useI18n();
  const createVersion = useCreateAdminProjectAssetVersion(
    accountId,
    projectId,
    kind === "agents" ? "skills" : kind,
  );
  const [search, setSearch] = useState("");
  const [bindingItem, setBindingItem] = useState<ProjectAssetItem | null>(null);
  const [versionItem, setVersionItem] = useState<ProjectAssetItem | null>(null);
  const data = useMemo<ProjectAssetList | null>(() => {
    if (!query.data) return null;
    return {
      ...query.data,
      system_items: filterAdminProjectDirectoryItems(
        query.data.system_items,
        search,
      ),
      project_items: filterAdminProjectDirectoryItems(
        query.data.project_items,
        search,
      ),
    };
  }, [query.data, search]);

  return (
    <>
      <AdminSection title={t.adminAssets.catalog.projectAssets}>
        <label className="relative mb-5 block max-w-xl">
          <SearchIcon
            aria-hidden
            className="text-muted-foreground absolute top-2.5 left-3 size-4"
          />
          <Input
            className="pl-9"
            value={search}
            placeholder={t.adminAssets.catalog.searchPlaceholder}
            aria-label={t.adminAssets.catalog.searchPlaceholder}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        {query.isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-32" />
            <Skeleton className="h-32" />
          </div>
        ) : query.error ? (
          <div className="space-y-3">
            <p role="alert" className="text-destructive text-sm">
              {query.error instanceof Error
                ? query.error.message
                : t.adminAssets.catalog.projectCatalogUnavailable}
            </p>
            <Button
              type="button"
              variant="outline"
              onClick={() => void query.refetch()}
            >
              {t.adminAssets.common.retry}
            </Button>
          </div>
        ) : data ? (
          <ProjectAssetCatalogView
            kind={kind}
            data={data}
            onManageBinding={setBindingItem}
            onCreateVersion={kind === "agents" ? undefined : setVersionItem}
            renderProjectDetails={(item) => (
              <AssetHistory
                accountId={accountId}
                projectId={projectId}
                kind={kind}
                item={item}
              />
            )}
          />
        ) : null}
      </AdminSection>

      {bindingItem ? (
        <AdminProjectSystemBindingDialog
          accountId={accountId}
          projectId={projectId}
          kind={kind}
          item={bindingItem}
          open
          onConflict={() => void query.refetch()}
          onOpenChange={(open) => !open && setBindingItem(null)}
        />
      ) : null}

      {versionItem && kind !== "agents" ? (
        <CreateVersionDialog
          kind={kind as VersionedKind}
          asset={versionItem}
          open
          pending={createVersion.isPending}
          errorMessage={
            createVersion.error instanceof Error
              ? createVersion.error.message
              : null
          }
          onOpenChange={(open) => !open && setVersionItem(null)}
          onSubmit={(input: VersionAuthoringInput) =>
            createVersion.mutate(
              { assetId: versionItem.id, input },
              { onSuccess: () => setVersionItem(null) },
            )
          }
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
  const title =
    kind === "agents"
      ? t.adminAssets.catalog.projectAgentTitle
      : kind === "skills"
        ? t.adminAssets.catalog.projectSkillTitle
        : t.adminAssets.catalog.projectMcpTitle;
  return (
    <AdminPage data-testid="admin-project-asset-page">
      <AdminPageHeader title={title} />
      <ProjectAssets accountId={user.id} projectId={projectId} kind={kind} />
    </AdminPage>
  );
}

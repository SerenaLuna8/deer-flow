"use client";

import { AssetVersionHistory } from "@/components/assets/asset-version-history";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import type {
  AdminProjectAssetStatusAction,
  AssetListKind,
  AssetVersion,
  ProjectAssetItem,
  ProjectAssetList,
} from "@/core/shared-assets";

import { ProjectAssetSection } from "./project-asset-section";
import {
  adminProjectAssetDetailLifecycleActions,
  projectAssetCanAuthor,
} from "./project-asset-view-model";
import { SystemAssetSection } from "./system-asset-section";

type MutableKind = AssetListKind;

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
        kind={kind}
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

export { projectAssetCanAuthor };

function ErrorNotice({ error }: { error: unknown }) {
  const { t } = useI18n();
  if (!error) return null;
  return (
    <p role="alert" className="text-destructive text-sm">
      {error instanceof Error ? error.message : t.adminAssets.errors.fallback}
    </p>
  );
}

export function ProjectAssetHistoryView<Kind extends MutableKind>({
  kind,
  item,
  versions,
  isLoading = false,
  error,
  actionError,
  pending = false,
  onChangeStatus,
  onActivate,
  onPublish,
}: {
  kind: Kind;
  item: ProjectAssetItem;
  versions: AssetVersion[];
  isLoading?: boolean;
  error?: unknown;
  actionError?: unknown;
  pending?: boolean;
  onChangeStatus?: (action: AdminProjectAssetStatusAction<Kind>) => void;
  onActivate?: (version: AssetVersion) => void;
  onPublish?: (version: AssetVersion) => void;
}) {
  const { t } = useI18n();
  const canAuthor = projectAssetCanAuthor(item, kind);

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
                : action === "enable"
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
          currentVersionId={item.current_version_id}
          pending={pending}
          onActivate={onActivate}
          onPublish={canAuthor ? onPublish : undefined}
        />
      )}
      <ErrorNotice error={actionError} />
    </div>
  );
}

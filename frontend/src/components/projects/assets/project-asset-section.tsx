"use client";

import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useI18n } from "@/core/i18n/hooks";
import type { AssetListKind, ProjectAssetList } from "@/core/shared-assets";

import {
  projectAssetCanAuthor,
  projectAssetCanCreateVersion,
} from "./project-asset-view-model";

type ProjectAssetItem = ProjectAssetList["project_items"][number];

export function ProjectAssetSection({
  kind,
  items,
  onCreateVersion,
  renderDetails,
}: {
  kind: AssetListKind;
  items: ProjectAssetItem[];
  onCreateVersion?: (item: ProjectAssetItem) => void;
  renderDetails?: (item: ProjectAssetItem) => React.ReactNode;
}) {
  const { t } = useI18n();
  return (
    <section aria-labelledby="project-assets-heading" className="space-y-4">
      <div>
        <h2 id="project-assets-heading" className="text-xl font-semibold">
          {t.adminAssets.catalog.projectAssets}
        </h2>
        <p className="text-muted-foreground mt-1 text-sm">
          {kind === "agents"
            ? t.adminAssets.catalog.projectAgentDescription
            : t.adminAssets.catalog.projectVersionedDescription}
        </p>
      </div>
      {items.length === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed p-6 text-sm">
          {t.adminAssets.catalog.noProjectAssets}
        </p>
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          {items.map((item) => (
            <Card key={item.id} data-testid={`project-asset-${item.id}`}>
              <CardHeader>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <CardTitle className="truncate">
                      {item.display_name}
                    </CardTitle>
                    <p className="text-muted-foreground mt-1 font-mono text-xs">
                      {item.slug}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge>{t.adminAssets.catalog.project}</Badge>
                    <AssetStatusBadge status={item.status} />
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4 text-sm">
                <dl className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <dt className="text-muted-foreground text-xs">
                      {t.adminAssets.common.assetVersion}
                    </dt>
                    <dd>{item.revision}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground text-xs">
                      {kind === "agents"
                        ? t.adminAssets.catalog.definitionStatus
                        : t.adminAssets.catalog.currentVersionStatus}
                    </dt>
                    <dd>
                      {kind === "agents"
                        ? item.definition_id
                          ? t.adminAssets.catalog.definitionAvailable
                          : t.adminAssets.catalog.definitionMissing
                        : item.current_version_id
                          ? t.adminAssets.catalog.currentVersionAvailable
                          : t.adminAssets.catalog.currentVersionMissing}
                    </dd>
                  </div>
                </dl>
                {projectAssetCanCreateVersion(
                  kind,
                  projectAssetCanAuthor(item, kind),
                ) && (
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => onCreateVersion?.(item)}
                  >
                    {t.adminAssets.catalog.createNewVersion}
                  </Button>
                )}
                {renderDetails?.(item)}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

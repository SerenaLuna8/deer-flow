"use client";

import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useI18n } from "@/core/i18n/hooks";
import type { AssetListKind, ProjectAssetList } from "@/core/shared-assets";

type SystemAssetItem = ProjectAssetList["system_items"][number];

export function canManageSystemBinding(item: SystemAssetItem): boolean {
  return (
    item.capabilities.includes("shared_assets.manage_bindings") &&
    (item.status === "active" || Boolean(item.binding?.enabled))
  );
}

export function SystemAssetSection({
  kind,
  items,
  onManageBinding,
  renderDetails,
}: {
  kind: AssetListKind;
  items: SystemAssetItem[];
  onManageBinding?: (item: SystemAssetItem) => void;
  renderDetails?: (item: SystemAssetItem) => React.ReactNode;
}) {
  const { t } = useI18n();
  return (
    <section aria-labelledby="system-assets-heading" className="space-y-4">
      <div>
        <h2 id="system-assets-heading" className="text-xl font-semibold">
          {t.adminAssets.catalog.systemAssets}
        </h2>
        <p className="text-muted-foreground mt-1 text-sm">
          {kind === "mcp-servers"
            ? t.adminAssets.catalog.systemMcpDescription
            : t.adminAssets.catalog.systemCurrentAssetsDescription}
        </p>
      </div>
      {items.length === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed p-6 text-sm">
          {t.adminAssets.catalog.noSystemAssets}
        </p>
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          {items.map((item) => {
            const canManage = canManageSystemBinding(item);
            return (
              <Card key={item.id} data-testid={`system-asset-${item.id}`}>
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
                      <Badge variant="secondary">
                        {t.adminAssets.catalog.system}
                      </Badge>
                      <AssetStatusBadge status={item.status} />
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="space-y-4 text-sm">
                  <dl className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <dt className="text-muted-foreground text-xs">
                        {kind === "mcp-servers"
                          ? t.adminAssets.catalog.systemPublishStatus
                          : t.adminAssets.catalog.currentVersionStatus}
                      </dt>
                      <dd>
                        {item.current_version_id
                          ? kind === "mcp-servers"
                            ? t.adminAssets.catalog.publishedAvailable
                            : t.adminAssets.catalog.currentVersionAvailable
                          : kind === "mcp-servers"
                            ? t.adminAssets.catalog.unpublished
                            : t.adminAssets.catalog.currentVersionMissing}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground text-xs">
                        {kind === "mcp-servers"
                          ? t.adminAssets.catalog.pinnedVersion
                          : t.adminAssets.catalog.bindingStatus}
                      </dt>
                      <dd>
                        {item.binding?.enabled
                          ? kind === "mcp-servers"
                            ? t.adminAssets.catalog.enabledAndPinned
                            : t.adminAssets.catalog.enabled
                          : item.binding
                            ? t.adminAssets.catalog.closed
                            : t.adminAssets.catalog.notBound}
                      </dd>
                    </div>
                    {kind === "mcp-servers" ? (
                      <>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            {t.adminAssets.catalog.bindingStatus}
                          </dt>
                          <dd>
                            {item.binding
                              ? item.binding.enabled
                                ? t.adminAssets.catalog.enabled
                                : t.adminAssets.catalog.closed
                              : t.adminAssets.catalog.notBound}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            {t.adminAssets.catalog.bindingRevision}
                          </dt>
                          <dd>
                            {item.binding?.version ??
                              t.adminAssets.catalog.none}
                          </dd>
                        </div>
                      </>
                    ) : null}
                  </dl>
                  {canManage && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => onManageBinding?.(item)}
                    >
                      {t.adminAssets.catalog.manageBinding}
                    </Button>
                  )}
                  {renderDetails?.(item)}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </section>
  );
}

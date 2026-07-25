"use client";

import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { AssetListKind, ProjectAssetList } from "@/core/shared-assets";

import { projectAssetCanAuthor } from "./project-asset-view-model";

type ProjectAssetItem = ProjectAssetList["project_items"][number];

export function ProjectAssetSection({
  kind,
  items,
  onCreateVersion,
  renderDetails,
}: {
  kind: Exclude<AssetListKind, "credentials">;
  items: ProjectAssetItem[];
  onCreateVersion?: (item: ProjectAssetItem) => void;
  renderDetails?: (item: ProjectAssetItem) => React.ReactNode;
}) {
  return (
    <section aria-labelledby="project-assets-heading" className="space-y-4">
      <div>
        <h2 id="project-assets-heading" className="text-xl font-semibold">
          项目资产
        </h2>
        <p className="text-muted-foreground mt-1 text-sm">
          只属于当前项目；内容变更通过不可变新版本完成。
        </p>
      </div>
      {items.length === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed p-6 text-sm">
          当前项目还没有此类资产。
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
                    <Badge>项目</Badge>
                    <AssetStatusBadge status={item.status} />
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4 text-sm">
                <dl className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <dt className="text-muted-foreground text-xs">资产版本</dt>
                    <dd>{item.version}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground text-xs">发布状态</dt>
                    <dd>
                      {item.current_published_version_id
                        ? "已有发布版本"
                        : "尚未发布"}
                    </dd>
                  </div>
                </dl>
                {projectAssetCanAuthor(item, kind) && (
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => onCreateVersion?.(item)}
                  >
                    创建新版本
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

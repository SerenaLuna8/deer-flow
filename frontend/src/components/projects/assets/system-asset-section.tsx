"use client";

import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ProjectAssetList } from "@/core/shared-assets";

type SystemAssetItem = ProjectAssetList["system_items"][number];

export function canManageSystemBinding(item: SystemAssetItem): boolean {
  return (
    item.capabilities.includes("shared_assets.manage_bindings") &&
    (item.status === "active" || Boolean(item.binding?.enabled))
  );
}

export function SystemAssetSection({
  items,
  onManageBinding,
  renderDetails,
}: {
  items: SystemAssetItem[];
  onManageBinding?: (item: SystemAssetItem) => void;
  renderDetails?: (item: SystemAssetItem) => React.ReactNode;
}) {
  return (
    <section aria-labelledby="system-assets-heading" className="space-y-4">
      <div>
        <h2 id="system-assets-heading" className="text-xl font-semibold">
          系统资产
        </h2>
        <p className="text-muted-foreground mt-1 text-sm">
          系统资产只读共享；项目绑定固定到明确版本，不会自动升级。
        </p>
      </div>
      {items.length === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed p-6 text-sm">
          暂无可见系统资产。
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
                      <Badge variant="secondary">系统</Badge>
                      <AssetStatusBadge status={item.status} />
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="space-y-4 text-sm">
                  <dl className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <dt className="text-muted-foreground text-xs">
                        系统发布状态
                      </dt>
                      <dd>
                        {item.current_published_version_id
                          ? "已有发布版本"
                          : "尚未发布"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground text-xs">
                        固定版本
                      </dt>
                      <dd>
                        {item.binding?.enabled
                          ? "已启用并固定"
                          : item.binding
                            ? "已关闭"
                            : "未绑定"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground text-xs">
                        绑定状态
                      </dt>
                      <dd>
                        {item.binding
                          ? item.binding.enabled
                            ? "已启用"
                            : "已关闭"
                          : "未绑定"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground text-xs">
                        绑定修订版本
                      </dt>
                      <dd>{item.binding?.version ?? "无"}</dd>
                    </div>
                  </dl>
                  {canManage && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => onManageBinding?.(item)}
                    >
                      管理绑定
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

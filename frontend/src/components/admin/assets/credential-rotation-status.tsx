import { CheckCircle2Icon, RotateCwIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import type { CredentialRotationStatus } from "@/core/shared-assets";

export function CredentialRotationStatusCard({
  status,
}: {
  status: CredentialRotationStatus;
}) {
  const isCurrent = status.status === "current";
  return (
    <Card data-testid="credential-rotation-status">
      <CardContent className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <div className="bg-primary/10 text-primary flex size-9 shrink-0 items-center justify-center rounded-lg">
            {isCurrent ? (
              <CheckCircle2Icon aria-hidden className="size-5" />
            ) : (
              <RotateCwIcon aria-hidden className="size-5" />
            )}
          </div>
          <div>
            <h2 className="font-semibold">Credential envelope 轮换状态</h2>
            <p className="text-muted-foreground mt-1 text-sm">
              当前 {status.current} / 共 {status.eligible_total} 个有效版本
            </p>
          </div>
        </div>
        <Badge variant={isCurrent ? "default" : "secondary"}>
          {isCurrent ? "轮换正常" : `待轮换 ${status.pending} 项`}
        </Badge>
      </CardContent>
    </Card>
  );
}

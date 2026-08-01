"use client";

import { CheckCircle2Icon, RotateCwIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { useI18n } from "@/core/i18n/hooks";
import type { CredentialRotationStatus } from "@/core/shared-assets";

export function CredentialRotationStatusCard({
  status,
}: {
  status: CredentialRotationStatus;
}) {
  const isCurrent = status.status === "current";
  const { t } = useI18n();
  return (
    <Card
      data-testid="credential-rotation-status"
      data-density="compact"
      className="gap-0 py-0 shadow-none"
    >
      <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <div
            className={
              isCurrent
                ? "border-success/30 bg-success/10 text-success flex size-8 shrink-0 items-center justify-center rounded-lg border"
                : "border-border bg-muted text-muted-foreground flex size-8 shrink-0 items-center justify-center rounded-lg border"
            }
          >
            {isCurrent ? (
              <CheckCircle2Icon aria-hidden className="size-4" />
            ) : (
              <RotateCwIcon aria-hidden className="size-4" />
            )}
          </div>
          <div>
            <h2 className="text-sm font-semibold">
              {t.adminAssets.rotation.title}
            </h2>
            <p className="text-muted-foreground mt-1 text-xs">
              {t.adminAssets.rotation.summary(
                status.current,
                status.eligible_total,
              )}
            </p>
          </div>
        </div>
        <Badge
          variant="outline"
          className={
            isCurrent
              ? "border-success/25 bg-success/10 text-foreground gap-1.5"
              : "border-warning/25 bg-warning/10 text-foreground gap-1.5"
          }
        >
          <span
            aria-hidden
            className={
              isCurrent
                ? "bg-success size-1.5 rounded-full"
                : "bg-warning size-1.5 rounded-full"
            }
          />
          {isCurrent
            ? t.adminAssets.rotation.current
            : t.adminAssets.rotation.pending(status.pending)}
        </Badge>
      </CardContent>
    </Card>
  );
}

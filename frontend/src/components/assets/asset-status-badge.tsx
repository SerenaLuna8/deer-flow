import { Badge } from "@/components/ui/badge";
import { useI18n } from "@/core/i18n/hooks";
import type { AssetStatus } from "@/core/shared-assets";
import { cn } from "@/lib/utils";

type Status =
  | AssetStatus
  | "draft"
  | "pending_approval"
  | "published"
  | "rejected"
  | "retired"
  | "revoked"
  | "current"
  | "candidate"
  | "historical";

export function AssetStatusBadge({
  status,
  label,
}: {
  status: Status;
  label?: string;
}) {
  const { t } = useI18n();
  const healthy =
    status === "active" || status === "published" || status === "current";
  const warning =
    status === "suspended" ||
    status === "pending_approval" ||
    status === "candidate";
  const danger = status === "rejected" || status === "revoked";

  return (
    <Badge
      variant="outline"
      className={cn(
        "gap-1.5 px-2.5 py-1 font-medium",
        healthy && "border-success/25 bg-success/10 text-foreground",
        warning && "border-chart-4/30 bg-chart-4/10 text-foreground",
        danger && "border-destructive/25 bg-destructive/10 text-destructive",
        !healthy &&
          !warning &&
          !danger &&
          "border-border bg-muted text-muted-foreground",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 rounded-full",
          healthy && "bg-success",
          warning && "bg-chart-4",
          danger && "bg-destructive",
          !healthy && !warning && !danger && "bg-muted-foreground/55",
        )}
      />
      {label ??
        (status === "current"
          ? t.adminAssets.version.current
          : status === "candidate"
            ? t.adminAssets.version.candidate
            : status === "historical"
              ? t.adminAssets.version.historical
              : t.adminAssets.status[status])}
    </Badge>
  );
}

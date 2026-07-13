import { Badge } from "@/components/ui/badge";
import type { AssetStatus } from "@/core/shared-assets";

type Status =
  | AssetStatus
  | "draft"
  | "pending_approval"
  | "published"
  | "rejected"
  | "retired"
  | "revoked";

const LABELS: Record<Status, string> = {
  active: "启用",
  archived: "已归档",
  suspended: "已暂停",
  draft: "草稿",
  pending_approval: "待审批",
  published: "已发布",
  rejected: "已拒绝",
  retired: "已替换",
  revoked: "已撤销",
};

export function AssetStatusBadge({ status }: { status: Status }) {
  return (
    <Badge
      variant={
        status === "active" || status === "published" ? "default" : "secondary"
      }
    >
      {LABELS[status]}
    </Badge>
  );
}

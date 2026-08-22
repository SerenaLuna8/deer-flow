"use client";

import { DownloadIcon, LoaderCircleIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import type { SkillDistributionDownload } from "@/core/shared-assets";

export type SkillExportBlockReason =
  | "unsaved"
  | "loading"
  | "revoked"
  | "not-current"
  | "no-version";

export function skillExportBlockReason({
  hasVersion,
  unsaved,
  loading,
  revoked,
  notCurrent,
}: {
  hasVersion: boolean;
  unsaved?: boolean;
  loading?: boolean;
  revoked?: boolean;
  notCurrent?: boolean;
}): SkillExportBlockReason | null {
  if (unsaved) return "unsaved";
  if (loading) return "loading";
  if (revoked) return "revoked";
  if (notCurrent) return "not-current";
  return hasVersion ? null : "no-version";
}

export function startSkillDistributionDownload({
  content,
  filename,
}: SkillDistributionDownload): void {
  const url = URL.createObjectURL(content);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  anchor.hidden = true;
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(url);
  }
}

export function SkillExportButton({
  versionNumber,
  blockReason = null,
  download,
}: {
  versionNumber: number | null;
  blockReason?: SkillExportBlockReason | null;
  download: () => Promise<SkillDistributionDownload>;
}) {
  const { t } = useI18n();
  const [pending, setPending] = useState(false);
  const copy = t.skills.export;

  return (
    <Button
      type="button"
      variant="outline"
      disabled={pending || blockReason !== null || versionNumber === null}
      aria-label={copy.label}
      onClick={() => {
        setPending(true);
        void download()
          .then((result) => {
            startSkillDistributionDownload(result);
            toast.success(copy.started);
          })
          .catch((error: unknown) => {
            toast.error(adminAssetErrorMessage(error, t.adminAssets.errors));
          })
          .finally(() => setPending(false));
      }}
    >
      {pending ? (
        <LoaderCircleIcon aria-hidden className="animate-spin" />
      ) : (
        <DownloadIcon aria-hidden />
      )}
      {pending ? copy.preparing : copy.label}
    </Button>
  );
}

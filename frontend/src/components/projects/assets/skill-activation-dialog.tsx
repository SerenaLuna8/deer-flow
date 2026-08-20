"use client";

import {
  AlertCircleIcon,
  CheckCircle2Icon,
  KeyRoundIcon,
  Loader2Icon,
  RefreshCwIcon,
} from "lucide-react";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useI18n } from "@/core/i18n/hooks";
import {
  buildSkillActivationInput,
  useActivateProjectAssetVersion,
  useProjectSkillActivationReadiness,
  type ProjectAssetItem,
  type SkillActivationReadinessResponse,
} from "@/core/shared-assets";

import type { SkillAssetVersion } from "./skill-asset-detail";

function readinessIdentity(readiness: SkillActivationReadinessResponse): string {
  return `${readiness.skill_version_id}:${readiness.revision}:${readiness.payload_checksum}:${readiness.binding_revision}`;
}

function mappingStatusLabel(
  status: SkillActivationReadinessResponse["requirements"][number]["mapping_status"],
  copy: {
    statusConfigured: string;
    statusInvalid: string;
    statusMissing: string;
  },
): string {
  if (status === "configured") return copy.statusConfigured;
  if (status === "invalid") return copy.statusInvalid;
  return copy.statusMissing;
}

export function SkillActivationDialog({
  open,
  accountId,
  projectId,
  item,
  version,
  onOpenChange,
  onActivated,
  onConfigureCredentials,
}: {
  open: boolean;
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  version: SkillAssetVersion;
  onOpenChange: (open: boolean) => void;
  onActivated: (versionId: string) => void;
  onConfigureCredentials: () => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.activationDialog;
  const readiness = useProjectSkillActivationReadiness(
    accountId,
    projectId,
    item.id,
    version.id,
    open,
  );
  const activate = useActivateProjectAssetVersion(
    accountId,
    projectId,
    "skills",
  );
  const plan = readiness.data ?? null;
  const canConfigureCredentials = item.capabilities.includes(
    "mcp.credentials.approve",
  );

  async function activateVersion() {
    if (!plan?.ready || activate.isPending) return;
    const identity = readinessIdentity(plan);
    try {
      const response = await activate.mutateAsync({
        assetId: item.id,
        versionId: version.id,
        input: buildSkillActivationInput({ readiness: plan }),
      });
      if (identity !== readinessIdentity(plan) || !("skill_id" in response.data)) {
        return;
      }
      onActivated(response.data.id);
      onOpenChange(false);
    } catch {
      void readiness.refetch();
    }
  }

  const error = activate.error ?? readiness.error;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !activate.isPending) onOpenChange(false);
      }}
    >
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription>
            {copy.description(version.version_number)}
          </DialogDescription>
        </DialogHeader>

        {readiness.isLoading || (!plan && readiness.isFetching) ? (
          <div role="status" className="text-muted-foreground flex items-center gap-2 rounded-lg border border-dashed p-5 text-sm">
            <Loader2Icon aria-hidden className="size-4 animate-spin" />
            {copy.loading}
          </div>
        ) : null}

        {plan ? (
          <div className="space-y-4">
            <div
              className={`flex items-start gap-2 rounded-lg border p-4 text-sm ${
                plan.ready ? "border-success/30" : "border-destructive/30"
              }`}
            >
              {plan.ready ? (
                <CheckCircle2Icon aria-hidden className="text-success mt-0.5 size-4 shrink-0" />
              ) : (
                <AlertCircleIcon aria-hidden className="text-destructive mt-0.5 size-4 shrink-0" />
              )}
              <div>
                <p className="font-medium">
                  {plan.ready ? copy.preflightReady : copy.preflightBlocked}
                </p>
                <p className="text-muted-foreground mt-1 text-xs">
                  {copy.preflightSummary(
                    plan.configured_required_count,
                    plan.required_count,
                    plan.invalid_count,
                  )}
                </p>
              </div>
            </div>

            {plan.requirements.length > 0 ? (
              <section className="space-y-3" aria-label={copy.bindingsTitle}>
                <div className="flex items-center gap-2">
                  <KeyRoundIcon aria-hidden className="size-4" />
                  <h3 className="text-sm font-semibold">{copy.bindingsTitle}</h3>
                </div>
                <div className="space-y-2">
                  {plan.requirements.map((requirement) => (
                    <div key={requirement.name} className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2">
                      <code className="min-w-0 flex-1 text-sm break-all">
                        {requirement.name}
                      </code>
                      <Badge variant="secondary">
                        {requirement.optional ? copy.optional : copy.required}
                      </Badge>
                      <Badge variant={requirement.mapping_status === "configured" ? "default" : "secondary"}>
                        {mappingStatusLabel(requirement.mapping_status, copy)}
                      </Badge>
                    </div>
                  ))}
                </div>
              </section>
            ) : (
              <p className="text-muted-foreground rounded-lg border p-4 text-sm">
                {copy.noRequirements}
              </p>
            )}

            {!plan.ready ? (
              <p role="alert" className="text-destructive text-sm">
                {copy.approvalRequiredForActive}
              </p>
            ) : null}
          </div>
        ) : null}

        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {adminAssetErrorMessage(error)}
          </p>
        ) : null}

        <DialogFooter>
          <Button type="button" variant="outline" disabled={activate.isPending} onClick={() => onOpenChange(false)}>
            {copy.cancel}
          </Button>
          {readiness.error ? (
            <Button type="button" variant="outline" disabled={readiness.isFetching} onClick={() => void readiness.refetch()}>
              <RefreshCwIcon aria-hidden className="size-4" />
              {copy.retry}
            </Button>
          ) : null}
          {plan && !plan.ready && canConfigureCredentials ? (
            <Button type="button" onClick={onConfigureCredentials}>
              {copy.configureCredentials}
            </Button>
          ) : null}
          <Button type="button" disabled={!plan?.ready || readiness.isFetching || activate.isPending} onClick={() => void activateVersion()}>
            {activate.isPending ? <Loader2Icon aria-hidden className="size-4 animate-spin" /> : null}
            {activate.isPending ? copy.activating : copy.activate}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

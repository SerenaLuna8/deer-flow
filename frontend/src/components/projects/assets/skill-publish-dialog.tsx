"use client";

import {
  AlertCircleIcon,
  CheckCircle2Icon,
  KeyRoundIcon,
  Loader2Icon,
  RefreshCwIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

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
import type { Translations } from "@/core/i18n/locales/types";
import {
  SharedAssetApiError,
  buildSkillPublishInput,
  useProjectSkillPublishPlan,
  usePublishProjectAssetVersion,
  type ProjectAssetItem,
  type SkillPublishPlanResponse,
} from "@/core/shared-assets";

import type { SkillAssetVersion } from "./skill-asset-detail";

function publishErrorMessage(
  error: unknown,
  copy: Translations["skills"]["publishDialog"],
): string | null {
  if (!error) return null;
  if (error instanceof SharedAssetApiError) {
    if (error.code === "SKILL_CREDENTIAL_BINDINGS_INCOMPLETE") {
      return copy.incomplete;
    }
    if (error.code === "SKILL_CREDENTIAL_BINDING_INVALID") {
      return copy.invalidBinding;
    }
    if (error.code === "SKILL_CREDENTIAL_SELECTION_STALE") {
      return copy.staleSelection;
    }
    if (error.code === "SKILL_PUBLISH_BASE_STALE") {
      return copy.stalePublishBase;
    }
    if (error.code === "SKILL_SECRET_DECLARATION_INVALID") {
      return copy.invalidDeclaration;
    }
    if (error.status === 403) return copy.forbidden;
  }
  return adminAssetErrorMessage(error);
}

function planIdentity(plan: SkillPublishPlanResponse): string {
  return `${plan.skill_version_id}:${plan.asset_version}:${plan.payload_checksum}:${plan.binding_revision}:${plan.request_id}`;
}

function mappingStatusLabel(
  status: SkillPublishPlanResponse["requirements"][number]["mapping_status"],
  copy: Translations["skills"]["publishDialog"],
): string {
  if (status === "configured") return copy.statusConfigured;
  if (status === "invalid") return copy.statusInvalid;
  return copy.statusMissing;
}

export function SkillPublishDialog({
  open,
  accountId,
  projectId,
  item,
  version,
  onOpenChange,
  onPublished,
  onConfigureCredentials,
}: {
  open: boolean;
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  version: SkillAssetVersion;
  onOpenChange: (open: boolean) => void;
  onPublished: (versionId: string) => void;
  onConfigureCredentials: () => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.publishDialog;
  const planQuery = useProjectSkillPublishPlan(
    accountId,
    projectId,
    item.id,
    version.id,
    open,
  );
  const publish = usePublishProjectAssetVersion(accountId, projectId, "skills");
  const [staleBaseRequired, setStaleBaseRequired] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const appliedPlanIdentity = useRef<string | null>(null);
  const publishResetRef = useRef(publish.reset);
  publishResetRef.current = publish.reset;
  const plan = planQuery.data ?? null;

  useEffect(() => {
    if (!open || !plan) return;
    const identity = planIdentity(plan);
    if (appliedPlanIdentity.current === identity) return;
    appliedPlanIdentity.current = identity;
    setNotice(null);
  }, [open, plan]);

  useEffect(() => {
    if (open) return;
    appliedPlanIdentity.current = null;
    setStaleBaseRequired(false);
    setNotice(null);
    publishResetRef.current();
  }, [open]);

  async function refreshPlan(message?: string) {
    const result = await planQuery.refetch();
    if (result.data) appliedPlanIdentity.current = planIdentity(result.data);
    if (message) setNotice(message);
    return result.data ?? null;
  }

  function submitPublish(acknowledgeStaleBase = false) {
    if (!plan || !plan.ready || publish.isPending) return;
    setNotice(null);
    publish.mutate(
      {
        assetId: item.id,
        versionId: version.id,
        input: buildSkillPublishInput({
          plan,
          acknowledgeStaleBase,
        }),
      },
      {
        onSuccess: (response) => {
          if (!("skill_id" in response.data)) return;
          onPublished(response.data.id);
          onOpenChange(false);
        },
        onError: (error) => {
          if (!(error instanceof SharedAssetApiError)) return;
          if (error.code === "SKILL_CREDENTIAL_SELECTION_STALE") {
            void refreshPlan(copy.credentialChanged);
          } else if (error.code === "SKILL_PUBLISH_BASE_STALE") {
            setStaleBaseRequired(true);
            void refreshPlan();
          } else if (error.code === "ASSET_CONFLICT") {
            void refreshPlan(copy.assetChanged);
          }
        },
      },
    );
  }

  const dialogError = publish.error
    ? publishErrorMessage(publish.error, copy)
    : planQuery.error
      ? publishErrorMessage(planQuery.error, copy)
      : null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (next || publish.isPending) return;
        onOpenChange(false);
      }}
    >
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription>
            {copy.description(version.version_number)}
          </DialogDescription>
        </DialogHeader>

        {planQuery.isLoading || (!plan && planQuery.isFetching) ? (
          <div
            role="status"
            aria-live="polite"
            className="text-muted-foreground flex items-center gap-2 rounded-lg border border-dashed p-5 text-sm"
          >
            <Loader2Icon aria-hidden className="size-4 animate-spin" />
            {copy.loading}
          </div>
        ) : null}

        {plan ? (
          <div className="space-y-4">
            <div className="bg-muted/25 grid gap-2 rounded-lg border p-3 text-xs sm:grid-cols-2">
              <p>
                <span className="text-muted-foreground">
                  {copy.targetVersion}
                </span>
                {version.version_number}
              </p>
              <p>
                <span className="text-muted-foreground">
                  {copy.bindingRevision}
                </span>
                {plan.binding_revision}
              </p>
            </div>

            <div
              className={`flex items-start gap-2 rounded-lg border p-4 text-sm ${
                plan.ready ? "border-success/30" : "border-destructive/30"
              }`}
            >
              {plan.ready ? (
                <CheckCircle2Icon
                  aria-hidden
                  className="text-success mt-0.5 size-4 shrink-0"
                />
              ) : (
                <AlertCircleIcon
                  aria-hidden
                  className="text-destructive mt-0.5 size-4 shrink-0"
                />
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

            {plan.requirements.length === 0 ? (
              <p className="text-muted-foreground rounded-lg border p-4 text-sm">
                {copy.noRequirements}
              </p>
            ) : (
              <section className="space-y-3" aria-label={copy.bindingsTitle}>
                <div className="flex items-center gap-2">
                  <KeyRoundIcon aria-hidden className="size-4" />
                  <h3 className="text-sm font-semibold">
                    {copy.bindingsTitle}
                  </h3>
                </div>
                <div className="space-y-2">
                  {plan.requirements.map((requirement) => (
                    <div
                      key={requirement.name}
                      className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2"
                    >
                      <code className="min-w-0 flex-1 text-sm break-all">
                        {requirement.name}
                      </code>
                      <Badge variant="secondary">
                        {requirement.optional ? copy.optional : copy.required}
                      </Badge>
                      <Badge
                        variant={
                          requirement.mapping_status === "configured"
                            ? "default"
                            : "secondary"
                        }
                      >
                        {mappingStatusLabel(requirement.mapping_status, copy)}
                      </Badge>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {!plan.ready ? (
              <p role="alert" className="text-destructive text-xs">
                {copy.configureBeforePublish}
              </p>
            ) : null}
            {staleBaseRequired ? (
              <div className="border-warning/40 bg-warning/10 rounded-lg border p-3 text-sm">
                <p role="alert">{copy.staleBase}</p>
              </div>
            ) : null}
            {notice ? (
              <p role="status" className="text-muted-foreground text-sm">
                {notice}
              </p>
            ) : null}
          </div>
        ) : null}

        {dialogError ? (
          <div className="text-destructive flex items-start gap-2 text-sm">
            <AlertCircleIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
            <p role="alert">{dialogError}</p>
          </div>
        ) : null}

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={publish.isPending}
            onClick={() => onOpenChange(false)}
          >
            {copy.cancel}
          </Button>
          {planQuery.error ? (
            <Button
              type="button"
              variant="outline"
              disabled={planQuery.isFetching}
              onClick={() => void refreshPlan()}
            >
              <RefreshCwIcon aria-hidden className="size-4" />
              {copy.retry}
            </Button>
          ) : null}
          {plan && !plan.ready ? (
            <Button
              type="button"
              onClick={() => {
                onOpenChange(false);
                onConfigureCredentials();
              }}
            >
              {copy.configureCredentials}
            </Button>
          ) : null}
          <Button
            type="button"
            variant={staleBaseRequired ? "destructive" : "default"}
            disabled={
              !plan || !plan.ready || planQuery.isFetching || publish.isPending
            }
            onClick={() => submitPublish(staleBaseRequired)}
          >
            {publish.isPending ? (
              <Loader2Icon aria-hidden className="size-4 animate-spin" />
            ) : null}
            {publish.isPending
              ? copy.publishing
              : staleBaseRequired
                ? copy.confirmOverwrite
                : copy.publish}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

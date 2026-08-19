"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  AlertCircleIcon,
  CheckCircle2Icon,
  KeyRoundIcon,
  Loader2Icon,
  RefreshCwIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { CredentialSecretDialog } from "@/components/admin/assets/admin-asset-dialogs";
import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
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
  createProjectCredential,
  initialSkillPublishSelections,
  mergeSkillPublishSelections,
  missingRequiredSkillPublishRequirements,
  projectAssetKey,
  skillPublishRequiredBindingsBlocked,
  useProjectSkillPublishPlan,
  usePublishProjectAssetVersion,
  type CreateCredentialInput,
  type ProjectAssetItem,
  type SkillPublishPlanResponse,
  type SkillPublishSelections,
} from "@/core/shared-assets";

import type { SkillAssetVersion } from "./skill-asset-detail";
import { SkillCredentialOptionSelect } from "./skill-credential-option-select";

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

export function SkillPublishDialog({
  open,
  accountId,
  projectId,
  item,
  version,
  canApproveCredentials,
  credentialsHref,
  onOpenChange,
  onPublished,
}: {
  open: boolean;
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  version: SkillAssetVersion;
  canApproveCredentials: boolean;
  credentialsHref: string;
  onOpenChange: (open: boolean) => void;
  onPublished: (versionId: string) => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.publishDialog;
  const queryClient = useQueryClient();
  const planQuery = useProjectSkillPublishPlan(
    accountId,
    projectId,
    item.id,
    version.id,
    open,
  );
  const publish = usePublishProjectAssetVersion(accountId, projectId, "skills");
  const [selections, setSelections] = useState<SkillPublishSelections>({});
  const [selectionDirty, setSelectionDirty] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [staleBaseRequired, setStaleBaseRequired] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [credentialRequirementName, setCredentialRequirementName] = useState<
    string | null
  >(null);
  const [credentialWritePending, setCredentialWritePending] = useState(false);
  const [credentialWriteError, setCredentialWriteError] = useState<
    string | null
  >(null);
  const appliedPlanIdentity = useRef<string | null>(null);
  const editedRequirementNames = useRef(new Set<string>());
  const requirementRefs = useRef(new Map<string, HTMLSelectElement>());
  const credentialWriteAbort = useRef<AbortController | null>(null);
  const publishResetRef = useRef(publish.reset);
  publishResetRef.current = publish.reset;
  const plan = planQuery.data ?? null;

  useEffect(() => {
    if (!open || !plan) return;
    const identity = planIdentity(plan);
    if (appliedPlanIdentity.current === identity) return;
    setSelections((current) =>
      appliedPlanIdentity.current === null
        ? initialSkillPublishSelections(plan)
        : mergeSkillPublishSelections(
            current,
            plan,
            editedRequirementNames.current,
          ),
    );
    appliedPlanIdentity.current = identity;
  }, [open, plan]);

  useEffect(() => {
    if (open) return;
    credentialWriteAbort.current?.abort();
    credentialWriteAbort.current = null;
    appliedPlanIdentity.current = null;
    editedRequirementNames.current.clear();
    setSelections({});
    setSelectionDirty(false);
    setAttempted(false);
    setStaleBaseRequired(false);
    setNotice(null);
    setDiscardOpen(false);
    setCredentialRequirementName(null);
    setCredentialWritePending(false);
    setCredentialWriteError(null);
    publishResetRef.current();
  }, [open]);

  useEffect(
    () => () => {
      credentialWriteAbort.current?.abort();
    },
    [projectId],
  );

  const missingRequired = plan
    ? missingRequiredSkillPublishRequirements(plan, selections)
    : [];
  const optionalUnbound =
    plan?.requirements.filter(
      (requirement) => requirement.optional && !selections[requirement.name],
    ) ?? [];
  const mustConfigureRequired = plan
    ? skillPublishRequiredBindingsBlocked({
        plan,
        selections,
        skillActive: item.status === "active",
        canApproveCredentials,
      })
    : false;
  const approvalRequiredForActive =
    plan !== null &&
    item.status === "active" &&
    !canApproveCredentials &&
    plan.requirements.some((requirement) => !requirement.optional);

  function requestClose() {
    if (publish.isPending || credentialWritePending) return;
    if (selectionDirty) {
      setDiscardOpen(true);
      return;
    }
    onOpenChange(false);
  }

  async function refreshPlan(message?: string) {
    const result = await planQuery.refetch();
    if (result.data) {
      setSelections((current) =>
        mergeSkillPublishSelections(
          current,
          result.data,
          editedRequirementNames.current,
        ),
      );
      appliedPlanIdentity.current = planIdentity(result.data);
    }
    if (message) setNotice(message);
    return result.data ?? null;
  }

  function focusFirstMissingRequirement() {
    const first = missingRequired[0];
    if (!first) return;
    requestAnimationFrame(() =>
      requirementRefs.current.get(first.name)?.focus(),
    );
  }

  function submitPublish(acknowledgeStaleBase = false) {
    if (!plan || publish.isPending || credentialWritePending) return;
    setAttempted(true);
    setNotice(null);
    if (mustConfigureRequired) {
      focusFirstMissingRequirement();
      return;
    }
    const includeCredentialBindings =
      canApproveCredentials && plan.requirements.length > 0;
    publish.mutate(
      {
        assetId: item.id,
        versionId: version.id,
        input: buildSkillPublishInput({
          plan,
          selections,
          includeCredentialBindings,
          acknowledgeStaleBase,
        }),
      },
      {
        onSuccess: (response) => {
          if (!("skill_id" in response.data)) return;
          setSelectionDirty(false);
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

  async function createCredential(input: CreateCredentialInput) {
    const requirementName = credentialRequirementName;
    if (!requirementName || !canApproveCredentials) return;
    credentialWriteAbort.current?.abort();
    const controller = new AbortController();
    credentialWriteAbort.current = controller;
    setCredentialWritePending(true);
    setCredentialWriteError(null);
    try {
      const result = await createProjectCredential(
        projectId,
        input,
        controller.signal,
      );
      if (
        controller.signal.aborted ||
        credentialWriteAbort.current !== controller
      )
        return;
      const credentialVersionId = result.item.current_version_id;
      if (!credentialVersionId) {
        setCredentialWriteError(copy.createdInvalid);
        return;
      }
      await queryClient.invalidateQueries({
        queryKey: projectAssetKey(accountId, projectId, "credentials"),
      });
      const refreshed = await refreshPlan();
      const requirement = refreshed?.requirements.find(
        (candidate) => candidate.name === requirementName,
      );
      if (
        !requirement?.eligible_credentials.some(
          (credential) =>
            credential.credential_version_id === credentialVersionId,
        )
      ) {
        setCredentialWriteError(copy.createdIneligible);
        return;
      }
      setSelections((current) => ({
        ...current,
        [requirementName]: credentialVersionId,
      }));
      editedRequirementNames.current.add(requirementName);
      setSelectionDirty(true);
      setCredentialRequirementName(null);
    } catch (error) {
      if (!controller.signal.aborted) {
        setCredentialWriteError(adminAssetErrorMessage(error));
      }
    } finally {
      if (credentialWriteAbort.current === controller) {
        credentialWriteAbort.current = null;
        setCredentialWritePending(false);
      }
    }
  }

  const dialogError =
    publish.error &&
    !(
      publish.error instanceof SharedAssetApiError &&
      publish.error.code === "SKILL_CREDENTIAL_SELECTION_STALE"
    )
      ? publishErrorMessage(publish.error, copy)
      : planQuery.error
        ? publishErrorMessage(planQuery.error, copy)
        : null;

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (next) return;
          requestClose();
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

              {plan.requirements.length === 0 ? (
                <div className="flex items-start gap-2 rounded-lg border p-4 text-sm">
                  <CheckCircle2Icon
                    aria-hidden
                    className="text-success mt-0.5 size-4 shrink-0"
                  />
                  <p>{copy.noRequirements}</p>
                </div>
              ) : (
                <section className="space-y-3" aria-label={copy.bindingsTitle}>
                  <div className="flex items-center gap-2">
                    <KeyRoundIcon aria-hidden className="size-4" />
                    <h3 className="text-sm font-semibold">
                      {copy.bindingsTitle}
                    </h3>
                  </div>
                  {!canApproveCredentials ? (
                    <div className="space-y-2">
                      <p
                        role="status"
                        className="text-muted-foreground text-xs"
                      >
                        {copy.noApprove}
                      </p>
                      {approvalRequiredForActive ? (
                        <p role="alert" className="text-destructive text-xs">
                          {copy.approvalRequiredForActive}
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                  {plan.requirements.map((requirement) => (
                    <SkillCredentialOptionSelect
                      key={requirement.name}
                      ref={(element) => {
                        if (element) {
                          requirementRefs.current.set(
                            requirement.name,
                            element,
                          );
                        } else {
                          requirementRefs.current.delete(requirement.name);
                        }
                      }}
                      name={requirement.name}
                      optional={requirement.optional}
                      options={requirement.eligible_credentials}
                      value={selections[requirement.name] ?? ""}
                      disabled={
                        publish.isPending ||
                        credentialWritePending ||
                        !canApproveCredentials
                      }
                      error={
                        attempted &&
                        canApproveCredentials &&
                        !requirement.optional &&
                        !selections[requirement.name] &&
                        mustConfigureRequired
                      }
                      onChange={(credentialVersionId) => {
                        editedRequirementNames.current.add(requirement.name);
                        setSelections((current) => {
                          const next = { ...current };
                          if (credentialVersionId) {
                            next[requirement.name] = credentialVersionId;
                          } else {
                            delete next[requirement.name];
                          }
                          return next;
                        });
                        setSelectionDirty(true);
                        setAttempted(false);
                        setNotice(null);
                      }}
                      onCreate={
                        canApproveCredentials
                          ? () => {
                              setCredentialWriteError(null);
                              setCredentialRequirementName(requirement.name);
                            }
                          : undefined
                      }
                      manageHref={
                        canApproveCredentials && !selectionDirty
                          ? credentialsHref
                          : undefined
                      }
                    />
                  ))}
                </section>
              )}

              {optionalUnbound.length > 0 ? (
                <p role="status" className="text-muted-foreground text-xs">
                  {copy.optionalUnbound(optionalUnbound.length)}
                </p>
              ) : null}
              {staleBaseRequired ? (
                <div className="border-warning/40 bg-warning/10 rounded-lg border p-3 text-sm">
                  <p role="alert">{copy.staleBase}</p>
                </div>
              ) : null}
              {notice ? (
                <p
                  role="status"
                  aria-live="polite"
                  className="text-muted-foreground text-sm"
                >
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
              disabled={publish.isPending || credentialWritePending}
              onClick={requestClose}
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
            <Button
              type="button"
              variant={staleBaseRequired ? "destructive" : "default"}
              disabled={
                !plan ||
                planQuery.isFetching ||
                publish.isPending ||
                approvalRequiredForActive
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

      <CredentialSecretDialog
        mode="create"
        open={credentialRequirementName !== null}
        pending={credentialWritePending}
        fixedFields
        fixedCredentialType="skill_auth"
        errorMessage={credentialWriteError}
        initialFields={
          credentialRequirementName
            ? [{ group: "env", field: credentialRequirementName }]
            : []
        }
        onOpenChange={(next) => {
          if (!next && credentialWritePending) return;
          if (!next) {
            setCredentialRequirementName(null);
            setCredentialWriteError(null);
          }
        }}
        onCreate={(input) => void createCredential(input)}
      />

      <Dialog open={discardOpen} onOpenChange={setDiscardOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{copy.discardTitle}</DialogTitle>
            <DialogDescription>{copy.discardDescription}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDiscardOpen(false)}
            >
              {copy.continue}
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => {
                setSelectionDirty(false);
                setDiscardOpen(false);
                onOpenChange(false);
              }}
            >
              {copy.discard}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

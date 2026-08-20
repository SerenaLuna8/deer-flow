"use client";

import { AlertCircleIcon, KeyRoundIcon } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import {
  SharedAssetApiError,
  skillCredentialBindingsInputSchema,
  useProjectSkillCredentialBindings,
  useUpdateProjectSkillCredentialBindings,
  type SkillCredentialBindingsInput,
  type SkillCredentialBindingsResponse,
  type SkillCredentialMappingStatus,
  type SkillCredentialRequirement,
} from "@/core/shared-assets";

import {
  SkillCredentialOptionSelect,
  skillCredentialRequirementOptions,
} from "./skill-credential-option-select";

export type BindingSelection = {
  credentialVersionId: string;
  sourceEnvFieldName: string;
};

export type BindingSelections = Record<string, BindingSelection>;

type SkillSecretsCopy = Translations["skills"]["secrets"];

export type SkillCredentialVersionGuard = {
  retained: SkillCredentialBindingsResponse | null;
  conflicted: boolean;
};

export function guardedSkillCredentialBindingResponse(
  guard: SkillCredentialVersionGuard,
  expectedSkillVersionId: string,
  response: SkillCredentialBindingsResponse | undefined,
): {
  data: SkillCredentialBindingsResponse | null;
  versionChanged: boolean;
} {
  const responseMatches = response?.skill_version_id === expectedSkillVersionId;
  const retainedVersionChanged = Boolean(
    guard.retained &&
    guard.retained.skill_version_id !== expectedSkillVersionId,
  );
  const versionChanged =
    guard.conflicted ||
    retainedVersionChanged ||
    Boolean(response && !responseMatches);

  if (guard.conflicted || retainedVersionChanged) {
    return { data: guard.retained, versionChanged };
  }
  if (responseMatches) return { data: response ?? null, versionChanged };
  if (guard.retained?.skill_version_id === expectedSkillVersionId) {
    return { data: guard.retained, versionChanged };
  }
  return { data: null, versionChanged };
}

export function skillCredentialVersionGuardAfterResponse(
  current: SkillCredentialVersionGuard,
  expectedSkillVersionId: string,
  response: SkillCredentialBindingsResponse | undefined,
): SkillCredentialVersionGuard {
  if (!response) return current;
  if (
    current.retained &&
    current.retained.skill_version_id !== expectedSkillVersionId
  ) {
    return current.conflicted ? current : { ...current, conflicted: true };
  }
  if (response.skill_version_id === expectedSkillVersionId) {
    if (current.conflicted || current.retained === response) return current;
    return { retained: response, conflicted: false };
  }
  if (!current.retained || current.conflicted) return current;
  return { ...current, conflicted: true };
}

export function skillCredentialBindingCanUnbind(
  skillActive: boolean,
  requirement: Pick<SkillCredentialRequirement, "optional">,
): boolean {
  return !skillActive || requirement.optional;
}

function configuredSelections(
  data: SkillCredentialBindingsResponse,
): BindingSelections {
  return Object.fromEntries(
    data.requirements.flatMap((requirement) =>
      requirement.configured &&
      requirement.credential_version_id !== null &&
      requirement.source_env_field_name !== null
        ? [
            [
              requirement.name,
              {
                credentialVersionId: requirement.credential_version_id,
                sourceEnvFieldName: requirement.source_env_field_name,
              },
            ] as const,
          ]
        : [],
    ),
  );
}

function sortedBindingEntries(
  selections: BindingSelections,
): Array<[string, string, string]> {
  return Object.entries(selections)
    .filter(
      ([, selection]) =>
        selection.credentialVersionId !== "" &&
        selection.sourceEnvFieldName !== "",
    )
    .map<[string, string, string]>(([name, selection]) => [
      name,
      selection.credentialVersionId,
      selection.sourceEnvFieldName,
    ])
    .sort(([left], [right]) => left.localeCompare(right));
}

function selectionsEqual(
  left: BindingSelections,
  right: BindingSelections,
): boolean {
  return (
    JSON.stringify(sortedBindingEntries(left)) ===
      JSON.stringify(sortedBindingEntries(right)) &&
    Object.values(left).every(
      (selection) =>
        selection.credentialVersionId !== "" &&
        selection.sourceEnvFieldName !== "",
    )
  );
}

export function skillCredentialSelectionsAfterServerRefresh(
  current: BindingSelections,
  previousOriginal: BindingSelections,
  nextOriginal: BindingSelections,
  nextRequirementNames: readonly string[],
): {
  selections: BindingSelections;
  preservedLocalChanges: boolean;
} {
  const selections: BindingSelections = {};
  let preservedLocalChanges = false;
  for (const name of nextRequirementNames) {
    const currentValue = current[name];
    const previousValue = previousOriginal[name];
    const nextValue = nextOriginal[name];
    const locallyChanged =
      JSON.stringify(currentValue ?? null) !==
      JSON.stringify(previousValue ?? null);
    const selectedValue = locallyChanged ? currentValue : nextValue;
    if (
      locallyChanged &&
      JSON.stringify(currentValue ?? null) !== JSON.stringify(nextValue ?? null)
    ) {
      preservedLocalChanges = true;
    }
    if (selectedValue) selections[name] = selectedValue;
  }
  return { selections, preservedLocalChanges };
}

export function skillCredentialBindingsPayload(
  expectedRevision: number,
  selections: BindingSelections,
): SkillCredentialBindingsInput {
  return skillCredentialBindingsInputSchema.parse({
    expected_revision: expectedRevision,
    bindings: sortedBindingEntries(selections).map(
      ([name, credentialVersionId, sourceEnvFieldName]) => ({
        name,
        credential_version_id: credentialVersionId,
        source_env_field_name: sourceEnvFieldName,
      }),
    ),
  });
}

function bindingErrorMessage(
  error: unknown,
  action: "load" | "save",
  copy: SkillSecretsCopy,
): string | null {
  if (!error) return null;
  if (error instanceof SharedAssetApiError) {
    if (error.status === 409) {
      return copy.mappingConflict;
    }
    if (error.status === 403) return copy.mappingForbidden;
    if (error.status === 404) return copy.mappingNotFound;
    if (error.code === "ASSET_RESPONSE_INVALID") {
      return copy.mappingInvalidResponse;
    }
  }
  return action === "load" ? copy.mappingLoadFailed : copy.mappingSaveFailed;
}

function localMappingStatus(
  requirement: SkillCredentialRequirement,
  selection: BindingSelection | undefined,
): SkillCredentialMappingStatus {
  if (!selection) return "missing";
  const credential = requirement.eligible_credentials.find(
    (candidate) =>
      candidate.credential_version_id === selection.credentialVersionId,
  );
  return credential?.env_fields.includes(selection.sourceEnvFieldName)
    ? "configured"
    : "invalid";
}

export function skillCredentialBindingValidation(
  requirements: readonly SkillCredentialRequirement[],
  selections: BindingSelections,
  skillActive: boolean,
): {
  statuses: Record<string, SkillCredentialMappingStatus>;
  hasInvalidSelection: boolean;
  hasBlockingMissingRequired: boolean;
} {
  const statuses: Record<string, SkillCredentialMappingStatus> =
    Object.fromEntries(
      requirements.map((requirement) => [
        requirement.name,
        localMappingStatus(requirement, selections[requirement.name]),
      ]),
    );
  return {
    statuses,
    hasInvalidSelection: requirements.some(
      (requirement) => statuses[requirement.name] === "invalid",
    ),
    hasBlockingMissingRequired:
      skillActive &&
      requirements.some(
        (requirement) =>
          !requirement.optional && statuses[requirement.name] === "missing",
      ),
  };
}

function ReadOnlyRequirement({
  requirement,
}: {
  requirement: SkillCredentialRequirement;
}) {
  const { t } = useI18n();
  const copy = t.skills.secrets;
  const statusLabel =
    requirement.mapping_status === "configured"
      ? copy.mappingStatusConfigured
      : requirement.mapping_status === "invalid"
        ? copy.mappingStatusInvalid
        : copy.mappingStatusMissing;
  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <code className="min-w-0 flex-1 text-sm font-medium break-all">
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
          {statusLabel}
        </Badge>
      </div>
      {requirement.credential_display_name &&
      requirement.credential_version_number &&
      requirement.source_env_field_name ? (
        <p className="text-muted-foreground text-xs">
          <code>{requirement.name}</code> ←{" "}
          {requirement.credential_display_name}
          {` · ${copy.versionLabel(requirement.credential_version_number)}`} /{" "}
          <code>env.{requirement.source_env_field_name}</code>
        </p>
      ) : null}
    </div>
  );
}

export function SkillCredentialBindingEditor({
  data,
  skillActive,
  canManage,
  readOnlyReason,
  credentialsHref,
  pending,
  errorMessage,
  onReload,
  onSave,
  onDirtyChange,
}: {
  data: SkillCredentialBindingsResponse;
  skillActive: boolean;
  canManage: boolean;
  readOnlyReason?: "approval" | "historical";
  credentialsHref: string;
  pending: boolean;
  errorMessage: string | null;
  onReload: () => void;
  onSave: (input: SkillCredentialBindingsInput) => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.secrets;
  const originalSelections = useMemo(() => configuredSelections(data), [data]);
  const [selections, setSelections] =
    useState<BindingSelections>(originalSelections);
  const [serverRefreshPreserved, setServerRefreshPreserved] = useState(false);
  const sourceIdentity = `${data.skill_version_id}:${data.revision}`;
  const appliedSourceIdentityRef = useRef(sourceIdentity);
  const baselineSelectionsRef = useRef(originalSelections);
  const selectionsRef = useRef(selections);
  selectionsRef.current = selections;

  useEffect(() => {
    if (appliedSourceIdentityRef.current === sourceIdentity) return;
    appliedSourceIdentityRef.current = sourceIdentity;
    const reconciled = skillCredentialSelectionsAfterServerRefresh(
      selectionsRef.current,
      baselineSelectionsRef.current,
      originalSelections,
      data.requirements.map((requirement) => requirement.name),
    );
    baselineSelectionsRef.current = originalSelections;
    setSelections(reconciled.selections);
    setServerRefreshPreserved(reconciled.preservedLocalChanges);
  }, [data.requirements, originalSelections, sourceIdentity]);
  const dirty = !selectionsEqual(selections, originalSelections);
  const hasPartialSelection = Object.values(selections).some(
    (selection) =>
      (selection.credentialVersionId === "") !==
      (selection.sourceEnvFieldName === ""),
  );
  const validation = skillCredentialBindingValidation(
    data.requirements,
    selections,
    skillActive,
  );
  const saveBlockedTitle = validation.hasInvalidSelection
    ? copy.mappingRepairInvalid
    : validation.hasBlockingMissingRequired
      ? copy.mappingCompleteRequired
      : hasPartialSelection
        ? copy.sourceFieldRequired
        : undefined;

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(
    () => () => {
      onDirtyChange?.(false);
    },
    [onDirtyChange],
  );

  return (
    <section className="border-border/70 space-y-4 rounded-xl border p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <KeyRoundIcon aria-hidden className="size-4" />
            <h3 className="text-sm font-semibold">{copy.mappingTitle}</h3>
          </div>
          <p className="text-muted-foreground mt-1 text-xs leading-5">
            {copy.mappingDescription}
          </p>
        </div>
        {canManage && !dirty ? (
          <Button asChild type="button" variant="outline" size="sm">
            <Link href={credentialsHref}>{copy.manageCredential}</Link>
          </Button>
        ) : null}
      </div>

      {data.requirements.length === 0 ? (
        <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
          {copy.mappingEmpty}
        </p>
      ) : canManage ? (
        <div className="space-y-3">
          {data.requirements.map((requirement) => {
            const selection = selections[requirement.name];
            const selectedVersionId = selection?.credentialVersionId ?? "";
            const sourceEnvFieldName = selection?.sourceEnvFieldName ?? "";
            const options = skillCredentialRequirementOptions(
              requirement,
              selectedVersionId,
            );
            const rowStatus = dirty
              ? (validation.statuses[requirement.name] ?? "missing")
              : requirement.mapping_status;
            const rowError =
              rowStatus === "invalid" ||
              (skillActive && !requirement.optional && rowStatus === "missing");
            return (
              <SkillCredentialOptionSelect
                key={requirement.name}
                name={requirement.name}
                optional={requirement.optional}
                mappingStatus={rowStatus}
                options={options}
                credentialVersionId={selectedVersionId}
                sourceEnvFieldName={sourceEnvFieldName}
                disabled={pending}
                error={rowError}
                allowEmpty={skillCredentialBindingCanUnbind(
                  skillActive,
                  requirement,
                )}
                manageHref={!dirty ? credentialsHref : undefined}
                onCredentialChange={(credentialVersionId) =>
                  setSelections((current) => {
                    const next = { ...current };
                    if (!credentialVersionId) {
                      delete next[requirement.name];
                      return next;
                    }
                    const candidate = requirement.eligible_credentials.find(
                      (credential) =>
                        credential.credential_version_id ===
                        credentialVersionId,
                    );
                    const previousSource =
                      current[requirement.name]?.sourceEnvFieldName;
                    const sourceEnvFieldName =
                      previousSource &&
                      candidate?.env_fields.includes(previousSource)
                        ? previousSource
                        : candidate?.env_fields.includes(requirement.name)
                          ? requirement.name
                          : "";
                    next[requirement.name] = {
                      credentialVersionId,
                      sourceEnvFieldName,
                    };
                    return next;
                  })
                }
                onSourceEnvFieldChange={(sourceEnvFieldName) =>
                  setSelections((current) => ({
                    ...current,
                    [requirement.name]: {
                      credentialVersionId:
                        current[requirement.name]?.credentialVersionId ?? "",
                      sourceEnvFieldName,
                    },
                  }))
                }
              />
            );
          })}
        </div>
      ) : (
        <div className="space-y-3">
          {data.requirements.map((requirement) => (
            <ReadOnlyRequirement
              key={requirement.name}
              requirement={requirement}
            />
          ))}
          <p role="status" className="text-muted-foreground text-xs leading-5">
            {readOnlyReason === "historical"
              ? copy.mappingHistoricalReadOnly
              : copy.mappingReadOnly}
          </p>
        </div>
      )}

      {serverRefreshPreserved ? (
        <p role="status" className="text-muted-foreground text-sm">
          {copy.mappingRefreshPreserved}
        </p>
      ) : null}

      {errorMessage ? (
        <div
          role="alert"
          className="text-destructive flex items-start gap-2 text-sm"
        >
          <AlertCircleIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
          <span>{errorMessage}</span>
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto p-0"
            onClick={() => {
              setServerRefreshPreserved(false);
              onReload();
            }}
          >
            {copy.mappingReload}
          </Button>
        </div>
      ) : null}

      {canManage && data.requirements.length > 0 ? (
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={!dirty || pending}
            onClick={() => {
              setSelections(originalSelections);
              baselineSelectionsRef.current = originalSelections;
              setServerRefreshPreserved(false);
            }}
          >
            {copy.mappingDiscard}
          </Button>
          <Button
            type="button"
            disabled={
              !dirty ||
              pending ||
              hasPartialSelection ||
              validation.hasInvalidSelection ||
              validation.hasBlockingMissingRequired
            }
            title={saveBlockedTitle}
            onClick={() =>
              onSave(skillCredentialBindingsPayload(data.revision, selections))
            }
          >
            {pending ? copy.mappingSaving : copy.mappingSave}
          </Button>
        </div>
      ) : null}
    </section>
  );
}

export function SkillCredentialBindings({
  accountId,
  projectId,
  skillId,
  versionId,
  skillActive,
  canManage,
  readOnlyReason,
  credentialsHref,
  onDirtyChange,
}: {
  accountId: string;
  projectId: string;
  skillId: string;
  versionId: string;
  skillActive: boolean;
  canManage: boolean;
  readOnlyReason?: "approval" | "historical";
  credentialsHref: string;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.secrets;
  const [versionGuard, setVersionGuard] = useState<SkillCredentialVersionGuard>(
    { retained: null, conflicted: false },
  );
  const bindings = useProjectSkillCredentialBindings(
    accountId,
    projectId,
    skillId,
    versionId,
  );
  const update = useUpdateProjectSkillCredentialBindings(
    accountId,
    projectId,
    skillId,
    versionId,
  );
  const bindingData = bindings.data;

  useEffect(() => {
    setVersionGuard((current) =>
      skillCredentialVersionGuardAfterResponse(current, versionId, bindingData),
    );
  }, [bindingData, versionId]);

  const guardedResponse = guardedSkillCredentialBindingResponse(
    versionGuard,
    versionId,
    bindingData,
  );
  const guardedBindingData = guardedResponse.data;
  const staleUpdate =
    update.error instanceof SharedAssetApiError && update.error.status === 409;
  const saveBlockedByVersionChange =
    guardedResponse.versionChanged || Boolean(bindings.error) || staleUpdate;

  if (!guardedBindingData && bindings.isLoading) {
    return (
      <section
        aria-busy="true"
        aria-label={copy.mappingLoadingAria}
        className="border-border/70 space-y-3 rounded-xl border p-4"
      >
        <Skeleton className="h-5 w-28" />
        <Skeleton className="h-20 w-full" />
      </section>
    );
  }

  if (!guardedBindingData) {
    return (
      <section className="border-border/70 space-y-3 rounded-xl border p-4">
        <div className="flex items-center gap-2">
          <KeyRoundIcon aria-hidden className="size-4" />
          <h3 className="text-sm font-semibold">{copy.mappingTitle}</h3>
        </div>
        <p role="alert" className="text-destructive text-sm">
          {bindingData && bindingData.skill_version_id !== versionId
            ? copy.mappingVersionMismatch
            : (bindingErrorMessage(bindings.error, "load", copy) ??
              copy.mappingLoadFailed)}
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void bindings.refetch()}
        >
          {copy.mappingRetry}
        </Button>
      </section>
    );
  }

  return (
    <SkillCredentialBindingEditor
      data={guardedBindingData}
      skillActive={skillActive}
      canManage={canManage && !saveBlockedByVersionChange}
      readOnlyReason={readOnlyReason}
      credentialsHref={credentialsHref}
      pending={update.isPending}
      errorMessage={
        guardedResponse.versionChanged
          ? copy.mappingVersionChanged
          : (bindingErrorMessage(bindings.error, "load", copy) ??
            bindingErrorMessage(update.error, "save", copy))
      }
      onDirtyChange={onDirtyChange}
      onReload={() => {
        update.reset();
        setVersionGuard({
          retained:
            bindingData?.skill_version_id === versionId ? bindingData : null,
          conflicted: false,
        });
        void bindings.refetch();
      }}
      onSave={(input) => update.mutate(input)}
    />
  );
}

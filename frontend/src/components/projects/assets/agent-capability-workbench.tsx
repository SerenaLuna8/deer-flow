"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Loader2Icon, SearchIcon, WrenchIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
} from "@/core/private-work/types";
import {
  SharedAssetApiError,
  useProjectAssets,
  useUpdateProjectAgentCapabilityBindings,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

import {
  cacheProjectAgentAuthoringReload,
  projectAgentAuthoringCacheEpochs,
  reloadProjectAgentAuthoringState,
} from "./agent-authoring-recovery";
import { useMcpDependencyRuntime } from "./use-mcp-dependency-runtime";

export { agentAuthoringBaseVersion } from "./agent-authoring-recovery";

type AgentAssetVersion = Extract<AssetVersion, { agent_id: string }>;
type DependencyKind = "skill" | "mcp";

export type AgentDependencyOption = {
  kind: DependencyKind;
  assetId: string;
  versionId: string | null;
  scope: "system" | "project";
  name: string;
  slug: string;
  disabled: boolean;
  reason: string | null;
  remediation: string | null;
};

type CapabilityCopy = Translations["agents"]["capabilities"];

function inactiveReason(
  status: ProjectAssetItem["status"],
  copy: CapabilityCopy,
): string | null {
  if (status === "active") return null;
  return status === "archived" ? copy.reasons.archived : copy.reasons.inactive;
}

function joinExplanations(
  values: readonly (string | null)[],
  separator: string,
): string | null {
  const unique = [
    ...new Set(values.filter((value): value is string => !!value)),
  ];
  return unique.length > 0 ? unique.join(separator) : null;
}

export function agentDependencyOptions(
  kind: DependencyKind,
  catalog: ProjectAssetList,
  copy: CapabilityCopy,
): AgentDependencyOption[] {
  const systemOptions = catalog.system_items.map((item) => {
    const currentVersionId =
      item.binding?.current_version_id ?? item.current_version_id;
    const versionId = currentVersionId
      ? kind === "skill"
        ? `system:${item.id}`
        : currentVersionId
      : null;
    const statusReason = inactiveReason(item.status, copy);
    const bindingReason = item.binding?.enabled
      ? null
      : item.binding
        ? copy.reasons.bindingDisabled
        : copy.reasons.bindingMissing;
    const versionReason = versionId ? null : copy.reasons.noCurrentVersion;
    const reason = joinExplanations(
      [statusReason, bindingReason, versionReason],
      copy.explanationSeparator,
    );
    return {
      kind,
      assetId: item.id,
      versionId,
      scope: "system" as const,
      name: item.display_name,
      slug: item.slug,
      disabled: reason !== null,
      reason,
      remediation: joinExplanations(
        [
          statusReason ? copy.remediation.restoreSystemAsset : null,
          bindingReason ? copy.remediation.enableSystemBinding : null,
          versionReason ? copy.remediation.activateCandidateVersion : null,
        ],
        copy.explanationSeparator,
      ),
    };
  });
  const projectOptions = catalog.project_items.map((item) => {
    const statusReason = inactiveReason(item.status, copy);
    const versionReason = item.current_version_id
      ? null
      : copy.reasons.noCurrentVersion;
    const reason = joinExplanations(
      [statusReason, versionReason],
      copy.explanationSeparator,
    );
    return {
      kind,
      assetId: item.id,
      versionId: item.current_version_id
        ? kind === "skill"
          ? `project:${item.id}`
          : item.current_version_id
        : null,
      scope: "project" as const,
      name: item.display_name,
      slug: item.slug,
      disabled: reason !== null,
      reason,
      remediation: joinExplanations(
        [
          statusReason ? copy.remediation.activateProjectAsset : null,
          versionReason ? copy.remediation.activateCandidateVersion : null,
        ],
        copy.explanationSeparator,
      ),
    };
  });
  return [...systemOptions, ...projectOptions];
}

export function agentDependencyOptionCanToggle(
  option: AgentDependencyOption,
  selected: boolean,
  editing: boolean,
): boolean {
  return editing && option.versionId !== null && (!option.disabled || selected);
}

export type AgentCapabilityConflictRecovery = {
  assetId: string;
  assetRevision: number;
  generation: number;
  status: "refreshing" | "error";
};

function sameIds(left: readonly string[], right: readonly string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function toggleId(values: readonly string[], value: string): string[] {
  return values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
}

export function AgentDependencySection({
  title,
  emptyLabel,
  options,
  selectedIds,
  editing,
  query,
  onToggle,
}: {
  title: string;
  emptyLabel: string;
  options: readonly AgentDependencyOption[];
  selectedIds: readonly string[];
  editing: boolean;
  query: string;
  onToggle: (versionId: string) => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.capabilities;
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visible = options.filter((option) => {
    if (!normalizedQuery) return true;
    return [option.name, option.slug].some((value) =>
      value.toLocaleLowerCase().includes(normalizedQuery),
    );
  });
  const knownIds = new Set(
    options.flatMap((option) =>
      option.versionId === null ? [] : [option.versionId],
    ),
  );
  const unresolvedIds = selectedIds.filter((id) => !knownIds.has(id));

  return (
    <section className="space-y-3" aria-label={title}>
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold">{title}</h3>
        <Badge variant="secondary">{copy.boundCount(selectedIds.length)}</Badge>
      </div>
      {visible.length === 0 && unresolvedIds.length === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed p-4 text-sm">
          {emptyLabel}
        </p>
      ) : (
        <div className="border-border/70 max-h-72 overflow-y-auto rounded-xl border">
          {visible.map((option) => {
            const checked =
              option.versionId !== null &&
              selectedIds.includes(option.versionId);
            const canToggle = agentDependencyOptionCanToggle(
              option,
              checked,
              editing,
            );
            return (
              <label
                key={`${option.scope}:${option.assetId}`}
                aria-disabled={!canToggle}
                className={`flex items-start gap-3 border-b px-4 py-3 last:border-b-0 ${
                  canToggle
                    ? "hover:bg-muted/40 cursor-pointer"
                    : "cursor-not-allowed opacity-70"
                }`}
              >
                <input
                  type="checkbox"
                  className="accent-foreground mt-1 size-4 shrink-0"
                  checked={checked}
                  disabled={!canToggle}
                  onChange={() => {
                    if (option.versionId) onToggle(option.versionId);
                  }}
                />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{option.name}</span>
                    <Badge variant="outline">
                      {option.scope === "system"
                        ? t.agents.common.system
                        : t.agents.common.project}
                    </Badge>
                  </span>
                  <span className="text-muted-foreground mt-1 block font-mono text-xs">
                    {option.slug}
                  </span>
                  {option.reason ? (
                    <span className="text-warning-foreground mt-2 block text-xs">
                      {copy.unavailablePrefix(option.reason)}
                    </span>
                  ) : null}
                  {option.remediation ? (
                    <span className="text-muted-foreground mt-1 block text-xs">
                      {copy.remediationPrefix(option.remediation)}
                    </span>
                  ) : null}
                  {checked && option.disabled ? (
                    <span className="text-muted-foreground mt-1 block text-xs">
                      {copy.historicalDisabled}
                    </span>
                  ) : null}
                </span>
              </label>
            );
          })}
          {unresolvedIds.map((versionId) => (
            <label
              key={versionId}
              className="bg-warning/5 flex cursor-pointer items-start gap-3 border-b px-4 py-3 last:border-b-0"
            >
              <input
                type="checkbox"
                className="accent-foreground mt-1 size-4 shrink-0"
                checked
                disabled={!editing}
                onChange={() => onToggle(versionId)}
              />
              <span className="min-w-0 flex-1">
                <span className="text-sm font-medium">
                  {copy.historicalVersion}
                </span>
                <span className="text-muted-foreground mt-1 block font-mono text-xs break-all">
                  {versionId}
                </span>
                <span className="text-muted-foreground mt-1 block text-xs">
                  {copy.historicalVersionDescription}
                </span>
              </span>
            </label>
          ))}
        </div>
      )}
    </section>
  );
}

export function AgentCapabilityWorkbench({
  accountId,
  projectId,
  item,
  version,
  canAuthor,
  authoringPreparationPending = false,
  onBeginEditing,
  onDirtyChange,
  onVersionCreated,
}: {
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  version: AgentAssetVersion | null;
  canAuthor: boolean;
  authoringPreparationPending?: boolean;
  onBeginEditing: () => Promise<boolean>;
  onDirtyChange: (dirty: boolean) => void;
  onVersionCreated: (versionId: string) => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.capabilities;
  const skills = useProjectAssets(accountId, projectId, "skills");
  const mcps = useProjectAssets(accountId, projectId, "mcp-servers");
  const queryClient = useQueryClient();
  const privateWork = usePrivateWorkAccess();
  const update = useUpdateProjectAgentCapabilityBindings(accountId, projectId);
  const initialSkills =
    version?.skill_refs.map((ref) => `${ref.scope}:${ref.asset_id}`) ?? [];
  const initialMcps = version?.mcp_version_ids ?? [];
  const [baselineSkills, setBaselineSkills] = useState(initialSkills);
  const [baselineMcps, setBaselineMcps] = useState(initialMcps);
  const [draftSkills, setDraftSkills] = useState(initialSkills);
  const [draftMcps, setDraftMcps] = useState(initialMcps);
  const [editing, setEditing] = useState(false);
  const [query, setQuery] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [conflictRecovery, setConflictRecovery] =
    useState<AgentCapabilityConflictRecovery | null>(null);
  const recoveryGenerationRef = useRef(0);
  const recoveryAbortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(false);
  const scopeKey = `${accountId}:${projectId}:${item.id}`;
  const scopeKeyRef = useRef(scopeKey);
  const savedVersionIdRef = useRef<string | null>(null);
  const expectedRevisionRef = useRef(item.revision);
  const dirty =
    !sameIds(baselineSkills, draftSkills) || !sameIds(baselineMcps, draftMcps);
  const skillOptions = useMemo(
    () =>
      skills.data ? agentDependencyOptions("skill", skills.data, copy) : [],
    [copy, skills.data],
  );
  const mcpOptions = useMemo(
    () => (mcps.data ? agentDependencyOptions("mcp", mcps.data, copy) : []),
    [copy, mcps.data],
  );
  const mcpRuntime = useMcpDependencyRuntime({
    accountId,
    projectId,
    requiredVersionIds: draftMcps,
    enabled: editing,
  });
  const catalogLoading = skills.isLoading || mcps.isLoading;
  const catalogError = skills.error ?? mcps.error;

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (canAuthor || !editing) return;
    setLocalError(copy.permissionLost);
  }, [canAuthor, copy.permissionLost, editing]);

  useEffect(() => {
    mountedRef.current = true;
    scopeKeyRef.current = scopeKey;
    return () => {
      mountedRef.current = false;
      recoveryAbortRef.current?.abort();
      recoveryAbortRef.current = null;
      recoveryGenerationRef.current += 1;
      onDirtyChange(false);
    };
  }, [onDirtyChange, scopeKey]);

  function recoveryIsCurrent(
    recovery: AgentCapabilityConflictRecovery,
    controller: AbortController,
  ): boolean {
    return (
      mountedRef.current &&
      scopeKeyRef.current === scopeKey &&
      !controller.signal.aborted &&
      recoveryAbortRef.current === controller &&
      isPrivateWorkAccessActive(privateWork) &&
      recoveryGenerationRef.current === recovery.generation &&
      recovery.assetId === item.id
    );
  }

  function cancelRecovery() {
    recoveryAbortRef.current?.abort();
    recoveryAbortRef.current = null;
    recoveryGenerationRef.current += 1;
  }

  useEffect(() => {
    if (conflictRecovery || dirty || update.isPending) return;
    if (
      savedVersionIdRef.current &&
      version?.id !== savedVersionIdRef.current
    ) {
      if (item.revision <= expectedRevisionRef.current) return;
    }
    savedVersionIdRef.current = null;
    const nextSkills =
      version?.skill_refs.map((ref) => `${ref.scope}:${ref.asset_id}`) ?? [];
    const nextMcps = version?.mcp_version_ids ?? [];
    setBaselineSkills(nextSkills);
    setBaselineMcps(nextMcps);
    setDraftSkills(nextSkills);
    setDraftMcps(nextMcps);
    expectedRevisionRef.current = item.revision;
  }, [conflictRecovery, dirty, item.revision, update.isPending, version]);

  function discard() {
    cancelRecovery();
    setConflictRecovery(null);
    savedVersionIdRef.current = null;
    setDraftSkills(baselineSkills);
    setDraftMcps(baselineMcps);
    setEditing(false);
    setLocalError(null);
  }

  async function recoverFromConflict(
    recovery: AgentCapabilityConflictRecovery,
    controller: AbortController,
  ) {
    const startedAt = projectAgentAuthoringCacheEpochs({
      queryClient,
      accountId,
      projectId,
      assetId: recovery.assetId,
    });
    try {
      const reload = await runPrivateWorkAbortable(privateWork, (scopeSignal) =>
        reloadProjectAgentAuthoringState({
          projectId,
          assetId: recovery.assetId,
          attemptedRevision: recovery.assetRevision,
          includeDependencyCatalogs: true,
          signal: scopeSignal
            ? AbortSignal.any([controller.signal, scopeSignal])
            : controller.signal,
        }),
      );
      if (!recoveryIsCurrent(recovery, controller)) return;
      await cacheProjectAgentAuthoringReload({
        queryClient,
        accountId,
        projectId,
        assetId: recovery.assetId,
        reload,
        startedAt,
        isCurrent: () => recoveryIsCurrent(recovery, controller),
      });
      if (!recoveryIsCurrent(recovery, controller)) return;
      const nextSkills = reload.version.skill_refs.map(
        (ref) => `${ref.scope}:${ref.asset_id}`,
      );
      const nextMcps = reload.version.mcp_version_ids;
      setBaselineSkills(nextSkills);
      setBaselineMcps(nextMcps);
      expectedRevisionRef.current = reload.item.revision;
      savedVersionIdRef.current = reload.version.id;
      setConflictRecovery(null);
      setLocalError(
        sameIds(nextSkills, draftSkills) && sameIds(nextMcps, draftMcps)
          ? copy.recoverySynced
          : copy.recoveryPreserved,
      );
    } catch {
      if (!recoveryIsCurrent(recovery, controller)) return;
      setConflictRecovery({ ...recovery, status: "error" });
      setLocalError(copy.recoveryFailed);
    } finally {
      if (recoveryAbortRef.current === controller) {
        recoveryAbortRef.current = null;
      }
    }
  }

  function retryConflictRecovery() {
    if (!conflictRecovery) return;
    recoveryAbortRef.current?.abort();
    const controller = new AbortController();
    const recovery: AgentCapabilityConflictRecovery = {
      ...conflictRecovery,
      generation: recoveryGenerationRef.current + 1,
      status: "refreshing",
    };
    recoveryAbortRef.current = controller;
    recoveryGenerationRef.current = recovery.generation;
    setConflictRecovery(recovery);
    setLocalError(copy.recoveryReloading);
    void recoverFromConflict(recovery, controller);
  }

  async function save() {
    const saveScopeKey = scopeKey;
    const saveGeneration = recoveryGenerationRef.current;
    setLocalError(null);
    try {
      const result = await update.mutateAsync({
        assetId: item.id,
        input: {
          skill_refs: draftSkills.map((value) => {
            const [scope, asset_id] = value.split(":", 2);
            if ((scope !== "system" && scope !== "project") || !asset_id) {
              throw new TypeError("Invalid Skill asset reference");
            }
            return { scope, asset_id };
          }),
          mcp_version_ids: draftMcps,
          expected_revision: expectedRevisionRef.current,
        },
      });
      if (
        !mountedRef.current ||
        scopeKeyRef.current !== saveScopeKey ||
        !isPrivateWorkAccessActive(privateWork) ||
        recoveryGenerationRef.current !== saveGeneration
      ) {
        return;
      }
      const nextVersion = result.data;
      const nextSkillRefs = nextVersion.skill_refs.map(
        (ref) => `${ref.scope}:${ref.asset_id}`,
      );
      setBaselineSkills(nextSkillRefs);
      setBaselineMcps(nextVersion.mcp_version_ids);
      setDraftSkills(nextSkillRefs);
      setDraftMcps(nextVersion.mcp_version_ids);
      expectedRevisionRef.current += 1;
      savedVersionIdRef.current = nextVersion.id;
      setConflictRecovery(null);
      setEditing(false);
      onVersionCreated(nextVersion.id);
    } catch (error) {
      if (
        !mountedRef.current ||
        scopeKeyRef.current !== saveScopeKey ||
        !isPrivateWorkAccessActive(privateWork) ||
        recoveryGenerationRef.current !== saveGeneration
      ) {
        return;
      }
      if (
        error instanceof SharedAssetApiError &&
        error.code === "ASSET_CONFLICT"
      ) {
        recoveryAbortRef.current?.abort();
        const controller = new AbortController();
        const recovery: AgentCapabilityConflictRecovery = {
          assetId: item.id,
          assetRevision: expectedRevisionRef.current,
          generation: recoveryGenerationRef.current + 1,
          status: "refreshing",
        };
        recoveryAbortRef.current = controller;
        recoveryGenerationRef.current = recovery.generation;
        setConflictRecovery(recovery);
        setLocalError(`${copy.conflictDetected} ${copy.recoveryReloading}`);
        void recoverFromConflict(recovery, controller);
      } else {
        setLocalError(adminAssetErrorMessage(error, t.adminAssets.errors));
      }
    }
  }

  const saveBlockedReason = conflictRecovery
    ? conflictRecovery.status === "error"
      ? copy.reloadRequired
      : copy.recoveryReloading
    : !canAuthor
      ? copy.permissionBlocked
      : authoringPreparationPending
        ? copy.preparingCandidate
        : catalogLoading
          ? copy.catalogLoading
          : catalogError
            ? copy.catalogLoadFailed
            : mcpRuntime.isLoading
              ? copy.validatingMcp
              : mcpRuntime.error
                ? copy.mcpValidationFailed
                : mcpRuntime.blockReason;

  return (
    <section className="space-y-5" aria-labelledby="agent-capability-title">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2
            id="agent-capability-title"
            className="flex items-center gap-2 text-base font-semibold"
          >
            <WrenchIcon aria-hidden className="size-4" />
            {copy.title}
          </h2>
          <p className="text-muted-foreground mt-1 text-sm leading-6">
            {copy.description}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {editing ? (
            <>
              <Button
                type="button"
                variant="outline"
                disabled={update.isPending}
                onClick={discard}
              >
                {t.agents.common.cancel}
              </Button>
              <Button
                type="button"
                disabled={
                  !dirty || update.isPending || saveBlockedReason !== null
                }
                title={saveBlockedReason ?? undefined}
                onClick={() => void save()}
              >
                {update.isPending ? (
                  <Loader2Icon aria-hidden className="size-4 animate-spin" />
                ) : null}
                {update.isPending ? copy.saving : copy.saveCandidate}
              </Button>
            </>
          ) : canAuthor ? (
            <Button
              type="button"
              disabled={!version || catalogLoading || Boolean(catalogError)}
              onClick={() => {
                setLocalError(null);
                void onBeginEditing().then((ready) => {
                  if (ready) setEditing(true);
                });
              }}
            >
              {copy.edit}
            </Button>
          ) : null}
        </div>
      </div>

      <div className="bg-muted/30 flex flex-wrap items-center gap-2 rounded-xl px-4 py-3 text-sm">
        <span className="text-muted-foreground">{copy.builtinGroups}</span>
        <span className="font-medium">
          {t.agents.common.count(version?.tool_groups.length ?? 0)}
        </span>
        <span className="text-muted-foreground">{copy.unchanged}</span>
      </div>

      {editing ? (
        <div className="relative">
          <SearchIcon
            aria-hidden
            className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
          />
          <Input
            value={query}
            className="pl-9"
            placeholder={copy.searchPlaceholder}
            aria-label={copy.searchAria}
            disabled={
              update.isPending || authoringPreparationPending || !canAuthor
            }
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      ) : null}

      {catalogLoading ? (
        <p role="status" className="text-muted-foreground text-sm">
          {copy.catalogLoadingStatus}
        </p>
      ) : catalogError ? (
        <p role="alert" className="text-destructive text-sm">
          {copy.catalogLoadFailedStatus}
        </p>
      ) : (
        <div className="grid gap-5 lg:grid-cols-2">
          <AgentDependencySection
            title="Skill"
            emptyLabel={copy.emptySkills}
            options={skillOptions}
            selectedIds={draftSkills}
            editing={
              editing &&
              canAuthor &&
              !update.isPending &&
              !conflictRecovery &&
              !authoringPreparationPending
            }
            query={query}
            onToggle={(versionId) =>
              setDraftSkills((current) => toggleId(current, versionId))
            }
          />
          <AgentDependencySection
            title="MCP"
            emptyLabel={copy.emptyMcps}
            options={mcpOptions}
            selectedIds={draftMcps}
            editing={
              editing &&
              canAuthor &&
              !update.isPending &&
              !conflictRecovery &&
              !authoringPreparationPending
            }
            query={query}
            onToggle={(versionId) =>
              setDraftMcps((current) => toggleId(current, versionId))
            }
          />
        </div>
      )}

      {editing && saveBlockedReason ? (
        <p role="alert" className="text-destructive text-sm">
          {saveBlockedReason}
        </p>
      ) : null}
      {localError ? (
        <div className="flex flex-wrap items-center gap-2">
          <p role="alert" className="text-destructive text-sm">
            {localError}
          </p>
          {conflictRecovery?.status === "error" ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={retryConflictRecovery}
            >
              {copy.reload}
            </Button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

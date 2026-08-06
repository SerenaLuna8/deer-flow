"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  AddProjectMcpDialog,
  CredentialSecretDialog,
  type ProjectMcpCredentialOption,
  type ProjectMcpDraft,
  projectMcpDraftFromVersion,
} from "@/components/admin/assets/admin-asset-dialogs";
import {
  adminAssetErrorMessage,
  projectMcpCredentialErrorMessage,
} from "@/components/admin/assets/admin-asset-view-model";
import { projectConfiguredMcpErrorMessage } from "@/components/projects/assets/project-asset-view-model";
import { useI18n } from "@/core/i18n/hooks";
import type { Project } from "@/core/projects/types";
import {
  createProjectCredential,
  projectAssetKey,
  updateConfiguredMcpInputSchema,
  useApproveProjectMcpVersion,
  useUpdateConfiguredProjectMcp,
  type AssetVersion,
  type ConfiguredMcpResponse,
  type CreateCredentialInput,
  type ProjectMcpEditableConfigurationResponse,
  type UpdateConfiguredMcpInput,
} from "@/core/shared-assets";

import {
  PROJECT_MCP_CREDENTIAL_TYPE,
  requirementFromDraft,
  useCompatibleProjectCredentials,
} from "./project-mcp-create-dialog";

type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;
type McpConfiguredAggregate =
  | ConfiguredMcpResponse
  | ProjectMcpEditableConfigurationResponse;

export type ProjectMcpEditCompletion = {
  assetId: string;
  versionId: string;
  status: "published" | "pending_approval";
};

function stableJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableJson(item)).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function comparableMcpVersionDefinition(version: McpVersion) {
  return {
    description: version.definition.description,
    transport: version.definition.transport,
    command: version.definition.command,
    args: version.definition.args,
    url: version.definition.url,
    env: version.definition.env,
    headers: version.definition.headers,
    oauth: version.definition.oauth,
    routing: version.definition.routing,
    tool_overrides: version.definition.tool_overrides,
    timeout_seconds: version.definition.timeout_seconds,
    credential_slots: version.credential_slots.map((slot) => ({
      name: slot.name,
      purpose: slot.purpose,
      payload_schema: slot.payload_schema,
      required: slot.required,
    })),
  };
}

export function projectMcpEditInputMatchesVersion(
  input: UpdateConfiguredMcpInput,
  version: McpVersion,
): boolean {
  const parsed = updateConfiguredMcpInputSchema.parse(input);
  const { expected_asset_version: _expectedAssetVersion, ...definition } =
    parsed;
  void _expectedAssetVersion;
  return (
    stableJson(definition) ===
    stableJson(comparableMcpVersionDefinition(version))
  );
}

export function projectMcpEligibleGrantCredentialVersionId(
  version: McpVersion,
  options: readonly ProjectMcpCredentialOption[],
): string {
  const credentialVersionId = projectMcpActiveGrantCredentialVersionId(version);
  if (!credentialVersionId) return "";
  return options.some(
    (option) => option.credentialVersionId === credentialVersionId,
  )
    ? credentialVersionId
    : "";
}

export function projectMcpActiveGrantCredentialVersionId(
  version: McpVersion,
): string {
  const slot = version.credential_slots[0];
  if (!slot) return "";
  const grant = version.credential_grants.find(
    (item) => item.status === "active" && item.credential_slot_id === slot.id,
  );
  if (!grant) return "";
  return grant.credential_version_id;
}

export type ProjectMcpEditOperation =
  | { type: "approve"; target: McpConfiguredAggregate }
  | { type: "complete"; target: ProjectMcpEditableConfigurationResponse }
  | { type: "update" };

export function projectMcpEditOperation({
  approvalTarget,
  baseline,
  input,
  canApprove,
  credentialSelectionTouched,
  selectedCredentialVersionId,
  baselineCredentialVersionId,
}: {
  approvalTarget: McpConfiguredAggregate | null;
  baseline: ProjectMcpEditableConfigurationResponse;
  input: UpdateConfiguredMcpInput;
  canApprove: boolean;
  credentialSelectionTouched: boolean;
  selectedCredentialVersionId: string;
  baselineCredentialVersionId: string;
}): ProjectMcpEditOperation {
  if (approvalTarget) return { type: "approve", target: approvalTarget };
  const definitionChanged = !projectMcpEditInputMatchesVersion(
    input,
    baseline.version,
  );
  const credentialChanged =
    baseline.version.workflow_status === "published" &&
    credentialSelectionTouched &&
    selectedCredentialVersionId !== baselineCredentialVersionId;
  if (definitionChanged || credentialChanged) return { type: "update" };
  if (
    baseline.version.workflow_status === "pending_approval" &&
    canApprove &&
    selectedCredentialVersionId !== ""
  ) {
    return { type: "approve", target: baseline };
  }
  return { type: "complete", target: baseline };
}

export function ProjectMcpEditDialog({
  accountId,
  project,
  configuration,
  open,
  onOpenChange,
  onCompleted,
}: {
  accountId: string;
  project: Project;
  configuration: ProjectMcpEditableConfigurationResponse;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCompleted: (result: ProjectMcpEditCompletion) => void;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const updateMcp = useUpdateConfiguredProjectMcp(accountId, project.id);
  const approveMcp = useApproveProjectMcpVersion(accountId, project.id);
  const canApprove = project.capabilities.includes("mcp.credentials.approve");
  const initialDraft = useMemo(
    () => projectMcpDraftFromVersion(configuration.version),
    [configuration.version],
  );
  const [draft, setDraft] = useState<ProjectMcpDraft>(initialDraft);
  const [selectedCredentialVersionId, setSelectedCredentialVersionId] =
    useState("");
  const [createdCredential, setCreatedCredential] =
    useState<ProjectMcpCredentialOption | null>(null);
  const [credentialCreateOpen, setCredentialCreateOpen] = useState(false);
  const [credentialWritePending, setCredentialWritePending] = useState(false);
  const [credentialWriteError, setCredentialWriteError] = useState<
    string | null
  >(null);
  const [approvalTarget, setApprovalTarget] =
    useState<McpConfiguredAggregate | null>(null);
  const credentialWriteAbort = useRef<AbortController | null>(null);
  const selectionTouched = useRef(false);
  const requirementSignature = useRef(
    requirementFromDraft(initialDraft)
      ? `${initialDraft.authMode}:${initialDraft.fields.join("\u0000")}`
      : "none",
  );
  const requirement = useMemo(() => requirementFromDraft(draft), [draft]);
  const compatible = useCompatibleProjectCredentials({
    accountId,
    projectId: project.id,
    requirement,
    enabled: open && canApprove && requirement !== null,
  });
  const options = useMemo(() => {
    if (!createdCredential) return compatible.options;
    return [
      createdCredential,
      ...compatible.options.filter(
        (item) =>
          item.credentialVersionId !== createdCredential.credentialVersionId,
      ),
    ];
  }, [compatible.options, createdCredential]);
  const baselineCredentialVersionId = useMemo(
    () => projectMcpActiveGrantCredentialVersionId(configuration.version),
    [configuration.version],
  );
  const preselectedCredentialVersionId = useMemo(
    () =>
      projectMcpEligibleGrantCredentialVersionId(
        configuration.version,
        options,
      ),
    [configuration.version, options],
  );

  useEffect(() => {
    return () => credentialWriteAbort.current?.abort();
  }, [project.id]);

  useEffect(() => {
    if (
      selectionTouched.current ||
      selectedCredentialVersionId ||
      compatible.loading ||
      !preselectedCredentialVersionId
    ) {
      return;
    }
    setSelectedCredentialVersionId(preselectedCredentialVersionId);
  }, [
    compatible.loading,
    preselectedCredentialVersionId,
    selectedCredentialVersionId,
  ]);

  useEffect(() => {
    if (!selectedCredentialVersionId || compatible.loading) return;
    if (
      !options.some(
        (item) => item.credentialVersionId === selectedCredentialVersionId,
      )
    ) {
      setSelectedCredentialVersionId("");
    }
  }, [compatible.loading, options, selectedCredentialVersionId]);

  const handleDraftChange = useCallback((nextDraft: ProjectMcpDraft) => {
    const nextRequirement = requirementFromDraft(nextDraft);
    const nextSignature = nextRequirement
      ? `${nextRequirement.group}:${nextRequirement.fields.join("\u0000")}`
      : "none";
    if (requirementSignature.current !== nextSignature) {
      requirementSignature.current = nextSignature;
      selectionTouched.current = false;
      setSelectedCredentialVersionId("");
      setCreatedCredential(null);
    }
    setDraft(nextDraft);
  }, []);

  const complete = useCallback(
    (
      target: McpConfiguredAggregate,
      status: ProjectMcpEditCompletion["status"],
    ) => {
      onCompleted({
        assetId: target.item.id,
        versionId: target.version.id,
        status,
      });
      onOpenChange(false);
    },
    [onCompleted, onOpenChange],
  );

  const approve = useCallback(
    async (target: McpConfiguredAggregate) => {
      if (!canApprove || !selectedCredentialVersionId) return;
      const slot = target.version.credential_slots[0];
      if (!slot) return;
      setApprovalTarget(target);
      try {
        await approveMcp.mutateAsync({
          assetId: target.item.id,
          versionId: target.version.id,
          input: {
            credential_versions: {
              [slot.name]: selectedCredentialVersionId,
            },
            expected_asset_version: target.item.version,
          },
        });
        complete(target, "published");
      } catch {
        // Keep the exact target so the next submit retries approval only.
      }
    },
    [approveMcp, canApprove, complete, selectedCredentialVersionId],
  );

  const submit = useCallback(
    async (input: UpdateConfiguredMcpInput) => {
      const operation = projectMcpEditOperation({
        approvalTarget,
        baseline: configuration,
        input,
        canApprove,
        credentialSelectionTouched: selectionTouched.current,
        selectedCredentialVersionId,
        baselineCredentialVersionId,
      });
      if (operation.type === "approve") {
        await approve(operation.target);
        return;
      }
      if (operation.type === "complete") {
        complete(operation.target, operation.target.version.workflow_status);
        return;
      }
      try {
        const result = await updateMcp.mutateAsync({
          assetId: configuration.item.id,
          input,
        });
        if (
          result.version.workflow_status === "pending_approval" &&
          canApprove &&
          selectedCredentialVersionId
        ) {
          setApprovalTarget(result);
          await approve(result);
          return;
        }
        complete(result, result.version.workflow_status);
      } catch {
        // The active dialog renders the mutation's safe public error.
      }
    },
    [
      approvalTarget,
      approve,
      baselineCredentialVersionId,
      canApprove,
      complete,
      configuration,
      selectedCredentialVersionId,
      updateMcp,
    ],
  );

  const createCredential = useCallback(
    async (input: CreateCredentialInput) => {
      const currentRequirement = requirement;
      if (!currentRequirement || !canApprove) return;
      credentialWriteAbort.current?.abort();
      const controller = new AbortController();
      credentialWriteAbort.current = controller;
      setCredentialWritePending(true);
      setCredentialWriteError(null);
      try {
        const result = await createProjectCredential(
          project.id,
          input,
          controller.signal,
        );
        if (credentialWriteAbort.current !== controller) return;
        const credentialVersionId = result.item.current_version_id;
        if (!credentialVersionId) {
          setCredentialWriteError(t.adminAssets.errors.invalidResponse);
          return;
        }
        const option = {
          credentialId: result.item.id,
          credentialVersionId,
          displayName: result.item.display_name,
          name: result.item.name,
        };
        selectionTouched.current = true;
        setCreatedCredential(option);
        setSelectedCredentialVersionId(credentialVersionId);
        setCredentialCreateOpen(false);
        await queryClient.invalidateQueries({
          queryKey: projectAssetKey(accountId, project.id, "credentials"),
        });
      } catch (error) {
        if (!controller.signal.aborted) {
          setCredentialWriteError(
            adminAssetErrorMessage(error, t.adminAssets.errors),
          );
        }
      } finally {
        if (credentialWriteAbort.current === controller) {
          credentialWriteAbort.current = null;
          setCredentialWritePending(false);
        }
      }
    },
    [accountId, canApprove, project.id, queryClient, requirement, t],
  );

  const updateError = updateMcp.error
    ? projectConfiguredMcpErrorMessage(updateMcp.error)
    : null;
  const approvalError = approveMcp.error
    ? projectMcpCredentialErrorMessage(approveMcp.error, t.adminAssets.errors)
    : null;
  const flowError = approvalTarget
    ? (approvalError ?? t.adminAssets.dialogs.mcpSavedApprovalFailed)
    : updateError;
  const submitLabel = approvalTarget
    ? t.adminAssets.dialogs.retryMcpApproval
    : draft.authMode === "none"
      ? t.adminAssets.dialogs.saveAndPublishMcpConfig
      : canApprove && selectedCredentialVersionId
        ? t.adminAssets.dialogs.saveAndApproveMcpConfig
        : t.adminAssets.dialogs.saveAndSubmitMcpConfig;
  const mutationPending =
    updateMcp.isPending || approveMcp.isPending || credentialWritePending;
  const pending = mutationPending || compatible.loading;

  return (
    <>
      <AddProjectMcpDialog
        open={open}
        pending={pending}
        errorMessage={flowError}
        editConfiguration={{
          asset: configuration.item,
          version: configuration.version,
        }}
        credentialSelection={{
          canApprove,
          loading: compatible.loading,
          errorMessage: compatible.error
            ? t.adminAssets.dialogs.approval.credentialsFailed
            : null,
          options,
          selectedCredentialVersionId,
          onChange: (credentialVersionId) => {
            selectionTouched.current = true;
            setSelectedCredentialVersionId(credentialVersionId);
          },
          onCreate: () => {
            if (requirement) {
              setCredentialWriteError(null);
              setCredentialCreateOpen(true);
            }
          },
          onRetry: () => void compatible.refetch(),
        }}
        configurationLocked={approvalTarget !== null}
        submitLabel={submitLabel}
        footerNote={
          approvalTarget
            ? t.adminAssets.dialogs.mcpSavedRetryApprovalOnly
            : undefined
        }
        onDraftChange={handleDraftChange}
        onOpenChange={(next) => {
          if (!next && mutationPending) return;
          onOpenChange(next);
        }}
        onSubmit={(input) => void submit(input as UpdateConfiguredMcpInput)}
      />

      <CredentialSecretDialog
        mode="create"
        open={credentialCreateOpen}
        pending={credentialWritePending}
        fixedFields
        fixedCredentialType={PROJECT_MCP_CREDENTIAL_TYPE}
        errorMessage={credentialWriteError}
        initialFields={
          requirement?.fields.map((field) => ({
            group: requirement.group,
            field,
          })) ?? []
        }
        onOpenChange={(next) => {
          if (!next && credentialWritePending) return;
          setCredentialCreateOpen(next);
          if (!next) setCredentialWriteError(null);
        }}
        onCreate={(input) => void createCredential(input)}
      />
    </>
  );
}

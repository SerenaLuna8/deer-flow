"use client";

import { useQueries, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  AddProjectMcpDialog,
  CredentialSecretDialog,
  type ProjectMcpAuthMode,
  type ProjectMcpCredentialOption,
  type ProjectMcpCredentialSlotGroup,
  type ProjectMcpDraft,
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
  listProjectAssetVersions,
  projectAssetKey,
  projectAssetVersionsKey,
  useApproveProjectMcpVersion,
  useCreateConfiguredProjectMcp,
  useProjectAssets,
  type AssetVersion,
  type ConfiguredMcpResponse,
  type CreateConfiguredMcpInput,
  type CreateCredentialInput,
  type ProjectCredentialItem,
  type ProjectCredentialList,
} from "@/core/shared-assets";

export const PROJECT_MCP_CREDENTIAL_TYPE = "mcp_auth";

export type ProjectMcpCredentialRequirement = {
  group: ProjectMcpCredentialSlotGroup;
  fields: string[];
};

export type ProjectMcpCredentialHistoryRow = {
  credential: ProjectCredentialItem;
  versions: AssetVersion[];
};

export type ProjectMcpSubmitIntent = "publish" | "approve" | "submit";

function exactStringListMatch(
  left: readonly string[],
  right: readonly string[],
): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

export function compatibleProjectCredentialOptions(
  rows: readonly ProjectMcpCredentialHistoryRow[],
  requirement: ProjectMcpCredentialRequirement,
): ProjectMcpCredentialOption[] {
  return rows
    .flatMap(({ credential, versions }) => {
      if (
        credential.scope !== "project" ||
        credential.status !== "active" ||
        credential.current_version_id === null
      ) {
        return [];
      }
      const currentVersion = versions.find(
        (version) =>
          "credential_id" in version &&
          version.credential_id === credential.id &&
          version.id === credential.current_version_id &&
          version.status === "active",
      );
      if (!currentVersion || !("credential_id" in currentVersion)) return [];
      const groups = Object.keys(currentVersion.payload_schema);
      if (
        groups.length !== 1 ||
        groups[0] !== requirement.group ||
        !exactStringListMatch(
          currentVersion.payload_schema[requirement.group] ?? [],
          requirement.fields,
        )
      ) {
        return [];
      }
      return [
        {
          credentialId: credential.id,
          credentialVersionId: credential.current_version_id,
          displayName: credential.display_name,
          name: credential.name,
        },
      ];
    })
    .sort((left, right) => left.displayName.localeCompare(right.displayName));
}

export function projectMcpSubmitIntent({
  authMode,
  canApprove,
  selectedCredentialVersionId,
}: {
  authMode: ProjectMcpAuthMode;
  canApprove: boolean;
  selectedCredentialVersionId: string | null;
}): ProjectMcpSubmitIntent {
  if (authMode === "none") return "publish";
  return canApprove && selectedCredentialVersionId ? "approve" : "submit";
}

export function requirementFromDraft(
  draft: ProjectMcpDraft,
): ProjectMcpCredentialRequirement | null {
  if (draft.authMode === "none" || draft.fields.length === 0) return null;
  return { group: draft.authMode, fields: draft.fields };
}

export function useCompatibleProjectCredentials({
  accountId,
  projectId,
  requirement,
  enabled,
}: {
  accountId: string;
  projectId: string;
  requirement: ProjectMcpCredentialRequirement | null;
  enabled: boolean;
}) {
  const catalog = useProjectAssets(
    accountId,
    projectId,
    "credentials",
    enabled,
  );
  const data = catalog.data as ProjectCredentialList | undefined;
  const candidates = useMemo(
    () =>
      enabled
        ? (data?.project_items ?? []).filter(
            (credential) =>
              credential.status === "active" &&
              credential.current_version_id !== null,
          )
        : [],
    [data?.project_items, enabled],
  );
  const histories = useQueries({
    queries: candidates.map((credential) => ({
      queryKey: projectAssetVersionsKey(
        accountId,
        projectId,
        "credentials",
        credential.id,
      ),
      queryFn: ({ signal }) =>
        listProjectAssetVersions(
          projectId,
          "credentials",
          credential.id,
          signal,
        ),
      enabled,
    })),
  });
  const rows = candidates.map((credential, index) => ({
    credential,
    versions: histories[index]?.data?.data ?? [],
  }));
  const options =
    enabled && requirement
      ? compatibleProjectCredentialOptions(rows, requirement)
      : [];
  const historyError = histories.find((query) => query.error)?.error ?? null;

  return {
    options,
    loading:
      enabled &&
      (catalog.isLoading || histories.some((query) => query.isLoading)),
    error: enabled ? (catalog.error ?? historyError) : null,
    refetch: async () => {
      if (!enabled) return;
      await catalog.refetch();
      await Promise.all(histories.map((query) => query.refetch()));
    },
  };
}

export type ProjectMcpCreateCompletion = {
  assetId: string;
  versionId: string;
  status: "published" | "pending_approval";
};

const EMPTY_DRAFT: ProjectMcpDraft = {
  transport: "http",
  url: "",
  authMode: "none",
  fields: [],
};

export function ProjectMcpCreateDialog({
  accountId,
  project,
  open,
  onOpenChange,
  onCompleted,
}: {
  accountId: string;
  project: Project;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCompleted: (result: ProjectMcpCreateCompletion) => void;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const createMcp = useCreateConfiguredProjectMcp(accountId, project.id);
  const approveMcp = useApproveProjectMcpVersion(accountId, project.id);
  const canApprove = project.capabilities.includes("mcp.credentials.approve");
  const [draft, setDraft] = useState<ProjectMcpDraft>(EMPTY_DRAFT);
  const [selectedCredentialVersionId, setSelectedCredentialVersionId] =
    useState("");
  const [createdCredential, setCreatedCredential] =
    useState<ProjectMcpCredentialOption | null>(null);
  const [credentialCreateOpen, setCredentialCreateOpen] = useState(false);
  const [credentialWritePending, setCredentialWritePending] = useState(false);
  const [credentialWriteError, setCredentialWriteError] = useState<
    string | null
  >(null);
  const [pendingApproval, setPendingApproval] =
    useState<ConfiguredMcpResponse | null>(null);
  const credentialWriteAbort = useRef<AbortController | null>(null);
  const requirementSignature = useRef("none");
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

  useEffect(() => {
    return () => credentialWriteAbort.current?.abort();
  }, [project.id]);

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

  const reset = useCallback(() => {
    credentialWriteAbort.current?.abort();
    credentialWriteAbort.current = null;
    createMcp.reset();
    approveMcp.reset();
    setDraft(EMPTY_DRAFT);
    setSelectedCredentialVersionId("");
    setCreatedCredential(null);
    setCredentialCreateOpen(false);
    setCredentialWritePending(false);
    setCredentialWriteError(null);
    setPendingApproval(null);
    requirementSignature.current = "none";
  }, [approveMcp, createMcp]);

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (
        !next &&
        (createMcp.isPending || approveMcp.isPending || credentialWritePending)
      ) {
        return;
      }
      if (!next) reset();
      onOpenChange(next);
    },
    [
      approveMcp.isPending,
      createMcp.isPending,
      credentialWritePending,
      onOpenChange,
      reset,
    ],
  );

  const handleDraftChange = useCallback((nextDraft: ProjectMcpDraft) => {
    const nextRequirement = requirementFromDraft(nextDraft);
    const nextSignature = nextRequirement
      ? `${nextRequirement.group}:${nextRequirement.fields.join("\u0000")}`
      : "none";
    if (requirementSignature.current !== nextSignature) {
      requirementSignature.current = nextSignature;
      setSelectedCredentialVersionId("");
      setCreatedCredential(null);
    }
    setDraft(nextDraft);
  }, []);

  const complete = useCallback(
    (
      result: ConfiguredMcpResponse,
      status: ProjectMcpCreateCompletion["status"],
    ) => {
      onCompleted({
        assetId: result.item.id,
        versionId: result.version.id,
        status,
      });
      reset();
      onOpenChange(false);
    },
    [onCompleted, onOpenChange, reset],
  );

  const approvePending = useCallback(
    async (result: ConfiguredMcpResponse) => {
      if (!selectedCredentialVersionId) return;
      const slot = result.version.credential_slots[0];
      if (!slot) return;
      try {
        await approveMcp.mutateAsync({
          assetId: result.item.id,
          versionId: result.version.id,
          input: {
            credential_versions: {
              [slot.name]: selectedCredentialVersionId,
            },
            expected_asset_version: result.item.version,
          },
        });
        complete(result, "published");
      } catch {
        setPendingApproval(result);
      }
    },
    [approveMcp, complete, selectedCredentialVersionId],
  );

  const submit = useCallback(
    async (input: CreateConfiguredMcpInput) => {
      if (pendingApproval) {
        await approvePending(pendingApproval);
        return;
      }
      try {
        const result = await createMcp.mutateAsync(input);
        if (
          result.version.workflow_status === "pending_approval" &&
          canApprove &&
          selectedCredentialVersionId
        ) {
          setPendingApproval(result);
          await approvePending(result);
          return;
        }
        complete(result, result.version.workflow_status);
      } catch {
        // The active dialog renders the mutation's safe public error.
      }
    },
    [
      approvePending,
      canApprove,
      complete,
      createMcp,
      pendingApproval,
      selectedCredentialVersionId,
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

  const submitIntent = projectMcpSubmitIntent({
    authMode: draft.authMode,
    canApprove,
    selectedCredentialVersionId: selectedCredentialVersionId || null,
  });
  const submitLabel = pendingApproval
    ? t.adminAssets.dialogs.retryMcpApproval
    : submitIntent === "publish"
      ? t.adminAssets.dialogs.addAndPublish
      : submitIntent === "approve"
        ? t.adminAssets.dialogs.addAndApprove
        : t.adminAssets.dialogs.addAndSubmitApproval;
  const createError = createMcp.error
    ? projectConfiguredMcpErrorMessage(createMcp.error)
    : null;
  const approvalError = approveMcp.error
    ? projectMcpCredentialErrorMessage(approveMcp.error, t.adminAssets.errors)
    : null;
  const flowError = pendingApproval
    ? (approvalError ?? t.adminAssets.dialogs.mcpSavedApprovalFailed)
    : createError;

  return (
    <>
      <AddProjectMcpDialog
        open={open}
        pending={
          createMcp.isPending || approveMcp.isPending || credentialWritePending
        }
        configurationLocked={pendingApproval !== null}
        errorMessage={flowError}
        credentialSelection={{
          canApprove,
          loading: compatible.loading,
          errorMessage: compatible.error
            ? t.adminAssets.dialogs.approval.credentialsFailed
            : null,
          options,
          selectedCredentialVersionId,
          onChange: setSelectedCredentialVersionId,
          onCreate: () => {
            if (requirement) {
              setCredentialWriteError(null);
              setCredentialCreateOpen(true);
            }
          },
          onRetry: () => void compatible.refetch(),
        }}
        submitLabel={submitLabel}
        footerNote={
          pendingApproval
            ? t.adminAssets.dialogs.mcpSavedRetryApprovalOnly
            : undefined
        }
        onDraftChange={handleDraftChange}
        onOpenChange={handleOpenChange}
        onSubmit={(input) => void submit(input as CreateConfiguredMcpInput)}
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
